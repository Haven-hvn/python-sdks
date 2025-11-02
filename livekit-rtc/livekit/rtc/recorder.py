# Copyright 2023 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module for recording remote participant streams to WebM files."""

from __future__ import annotations

import asyncio
import logging
import gc
from dataclasses import dataclass
from typing import Optional, Set
from pathlib import Path
from fractions import Fraction
import time

try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False
    av = None  # type: ignore

from .room import Room
from .participant import RemoteParticipant
from .track import RemoteVideoTrack, RemoteAudioTrack, Track
from .track_publication import RemoteTrackPublication
from .video_stream import VideoStream, VideoFrameEvent
from .audio_stream import AudioStream, AudioFrameEvent
from .video_frame import VideoFrame
from .audio_frame import AudioFrame
from ._proto.track_pb2 import TrackKind
from ._proto.video_frame_pb2 import VideoBufferType

logger = logging.getLogger(__name__)


class RecordingError(Exception):
    """Base exception for recording-related errors."""

    pass


class ParticipantNotFoundError(RecordingError):
    """Raised when a participant is not found in the room."""

    pass


class TrackNotFoundError(RecordingError):
    """Raised when a track is not found or not available."""

    pass


class WebMEncoderNotAvailableError(RecordingError):
    """Raised when WebM encoding libraries are not available."""

    pass


@dataclass
class RecordingStats:
    """Statistics for a recording session."""

    video_frames_recorded: int = 0
    audio_frames_recorded: int = 0
    recording_duration_seconds: float = 0.0
    output_file_size_bytes: int = 0


class ParticipantRecorder:
    """Records video and audio from a remote participant to a WebM file.

    This class captures WebRTC streams from a specific remote participant in a LiveKit room
    and encodes them into a WebM file with VP8/VP9 video and Opus audio codecs.

    Example:
        ```python
        recorder = ParticipantRecorder(room)
        await recorder.start_recording("participant_identity")
        
        # Recording is now in progress...
        await asyncio.sleep(60)  # Record for 1 minute
        
        await recorder.stop_recording("output.webm")
        stats = recorder.get_stats()
        print(f"Recorded {stats.video_frames_recorded} video frames")
        ```
    """

    def __init__(
        self,
        room: Room,
        video_codec: str = "vp8",
        audio_codec: str = "opus",
        video_bitrate: int = 2000000,  # 2 Mbps
        audio_bitrate: int = 128000,  # 128 kbps
        video_fps: int = 30,
    ) -> None:
        """Initialize a ParticipantRecorder instance.

        Args:
            room: The LiveKit Room instance connected to the session.
            video_codec: Video codec to use ('vp8' or 'vp9'). Defaults to 'vp8'.
            audio_codec: Audio codec to use. Defaults to 'opus'.
            video_bitrate: Target video bitrate in bits per second. Defaults to 2000000 (2 Mbps).
            audio_bitrate: Target audio bitrate in bits per second. Defaults to 128000 (128 kbps).
            video_fps: Target video frame rate. Defaults to 30.

        Raises:
            WebMEncoderNotAvailableError: If PyAV is not installed.
        """
        if not HAS_AV:
            raise WebMEncoderNotAvailableError(
                "PyAV is required for recording. Install it with: pip install av"
            )

        if video_codec not in ("vp8", "vp9"):
            raise ValueError("video_codec must be 'vp8' or 'vp9'")

        self.room = room
        self.video_codec = video_codec
        self.audio_codec = audio_codec
        self.video_bitrate = video_bitrate
        self.audio_bitrate = audio_bitrate
        self.video_fps = video_fps

        self._participant_identity: Optional[str] = None
        self._participant: Optional[RemoteParticipant] = None
        self._is_recording: bool = False
        self._recording_task: Optional[asyncio.Task[None]] = None

        # Streams for video and audio capture
        self._video_capture_stream: Optional[VideoStream] = None
        self._audio_capture_stream: Optional[AudioStream] = None

        # Frame queues with bounded size to prevent excessive memory growth
        # Buffer approximately 30 seconds of frames at target fps to handle bursts
        # This allows sufficient buffering while still limiting memory growth
        # For 30fps video: 30 * 30 = 900 frames (~27MB of video data)
        # For audio at ~100fps: 30 * 100 = 3000 frames (~10MB of audio data)
        max_video_queue_size = max(500, video_fps * 30)  # At least 500, or 30 seconds worth
        # Audio frames arrive at ~100fps (every ~10ms), so 30 seconds = 3000 frames
        max_audio_queue_size = max(1000, video_fps * 100)  # ~30 seconds at typical audio frame rate
        
        self._video_queue: asyncio.Queue[VideoFrameEvent] = asyncio.Queue(maxsize=max_video_queue_size)
        self._audio_queue: asyncio.Queue[AudioFrameEvent] = asyncio.Queue(maxsize=max_audio_queue_size)

        # Background tasks for capturing frames
        self._video_capture_task: Optional[asyncio.Task[None]] = None
        self._audio_capture_task: Optional[asyncio.Task[None]] = None

        # Incremental encoding state
        self._encoding_task: Optional[asyncio.Task[None]] = None
        self._output_container: Optional[av.container.OutputContainer] = None
        self._output_file_path: Optional[str] = None
        self._container_was_initialized: bool = False  # Track if container was ever initialized
        self._video_stream: Optional[av.VideoStream] = None  # PyAV stream
        self._audio_stream: Optional[av.AudioStream] = None  # PyAV stream
        self._video_stream_initialized: bool = False
        self._audio_stream_initialized: bool = False
        self._cumulative_audio_samples: int = 0
        self._first_video_frame_time: Optional[float] = None
        self._video_frame_count: int = 0

        # Synchronization
        self._lock = asyncio.Lock()
        self._start_time: Optional[float] = None
        self._recording_start_time_us: Optional[int] = None

        # Statistics
        self._stats = RecordingStats()

        # Track subscriptions tracking
        self._subscribed_track_sids: Set[str] = set()

    async def start_recording(self, participant_identity: str) -> None:
        """Start recording from the specified participant.

        Args:
            participant_identity: The identity of the remote participant to record.

        Raises:
            ParticipantNotFoundError: If the participant is not found in the room.
            RecordingError: If recording is already in progress or fails to start.
        """
        async with self._lock:
            if self._is_recording:
                raise RecordingError("Recording is already in progress")

            # Find the participant
            participant = self.room.remote_participants.get(participant_identity)
            if not participant:
                raise ParticipantNotFoundError(
                    f"Participant '{participant_identity}' not found in room"
                )

            self._participant_identity = participant_identity
            self._participant = participant
            self._is_recording = True
            self._start_time = time.time()

            # Subscribe to all published tracks
            await self._subscribe_to_participant_tracks(participant)

            # Set up event handlers for tracks that may be published later
            self._setup_track_handlers(participant)

            # Wait for tracks to be available and start capturing
            await self._wait_for_tracks_and_start_capture()
            
            # Start incremental encoding task
            self._encoding_task = asyncio.create_task(self._incremental_encoding_loop())

            logger.info(
                f"Started recording participant '{participant_identity}'"
            )

    async def _subscribe_to_participant_tracks(
        self, participant: RemoteParticipant
    ) -> None:
        """Subscribe to all published tracks of a participant."""
        for publication in participant.track_publications.values():
            if not publication.subscribed:
                publication.set_subscribed(True)
                logger.debug(
                    f"Subscribed to track {publication.sid} "
                    f"(kind: {publication.kind})"
                )

    def _setup_track_handlers(self, participant: RemoteParticipant) -> None:
        """Set up event handlers for track publication events."""
        original_on_track_subscribed = None

        async def on_track_subscribed_wrapper(
            track: Track,
            publication: RemoteTrackPublication,
            p: RemoteParticipant,
        ) -> None:
            # Only handle tracks from the participant we're recording
            if p.identity != self._participant_identity:
                return

            if original_on_track_subscribed:
                await original_on_track_subscribed(track, publication, p)

            # If we're recording, start capturing from this track
            if self._is_recording and publication.sid not in self._subscribed_track_sids:
                self._subscribed_track_sids.add(publication.sid)
                await self._start_capture_for_track(track, publication)

        # This is a simplified approach - in practice, we rely on the room's
        # track_subscribed event which we'll monitor in the main recording loop
        pass

    async def _wait_for_tracks_and_start_capture(self) -> None:
        """Wait for tracks to be subscribed and start capturing frames."""
        if not self._participant:
            return

        # Wait a bit for tracks to become available
        await asyncio.sleep(0.5)

        # Start capturing from already-subscribed tracks
        for publication in self._participant.track_publications.values():
            if publication.subscribed and publication.track:
                track = publication.track
                if publication.sid not in self._subscribed_track_sids:
                    self._subscribed_track_sids.add(publication.sid)
                    await self._start_capture_for_track(track, publication)

        # If no tracks found yet, we'll wait for them via event handlers
        if not self._subscribed_track_sids:
            logger.warning(
                f"No tracks found for participant '{self._participant_identity}'. "
                "Will wait for tracks to be published."
            )

    async def _start_capture_for_track(
        self, track: Track, publication: RemoteTrackPublication
    ) -> None:
        """Start capturing frames from a track."""
        if track.kind == TrackKind.KIND_VIDEO and isinstance(
            track, RemoteVideoTrack
        ):
            if self._video_capture_stream is None:
                # Set bounded capacity on VideoStream's internal queue to prevent unbounded memory growth
                # Use same capacity as our recorder queue (~30 seconds of frames)
                stream_capacity = max(500, self.video_fps * 30)
                self._video_capture_stream = VideoStream(track, capacity=stream_capacity)
                self._video_capture_task = asyncio.create_task(
                    self._capture_video_frames()
                )
                logger.debug(f"Started video capture from track {track.sid} with capacity={stream_capacity}")

        elif track.kind == TrackKind.KIND_AUDIO and isinstance(
            track, RemoteAudioTrack
        ):
            if self._audio_capture_stream is None:
                # Set bounded capacity on AudioStream's internal queue to prevent unbounded memory growth
                # Use same capacity as our recorder queue (~30 seconds of frames)
                # Audio frames arrive at ~100fps, so 30 seconds = 3000 frames
                stream_capacity = max(1000, self.video_fps * 100)
                # Use Opus-compatible settings
                self._audio_capture_stream = AudioStream.from_track(
                    track=track,
                    sample_rate=48000,  # Opus typically uses 48kHz
                    num_channels=2,  # Stereo
                    capacity=stream_capacity,
                )
                self._audio_capture_task = asyncio.create_task(
                    self._capture_audio_frames()
                )
                logger.debug(f"Started audio capture from track {track.sid} with capacity={stream_capacity}")

    async def _capture_video_frames(self) -> None:
        """Capture video frames from the video stream."""
        if not self._video_capture_stream:
            logger.warning("Video capture stream is None, cannot capture frames")
            return

        logger.debug("Starting video frame capture")
        try:
            first_frame = True
            frame_count = 0
            async for frame_event in self._video_capture_stream:
                if not self._is_recording:
                    logger.debug(f"Recording stopped, breaking video capture loop. Captured {frame_count} frames")
                    break
                # Store first frame timestamp as reference for synchronization
                if first_frame and self._start_time:
                    # Convert start_time (seconds) to microseconds for consistency
                    self._recording_start_time_us = int(self._start_time * 1_000_000)
                    first_frame = False
                    logger.debug(f"Captured first video frame. Queue size before put: {self._video_queue.qsize()}")
                
                # Put frame in queue, waiting if necessary
                # The bounded queue size limits memory growth while allowing backpressure
                await self._video_queue.put(frame_event)
                frame_count += 1
                self._stats.video_frames_recorded += 1
                if frame_count % 100 == 0:
                    logger.debug(f"Captured {frame_count} video frames. Queue size: {self._video_queue.qsize()}")
        except Exception as e:
            logger.error(f"Error capturing video frames: {e}", exc_info=True)
        finally:
            if self._video_capture_stream:
                await self._video_capture_stream.aclose()
                self._video_capture_stream = None

    async def _capture_audio_frames(self) -> None:
        """Capture audio frames from the audio stream."""
        if not self._audio_capture_stream:
            logger.warning("Audio capture stream is None, cannot capture frames")
            return

        logger.debug("Starting audio frame capture")
        try:
            first_frame = True
            frame_count = 0
            async for frame_event in self._audio_capture_stream:
                if not self._is_recording:
                    logger.debug(f"Recording stopped, breaking audio capture loop. Captured {frame_count} frames")
                    break
                
                if first_frame:
                    logger.debug(f"Captured first audio frame. Queue size before put: {self._audio_queue.qsize()}")
                    first_frame = False
                
                # Put frame in queue, waiting if necessary
                # The bounded queue size limits memory growth while allowing backpressure
                await self._audio_queue.put(frame_event)
                frame_count += 1
                self._stats.audio_frames_recorded += 1
                if frame_count % 500 == 0:
                    logger.debug(f"Captured {frame_count} audio frames. Queue size: {self._audio_queue.qsize()}")
        except Exception as e:
            logger.error(f"Error capturing audio frames: {e}", exc_info=True)
        finally:
            if self._audio_capture_stream:
                await self._audio_capture_stream.aclose()
                self._audio_capture_stream = None

    async def _incremental_encoding_loop(self) -> None:
        """Incremental encoding loop that processes frames as they arrive."""
        import tempfile
        
        logger.info("Incremental encoding loop started")
        
        # Create temporary file for incremental encoding
        # This will be moved to the final location when stop_recording is called
        temp_fd, temp_path = tempfile.mkstemp(suffix=".webm", prefix="livekit_recording_")
        import os
        os.close(temp_fd)  # Close file descriptor, we'll open it with av.open
        self._output_file_path = temp_path
        logger.debug(f"Created temporary output file: {temp_path}")
        
        container_initialized = False
        container_init_lock = asyncio.Lock()
        
        try:
            
            # Process frames from queues as they arrive
            video_frame_count = 0
            audio_frame_count = 0
            logger.debug(f"Starting frame processing. Video queue size: {self._video_queue.qsize()}, Audio queue size: {self._audio_queue.qsize()}")
            
            # Use separate tasks to process video and audio frames concurrently
            # This allows proper waiting for frames without blocking
            async def process_video_frames():
                nonlocal video_frame_count, container_initialized
                frames_processed = False
                logger.debug("Video frame processing task started")
                while self._is_recording or not self._video_queue.empty():
                    try:
                        # Wait for frame with timeout to check recording status periodically
                        if self._is_recording:
                            try:
                                logger.debug(f"Waiting for video frame. Queue size: {self._video_queue.qsize()}, Recording: {self._is_recording}")
                                frame_event = await asyncio.wait_for(
                                    self._video_queue.get(), timeout=0.1
                                )
                                frames_processed = True
                                logger.debug(f"Received video frame {video_frame_count + 1}")
                            except asyncio.TimeoutError:
                                continue
                        else:
                            # When recording stopped, process remaining frames
                            # Wait a bit longer to ensure all frames are captured
                            queue_size = self._video_queue.qsize()
                            logger.debug(f"Recording stopped. Processing remaining video frames. Queue size: {queue_size}")
                            try:
                                frame_event = await asyncio.wait_for(
                                    self._video_queue.get(), timeout=1.0
                                )
                                frames_processed = True
                                logger.debug(f"Processed remaining video frame {video_frame_count + 1}")
                            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                                logger.debug(f"Video queue empty. Processed {video_frame_count} frames total")
                                break
                        
                        async with container_init_lock:
                            if not container_initialized:
                                # Initialize container when first frame arrives
                                output_file = Path(self._output_file_path).resolve()
                                output_file.parent.mkdir(parents=True, exist_ok=True)
                                self._output_container = av.open(str(output_file), mode="w", format="webm")
                                container_initialized = True
                                self._container_was_initialized = True
                                logger.info("Initialized output container for incremental encoding")
                        
                        if not self._video_stream_initialized:
                            # Initialize video stream from first frame
                            frame = frame_event.frame
                            video_width = frame.width
                            video_height = frame.height
                            
                            self._video_stream = self._output_container.add_stream(
                                self.video_codec,
                                rate=self.video_fps,
                            )
                            self._video_stream.width = video_width
                            self._video_stream.height = video_height
                            self._video_stream.pix_fmt = "yuv420p"
                            self._video_stream.options = {
                                "bitrate": str(self.video_bitrate),
                            }
                            # Set time_base for video (1/1000 for milliseconds-based timing)
                            self._video_stream.time_base = Fraction(1, 1000)
                            self._video_stream_initialized = True
                            logger.info(f"Initialized video stream: {video_width}x{video_height}")
                        
                        # Encode video frame
                        await self._encode_video_frame_incremental(frame_event, video_frame_count)
                        video_frame_count += 1
                        
                        # Release frame reference immediately after encoding
                        frame_event.frame = None  # type: ignore
                        
                    except Exception as e:
                        logger.error(f"Error processing video frame: {e}", exc_info=True)
                        break
            
            async def process_audio_frames():
                nonlocal audio_frame_count, container_initialized
                frames_processed = False
                logger.debug("Audio frame processing task started")
                while self._is_recording or not self._audio_queue.empty():
                    try:
                        # Wait for frame with timeout to check recording status periodically
                        if self._is_recording:
                            try:
                                logger.debug(f"Waiting for audio frame. Queue size: {self._audio_queue.qsize()}, Recording: {self._is_recording}")
                                frame_event = await asyncio.wait_for(
                                    self._audio_queue.get(), timeout=0.1
                                )
                                frames_processed = True
                                logger.debug(f"Received audio frame {audio_frame_count + 1}")
                            except asyncio.TimeoutError:
                                continue
                        else:
                            # When recording stopped, process remaining frames
                            # Wait a bit longer to ensure all frames are captured
                            queue_size = self._audio_queue.qsize()
                            logger.debug(f"Recording stopped. Processing remaining audio frames. Queue size: {queue_size}")
                            try:
                                frame_event = await asyncio.wait_for(
                                    self._audio_queue.get(), timeout=1.0
                                )
                                frames_processed = True
                                logger.debug(f"Processed remaining audio frame {audio_frame_count + 1}")
                            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                                logger.debug(f"Audio queue empty. Processed {audio_frame_count} frames total")
                                break
                        
                        async with container_init_lock:
                            if not container_initialized:
                                # Initialize container when first frame arrives
                                output_file = Path(self._output_file_path).resolve()
                                output_file.parent.mkdir(parents=True, exist_ok=True)
                                self._output_container = av.open(str(output_file), mode="w", format="webm")
                                container_initialized = True
                                self._container_was_initialized = True
                                logger.info("Initialized output container for incremental encoding")
                        
                        if not self._audio_stream_initialized:
                            # Initialize audio stream from first frame
                            frame = frame_event.frame
                            
                            self._audio_stream = self._output_container.add_stream(self.audio_codec)
                            self._audio_stream.rate = frame.sample_rate
                            # Set layout based on number of channels
                            if frame.num_channels == 1:
                                layout_str = "mono"
                            elif frame.num_channels == 2:
                                layout_str = "stereo"
                            else:
                                layout_str = f"{frame.num_channels}"
                            self._audio_stream.codec_context.layout = layout_str
                            self._audio_stream.options = {"bitrate": str(self.audio_bitrate)}
                            # Set time_base to match sample rate to avoid timestamp issues
                            self._audio_stream.time_base = Fraction(1, frame.sample_rate)
                            self._audio_stream_initialized = True
                            logger.info(f"Initialized audio stream: {frame.sample_rate}Hz, {frame.num_channels}ch")
                        
                        # Encode audio frame
                        await self._encode_audio_frame_incremental(frame_event)
                        audio_frame_count += 1
                        
                        # Release frame reference immediately after encoding
                        frame_event.frame = None  # type: ignore
                        
                    except Exception as e:
                        logger.error(f"Error processing audio frame: {e}", exc_info=True)
                        break
            
            # Process video and audio frames concurrently
            logger.debug("Starting concurrent frame processing tasks")
            await asyncio.gather(
                process_video_frames(),
                process_audio_frames(),
            )
            logger.debug("Frame processing tasks completed")
            
            # Flush encoders
            if self._video_stream:
                for packet in self._video_stream.encode():
                    self._output_container.mux(packet)
            
            if self._audio_stream:
                for packet in self._audio_stream.encode():
                    self._output_container.mux(packet)
            
            logger.info(f"Incremental encoding completed: {video_frame_count} video, {audio_frame_count} audio frames")
            
            # If no frames were processed, log a warning
            if video_frame_count == 0 and audio_frame_count == 0:
                logger.warning("No frames were processed during incremental encoding")
                if not container_initialized:
                    logger.warning("Container was never initialized - no frames arrived")
            
        except Exception as e:
            logger.error(f"Error in incremental encoding loop: {e}", exc_info=True)
            raise
        finally:
            if self._output_container:
                try:
                    self._output_container.close()
                    logger.debug("Output container closed")
                except Exception as e:
                    logger.error(f"Error closing output container: {e}", exc_info=True)
                self._output_container = None
            else:
                if self._output_file_path:
                    logger.debug(f"No container was initialized, temp file remains at: {self._output_file_path}")

    def _encode_video_frame_incremental_sync(self, frame_event: VideoFrameEvent, frame_index: int) -> None:
        """Encode a single video frame incrementally (synchronous)."""
        if not self._video_stream or not self._output_container:
            return
        
        # Calculate PTS based on frame index and actual frame rate
        # Use frame index * frame_interval to ensure proper spacing
        if self._video_stream.time_base:
            time_base_denominator = self._video_stream.time_base.denominator
            time_base_numerator = self._video_stream.time_base.numerator
        else:
            time_base_denominator = 1000
            time_base_numerator = 1
        
        # Calculate frame interval based on target FPS
        frame_interval = int(time_base_denominator / (self.video_fps * time_base_numerator))
        if frame_interval < 1:
            frame_interval = 1
        
        pts = frame_index * frame_interval
        
        # Convert and encode frame
        frame = frame_event.frame
        pyav_frame = self._convert_video_frame_to_pyav(frame, self._video_stream, pts)
        if pyav_frame and self._video_stream:
            for packet in self._video_stream.encode(pyav_frame):
                self._output_container.mux(packet)
        
        # Release frame reference immediately
        del pyav_frame
        
        # Periodic GC (every 50 frames)
        if frame_index > 0 and frame_index % 50 == 0:
            gc.collect()

    async def _encode_video_frame_incremental(self, frame_event: VideoFrameEvent, frame_index: int) -> None:
        """Encode a single video frame incrementally."""
        # Encoding is fast enough that we can do it directly in async context
        # PyAV encode operations are typically < 10ms per frame
        self._encode_video_frame_incremental_sync(frame_event, frame_index)

    def _encode_audio_frame_incremental_sync(self, frame_event: AudioFrameEvent) -> None:
        """Encode a single audio frame incrementally (synchronous)."""
        if not self._audio_stream or not self._output_container:
            return
        
        frame = frame_event.frame
        sample_rate = frame.sample_rate
        samples_per_channel = frame.samples_per_channel
        
        # Calculate audio PTS based on cumulative samples
        # Use time_base of audio stream (1/sample_rate) to avoid timestamp issues
        # PTS = cumulative_samples (since time_base is 1/sample_rate)
        audio_pts = self._cumulative_audio_samples
        
        # Convert and encode frame
        pyav_frame = self._convert_audio_frame_to_pyav(frame, self._audio_stream, audio_pts)
        if pyav_frame and self._audio_stream:
            for packet in self._audio_stream.encode(pyav_frame):
                self._output_container.mux(packet)
            
            # Update cumulative samples for next frame
            self._cumulative_audio_samples += samples_per_channel
        
        # Release frame reference immediately
        del pyav_frame
        
        # Periodic GC (every ~4 seconds of audio)
        if self._cumulative_audio_samples % (sample_rate * 4) == 0:
            gc.collect()

    async def _encode_audio_frame_incremental(self, frame_event: AudioFrameEvent) -> None:
        """Encode a single audio frame incrementally."""
        # Encoding is fast enough that we can do it directly in async context
        self._encode_audio_frame_incremental_sync(frame_event)

    async def stop_recording(self, output_path: str) -> str:
        """Stop recording and save to a WebM file.

        Args:
            output_path: Path where the WebM file should be saved.

        Returns:
            The absolute path to the saved file.

        Raises:
            RecordingError: If recording is not in progress or encoding fails.
        """
        async with self._lock:
            if not self._is_recording:
                raise RecordingError("Recording is not in progress")

            self._is_recording = False

            # Update statistics
            if self._start_time:
                self._stats.recording_duration_seconds = (
                    time.time() - self._start_time
                )

            logger.info("Stopping recording and encoding to WebM...")

            # Stop capturing tasks
            if self._video_capture_task:
                self._video_capture_task.cancel()
                try:
                    await self._video_capture_task
                except asyncio.CancelledError:
                    pass

            if self._audio_capture_task:
                self._audio_capture_task.cancel()
                try:
                    await self._audio_capture_task
                except asyncio.CancelledError:
                    pass

            # Close streams
            if self._video_capture_stream:
                try:
                    await self._video_capture_stream.aclose()
                except asyncio.CancelledError:
                    pass
                self._video_capture_stream = None

            if self._audio_capture_stream:
                try:
                    await self._audio_capture_stream.aclose()
                except asyncio.CancelledError:
                    pass
                self._audio_capture_stream = None

            # Wait for encoding task to complete and flush/close
            if self._encoding_task:
                try:
                    await self._encoding_task
                except Exception as e:
                    logger.error(f"Error during incremental encoding: {e}", exc_info=True)
                    raise RecordingError(f"Failed to complete encoding: {e}") from e
            
            # Move temporary file to final location
            import shutil
            import os
            if self._output_file_path and os.path.exists(self._output_file_path):
                # Check if container was initialized (file should have content)
                file_size = os.path.getsize(self._output_file_path)
                if file_size > 0 or self._container_was_initialized:
                    output_file_obj = Path(output_path).resolve()
                    output_file_obj.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(self._output_file_path, str(output_file_obj))
                    output_file = str(output_file_obj)
                    logger.info(f"Moved encoded file from {self._output_file_path} to {output_file} (size: {file_size} bytes)")
                else:
                    # Container was never initialized (no frames arrived)
                    logger.warning(f"No frames were encoded. Container was never initialized. Temp file size: {file_size} bytes")
                    # Remove empty temp file
                    try:
                        os.unlink(self._output_file_path)
                    except Exception:
                        pass
                    # Create empty output file or raise error
                    output_file_obj = Path(output_path).resolve()
                    output_file_obj.parent.mkdir(parents=True, exist_ok=True)
                    output_file_obj.touch()
                    output_file = str(output_file_obj)
            else:
                # No temp file was created (encoding task might have failed)
                logger.warning("No output file was created. Encoding may have failed.")
                output_file_obj = Path(output_path).resolve()
                output_file_obj.parent.mkdir(parents=True, exist_ok=True)
                output_file_obj.touch()
                output_file = str(output_file_obj)

            # Clean up all references to allow proper resource release
            self._participant_identity = None
            self._participant = None
            self._subscribed_track_sids.clear()
            # Clear room reference to allow room to be disconnected
            self.room = None  # type: ignore

            logger.info(f"Recording saved to {output_file}")
            return output_file

    async def _encode_to_webm(self, output_path: str) -> str:
        """Encode captured frames to a WebM file incrementally.
        
        This method processes frames as they're dequeued rather than collecting
        all frames first, significantly reducing memory usage for long recordings.
        """
        output_file = Path(output_path).resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Check if we have any frames to encode
        if self._video_queue.empty() and self._audio_queue.empty():
            logger.warning(
                "No frames captured during recording. Creating empty file will be skipped."
            )
            # Create an empty file but mark it as such
            output_file.touch()
            return str(output_file)

        # Process frames incrementally - collect them in small batches and sort
        # This allows us to process frames in order while limiting memory usage
        # We'll collect frames in batches, sort each batch, then encode incrementally
        video_frames_batch: list[VideoFrameEvent] = []
        audio_frames_batch: list[AudioFrameEvent] = []
        
        # Collect remaining frames from queues (queues are bounded so this is limited)
        while True:
            try:
                frame_event = self._video_queue.get_nowait()
                video_frames_batch.append(frame_event)
            except asyncio.QueueEmpty:
                break

        while True:
            try:
                frame_event = self._audio_queue.get_nowait()
                audio_frames_batch.append(frame_event)
            except asyncio.QueueEmpty:
                break

        # Sort frames by timestamp to ensure proper synchronization order
        # Since queues are bounded (max ~5 seconds of frames), sorting is still efficient
        video_frames_batch.sort(key=lambda e: e.timestamp_us)
        # Audio frames don't have timestamps, but we'll process them in order

        # Run encoding in a thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        absolute_path = await loop.run_in_executor(
            None,
            self._encode_to_webm_sync,
            str(output_file),
            video_frames_batch,
            audio_frames_batch,
        )

        # Clear frames from memory after encoding to prevent memory leaks
        del video_frames_batch
        del audio_frames_batch

        # Get file size
        output_file_obj = Path(absolute_path)
        if output_file_obj.exists():
            self._stats.output_file_size_bytes = output_file_obj.stat().st_size

        return absolute_path

    def _encode_to_webm_sync(
        self,
        output_path: str,
        video_frames: list[VideoFrameEvent],
        audio_frames: list[AudioFrameEvent],
    ) -> str:
        """Synchronously encode frames to WebM (runs in executor)."""
        logger.info(f"Starting WebM encoding: {len(video_frames)} video frames, {len(audio_frames)} audio frames")
        # Create output container
        output_container = av.open(output_path, mode="w", format="webm")

        try:
            video_stream: Optional[av.VideoStream] = None
            audio_stream: Optional[av.AudioStream] = None

            # Determine video properties from first frame
            if video_frames:
                first_video_frame = video_frames[0].frame
                video_width = first_video_frame.width
                video_height = first_video_frame.height

                # Add video stream
                video_stream = output_container.add_stream(
                    self.video_codec,
                    rate=self.video_fps,
                )
                video_stream.width = video_width
                video_stream.height = video_height
                video_stream.pix_fmt = "yuv420p"
                video_stream.options = {
                    "bitrate": str(self.video_bitrate),
                }

            # Add audio stream
            if audio_frames:
                first_audio_frame = audio_frames[0].frame
                audio_stream = output_container.add_stream(self.audio_codec)
                audio_stream.rate = first_audio_frame.sample_rate
                # Set layout (channels is read-only on stream and codec_context)
                # Determine layout string based on number of channels
                if first_audio_frame.num_channels == 1:
                    layout_str = "mono"
                elif first_audio_frame.num_channels == 2:
                    layout_str = "stereo"
                else:
                    layout_str = f"{first_audio_frame.num_channels}"
                audio_stream.codec_context.layout = layout_str
                audio_stream.options = {"bitrate": str(self.audio_bitrate)}

            # Encode video frames using frame-based PTS calculation
            # Calculate spacing based on actual recording duration to ensure correct playback speed
            if video_stream:
                total_video_frames = len(video_frames)
                logger.debug(f"Encoding {total_video_frames} video frames...")
                
                # Calculate frame interval in time_base units
                if video_stream.time_base:
                    time_base_denominator = video_stream.time_base.denominator
                    time_base_numerator = video_stream.time_base.numerator
                else:
                    time_base_denominator = 1000
                    time_base_numerator = 1
                
                # Calculate actual frame rate from recording duration
                # This ensures video plays at correct speed over the full duration
                recording_duration_sec = self._stats.recording_duration_seconds
                if recording_duration_sec > 0 and total_video_frames > 0:
                    # Calculate actual frame rate from captured frames
                    actual_fps = total_video_frames / recording_duration_sec
                    logger.debug(f"Recording duration: {recording_duration_sec:.2f}s, frames: {total_video_frames}, actual FPS: {actual_fps:.2f}")
                else:
                    # Fallback to target FPS
                    actual_fps = self.video_fps
                
                # Calculate frame interval based on actual frame rate
                # This ensures frames are evenly spaced over the recording duration
                # frame_interval = time_base_denominator / (actual_fps * time_base_numerator)
                frame_interval = int(time_base_denominator / (actual_fps * time_base_numerator))
                if frame_interval < 1:
                    frame_interval = 1
                
                logger.debug(f"Frame interval: {frame_interval} PTS units ({frame_interval/1000:.3f} ms per frame at {actual_fps:.2f} fps)")
                
                # Calculate PTS for each frame based on frame index
                # This ensures frames are spaced correctly at the target frame rate
                for idx, frame_event in enumerate(video_frames):
                    if idx > 0 and idx % 100 == 0:
                        logger.debug(f"Encoding video frame {idx}/{len(video_frames)}...")
                    frame = frame_event.frame
                    
                    # Calculate PTS based on frame index
                    # PTS = frame_index * frame_interval
                    # Frame 0: PTS = 0 (starts immediately)
                    # Frame 1: PTS = frame_interval (~33ms at 30fps)
                    # Frame 30: PTS = 30 * frame_interval (~1 second at 30fps)
                    pts = idx * frame_interval
                    
                    # Convert VideoFrame to PyAV frame
                    pyav_frame = self._convert_video_frame_to_pyav(
                        frame, video_stream, pts
                    )
                    if pyav_frame and video_stream:
                        for packet in video_stream.encode(pyav_frame):
                            output_container.mux(packet)
                    
                    # Explicitly release frame references to help GC
                    # This is critical for memory efficiency as frames can be large (~3-4MB each)
                    del pyav_frame
                    # Also release the frame reference from the event
                    # This helps Python GC free the underlying frame data sooner
                    frame_event.frame = None  # type: ignore
                    
                    # Trigger GC periodically to free memory (every 50 frames)
                    # This helps prevent memory accumulation during encoding
                    if idx > 0 and idx % 50 == 0:
                        gc.collect()

            # Flush video encoder
            if video_stream:
                for packet in video_stream.encode():
                    output_container.mux(packet)

            # Encode audio frames synchronized with video timestamps
            # Calculate audio PTS based on cumulative samples, aligned with video timing
            # Use the same time_base as video (1/1000 milliseconds) for consistency
            if audio_stream:
                sample_rate = audio_frames[0].frame.sample_rate if audio_frames else 48000
                
                # Calculate audio PTS in milliseconds (same time_base as video: 1/1000)
                # Audio PTS should match the video timing based on sample count
                cumulative_samples = 0
                
                for frame_event in audio_frames:
                    frame = frame_event.frame
                    samples_per_channel = frame.samples_per_channel
                    
                    # Calculate PTS in milliseconds based on cumulative samples
                    # This naturally aligns with video timing since both start from 0
                    # PTS_ms = (cumulative_samples / sample_rate) * 1000
                    audio_pts_ms = int((cumulative_samples / sample_rate) * 1000)
                    
                    # Convert AudioFrame to PyAV frame
                    pyav_frame = self._convert_audio_frame_to_pyav(
                        frame, audio_stream, audio_pts_ms
                    )
                    if pyav_frame and audio_stream:
                        for packet in audio_stream.encode(pyav_frame):
                            output_container.mux(packet)
                        # Increment by samples per channel for next frame
                        cumulative_samples += samples_per_channel
                    
                    # Explicitly release frame references to help GC
                    del pyav_frame
                    # Release the frame reference from the event to help GC free audio data
                    frame_event.frame = None  # type: ignore
                    
                    # Trigger GC periodically (every 200 audio frames since they're smaller)
                    if cumulative_samples % (sample_rate * 4) == 0:  # Every ~4 seconds of audio
                        gc.collect()

            # Flush audio encoder
            if audio_stream:
                for packet in audio_stream.encode():
                    output_container.mux(packet)

        finally:
            output_container.close()
            # Explicitly release all frame references to help GC
            # This is critical for memory efficiency - frames can be several MB each
            for frame_event in video_frames:
                if hasattr(frame_event, 'frame'):
                    frame_event.frame = None  # type: ignore
            for frame_event in audio_frames:
                if hasattr(frame_event, 'frame'):
                    frame_event.frame = None  # type: ignore
            video_frames.clear()
            audio_frames.clear()
            
            # Force garbage collection after encoding to free memory
            # This is important because frames can be large and GC might not run immediately
            gc.collect()

        return output_path

    def _convert_video_frame_to_pyav(
        self,
        frame: VideoFrame,
        stream: Optional[av.VideoStream],
        pts: int,
    ) -> Optional[av.VideoFrame]:
        """Convert a VideoFrame to a PyAV VideoFrame.
        
        Note: This creates a copy of frame data. For memory efficiency, frame references
        should be released after encoding (handled in calling code).
        """
        if not stream:
            return None

        try:
            # Convert frame to I420 format if needed
            # This creates a new VideoFrame - the old one will be GC'd after use
            converted_frame = None
            if frame.type != VideoBufferType.I420:
                converted_frame = frame.convert(VideoBufferType.I420)
                frame = converted_frame

            # Get I420 planes as memoryviews (these include stride padding)
            y_plane = frame.get_plane(0)
            u_plane = frame.get_plane(1)
            v_plane = frame.get_plane(2)

            if not y_plane or not u_plane or not v_plane:
                return None

            # Create PyAV frame directly and update planes
            # PyAV doesn't support from_ndarray with different-sized planes for YUV420P
            # So we create the frame and update planes manually
            pyav_frame = av.VideoFrame(frame.width, frame.height, "yuv420p")
            
            # Copy plane data accounting for stride differences
            # The memoryview from get_plane() includes stride padding
            # We need to copy the actual image data, skipping stride padding if needed
            
            # Calculate strides
            y_stride = len(y_plane) // frame.height
            pyav_y_stride = pyav_frame.planes[0].line_size
            chroma_height = frame.height // 2
            u_stride = len(u_plane) // chroma_height if chroma_height > 0 else 0
            v_stride = len(v_plane) // chroma_height if chroma_height > 0 else 0
            pyav_u_stride = pyav_frame.planes[1].line_size
            pyav_v_stride = pyav_frame.planes[2].line_size
            
            # Copy Y plane row by row, handling stride differences
            # Use memoryview for direct access without intermediate numpy copies where possible
            pyav_y_data = bytearray(pyav_frame.planes[0].buffer_size)
            y_mv = memoryview(y_plane)
            for row in range(frame.height):
                src_start = row * y_stride
                dst_start = row * pyav_y_stride
                copy_len = min(frame.width, min(y_stride, pyav_y_stride))
                # Direct slice assignment avoids tobytes() copy
                pyav_y_data[dst_start:dst_start + copy_len] = y_mv[src_start:src_start + copy_len]
            # Use memoryview for direct update if possible, otherwise use bytes()
            # PyAV's update() accepts bytes/memoryview, so we can pass the bytearray directly as memoryview
            pyav_frame.planes[0].update(memoryview(pyav_y_data))
            # Release intermediate buffers immediately
            del pyav_y_data, y_mv
            
            # Copy U plane row by row
            chroma_width = frame.width // 2
            pyav_u_data = bytearray(pyav_frame.planes[1].buffer_size)
            u_mv = memoryview(u_plane)
            for row in range(chroma_height):
                src_start = row * u_stride
                dst_start = row * pyav_u_stride
                copy_len = min(chroma_width, min(u_stride, pyav_u_stride))
                pyav_u_data[dst_start:dst_start + copy_len] = u_mv[src_start:src_start + copy_len]
            # Use memoryview instead of bytes() to avoid extra copy
            pyav_frame.planes[1].update(memoryview(pyav_u_data))
            # Release intermediate buffers immediately
            del pyav_u_data, u_mv
            
            # Copy V plane row by row
            pyav_v_data = bytearray(pyav_frame.planes[2].buffer_size)
            v_mv = memoryview(v_plane)
            for row in range(chroma_height):
                src_start = row * v_stride
                dst_start = row * pyav_v_stride
                copy_len = min(chroma_width, min(v_stride, pyav_v_stride))
                pyav_v_data[dst_start:dst_start + copy_len] = v_mv[src_start:src_start + copy_len]
            # Use memoryview instead of bytes() to avoid extra copy
            pyav_frame.planes[2].update(memoryview(pyav_v_data))
            # Release intermediate buffers immediately
            del pyav_v_data, v_mv
            
            pyav_frame.pts = pts
            # Set time_base only if stream has one and it's valid
            if stream and hasattr(stream, 'time_base') and stream.time_base is not None:
                try:
                    pyav_frame.time_base = stream.time_base
                except (AttributeError, ValueError, TypeError) as e:
                    logger.warning(f"Could not set time_base: {e}")
                    # Continue without time_base - PyAV may infer it

            # Release converted frame reference if we created one
            # This helps GC free the conversion buffer sooner
            if converted_frame is not None:
                del converted_frame
            
            return pyav_frame
        except Exception as e:
            logger.error(f"Error converting video frame: {e}", exc_info=True)
            return None

    def _convert_audio_frame_to_pyav(
        self,
        frame: AudioFrame,
        stream: Optional[av.AudioStream],
        pts: int,
    ) -> Optional[av.AudioFrame]:
        """Convert an AudioFrame to a PyAV AudioFrame."""
        if not stream:
            return None

        try:
            # Get audio data as numpy array
            import numpy as np

            audio_data = np.frombuffer(frame.data, dtype=np.int16)

            # PyAV expects "packed" format: 1D array with shape (1, samples*channels)
            # AudioFrame.data is already interleaved (channels interleaved)
            # Reshape to (1, samples*channels) for PyAV's packed format
            audio_packed = audio_data.reshape(1, -1)

            # Determine layout string
            if frame.num_channels == 1:
                layout = "mono"
            elif frame.num_channels == 2:
                layout = "stereo"
            else:
                layout = f"{frame.num_channels}"

            # Create PyAV frame with packed format (1, samples*channels)
            pyav_frame = av.AudioFrame.from_ndarray(
                audio_packed,
                format="s16",
                layout=layout,
            )
            pyav_frame.sample_rate = frame.sample_rate
            pyav_frame.pts = pts
            # Set time_base only if stream has one (it may be None initially)
            if stream and stream.time_base:
                pyav_frame.time_base = stream.time_base

            return pyav_frame
        except Exception as e:
            logger.error(f"Error converting audio frame: {e}", exc_info=True)
            return None

    def get_stats(self) -> RecordingStats:
        """Get statistics about the current or completed recording.

        Returns:
            RecordingStats object with recording statistics.
        """
        if self._start_time and self._is_recording:
            self._stats.recording_duration_seconds = (
                time.time() - self._start_time
            )
        return RecordingStats(
            video_frames_recorded=self._stats.video_frames_recorded,
            audio_frames_recorded=self._stats.audio_frames_recorded,
            recording_duration_seconds=self._stats.recording_duration_seconds,
            output_file_size_bytes=self._stats.output_file_size_bytes,
        )

    @property
    def is_recording(self) -> bool:
        """Check if recording is currently in progress."""
        return self._is_recording

