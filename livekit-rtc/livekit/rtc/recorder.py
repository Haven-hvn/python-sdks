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
        video_quality: str = "medium",  # "low", "medium", "high", "best"
        auto_bitrate: bool = True,  # Auto-adjust bitrate based on resolution
    ) -> None:
        """Initialize a ParticipantRecorder instance.

        Args:
            room: The LiveKit Room instance connected to the session.
            video_codec: Video codec to use ('vp8' or 'vp9'). Defaults to 'vp8'.
                VP9 provides better quality at the same bitrate but is slower to encode.
            audio_codec: Audio codec to use. Defaults to 'opus'.
            video_bitrate: Target video bitrate in bits per second. Defaults to 2000000 (2 Mbps).
                If auto_bitrate is True, this will be adjusted based on resolution.
            audio_bitrate: Target audio bitrate in bits per second. Defaults to 128000 (128 kbps).
            video_fps: Target video frame rate. Defaults to 30.
            video_quality: Quality preset for encoding ('low', 'medium', 'high', 'best').
                Higher quality uses slower encoding but produces better results.
                Defaults to 'medium'.
            auto_bitrate: If True, automatically adjust bitrate based on resolution.
                Higher resolutions get higher bitrates. Defaults to True.

        Raises:
            WebMEncoderNotAvailableError: If PyAV is not installed.
        """
        if not HAS_AV:
            raise WebMEncoderNotAvailableError(
                "PyAV is required for recording. Install it with: pip install av"
            )

        if video_codec not in ("vp8", "vp9"):
            raise ValueError("video_codec must be 'vp8' or 'vp9'")
        
        if video_quality not in ("low", "medium", "high", "best"):
            raise ValueError("video_quality must be 'low', 'medium', 'high', or 'best'")

        self.room = room
        self.video_codec = video_codec
        self.audio_codec = audio_codec
        self.video_bitrate = video_bitrate
        self.audio_bitrate = audio_bitrate
        self.video_fps = video_fps
        self.video_quality = video_quality
        self.auto_bitrate = auto_bitrate

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
        
        # Store (event, capture_time) tuples to track wall-clock arrival time
        self._video_queue: asyncio.Queue[tuple[VideoFrameEvent, float]] = asyncio.Queue(maxsize=max_video_queue_size)
        self._audio_queue: asyncio.Queue[tuple[AudioFrameEvent, float]] = asyncio.Queue(maxsize=max_audio_queue_size)

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
        self._last_video_pts: int = -1  # For monotonic PTS enforcement

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

            # Reset incremental encoding state for new recording
            self._first_video_frame_time = None
            self._cumulative_audio_samples = 0
            self._video_frame_count = 0
            self._last_video_pts = -1  # Reset monotonic PTS tracker

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
                self._subscribed_track_sids.add(publication.sid)
                logger.debug(
                    f"Subscribed to track {publication.sid} "
                    f"(kind: {publication.kind})"
                )

    async def _unsubscribe_from_participant_tracks(
        self, participant: Optional[RemoteParticipant]
    ) -> None:
        """Unsubscribe from all tracks that were subscribed by this recorder.
        
        This method only unsubscribes from tracks that this recorder subscribed to,
        to avoid interfering with other subscriptions that may exist.
        """
        if not participant:
            return
        
        for publication in participant.track_publications.values():
            # Only unsubscribe if we subscribed to this track
            if publication.sid in self._subscribed_track_sids and publication.subscribed:
                try:
                    publication.set_subscribed(False)
                    logger.debug(
                        f"Unsubscribed from track {publication.sid} "
                        f"(kind: {publication.kind})"
                    )
                except Exception as e:
                    logger.warning(
                        f"Error unsubscribing from track {publication.sid}: {e}"
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
                # We store capture time to ensure PTS matches wall clock time
                await self._video_queue.put((frame_event, time.time()))
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
                await self._audio_queue.put((frame_event, time.time()))
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
                    # Debug log every 50 frames
                    if video_frame_count % 50 == 0:
                        try:
                            import psutil, os
                            process = psutil.Process(os.getpid())
                            mem = process.memory_info().rss / 1024 / 1024
                            logger.debug(
                                f"🎥 Video Loop | Frame: {video_frame_count} | "
                                f"Queue: {self._video_queue.qsize()} | Mem: {mem:.1f}MB"
                            )
                        except ImportError:
                            pass

                    try:
                        # Wait for frame with timeout to check recording status periodically
                        if self._is_recording:
                            try:
                                logger.debug(f"Waiting for video frame. Queue size: {self._video_queue.qsize()}, Recording: {self._is_recording}")
                                frame_data = await asyncio.wait_for(
                                    self._video_queue.get(), timeout=0.1
                                )
                                frame_event, capture_time = frame_data
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
                                frame_data = await asyncio.wait_for(
                                    self._video_queue.get(), timeout=1.0
                                )
                                frame_event, capture_time = frame_data
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
                            
                            # Log exact parameters before stream creation
                            logger.info(
                                f"Attempting to add_stream: codec={self.video_codec}, "
                                f"rate={self.video_fps}, width={video_width}, height={video_height}, "
                                f"auto_bitrate={self.auto_bitrate}"
                            )
                            
                            # Store first frame timestamp as reference for PTS calculation
                            self._first_video_frame_time = frame_event.timestamp_us
                            
                            # Calculate optimal bitrate if auto_bitrate is enabled
                            actual_bitrate = self._calculate_bitrate(video_width, video_height)
                            
                            try:
                                self._video_stream = self._output_container.add_stream(
                                    self.video_codec,
                                    rate=self.video_fps,
                                )
                                self._video_stream.width = video_width
                                self._video_stream.height = video_height
                                self._video_stream.pix_fmt = "yuv420p"
                                
                                # Build encoding options with quality settings
                                encoding_options = self._get_video_encoding_options(actual_bitrate)
                                self._video_stream.options = encoding_options
                                logger.info(f"Video encoding: {video_width}x{video_height} @ {actual_bitrate/1_000_000:.2f} Mbps, quality={self.video_quality}")
                                
                                # CRITICAL: Allow PyAV to determine best time_base (likely 1/FPS)
                                # We will convert to 1/1000 ONLY if the container specifically demands it during muxing
                                # This prevents fighting the encoder's native timebase
                                # self._video_stream.time_base = Fraction(1, 1000)
                                
                                if self._video_stream.time_base:
                                    logger.info(f"Stream time_base auto-detected as: {self._video_stream.time_base}")
                                else:
                                    logger.warning("Stream time_base is None after add_stream - will fallback to 1/FPS")

                                self._video_stream_initialized = True
                                logger.info(
                                    f"✅ Initialized video stream: {video_width}x{video_height}, "
                                    f"first timestamp: {self._first_video_frame_time}us, "
                                    f"time_base: {self._video_stream.time_base}, "
                                    f"bitrate: {actual_bitrate/1_000_000:.2f}Mbps, "
                                    f"codec: {self.video_codec}, fps: {self.video_fps}"
                                )
                            except Exception as e:
                                logger.critical(f"❌ PyAV add_stream failed: {e}")
                                raise
                        
                        # Encode video frame
                        await self._encode_video_frame_incremental(frame_event, video_frame_count, capture_time)
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
                                frame_data = await asyncio.wait_for(
                                    self._audio_queue.get(), timeout=0.1
                                )
                                frame_event, capture_time = frame_data
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
                                frame_data = await asyncio.wait_for(
                                    self._audio_queue.get(), timeout=1.0
                                )
                                frame_event, capture_time = frame_data
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
                        await self._encode_audio_frame_incremental(frame_event, capture_time)
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
                flush_packet_idx = 0
                for packet in self._video_stream.encode():
                    logger.debug(
                        f"Flush video packet {flush_packet_idx}: "
                        f"pts={packet.pts}, dts={packet.dts}, "
                        f"packet_tb={packet.time_base}, stream_tb={self._video_stream.time_base}"
                    )
                    
                    # Enforce monotonicity for flush packets too
                    # The encoder might emit a packet with a PTS lower than our artificially adjusted last PTS
                    if packet.pts is not None:
                        if packet.pts <= self._last_video_pts:
                            old_pts = packet.pts
                            packet.pts = self._last_video_pts + 1
                            logger.debug(f"Flush video packet {flush_packet_idx}: Adjusted PTS {old_pts} -> {packet.pts} for monotonicity")
                        self._last_video_pts = packet.pts

                    # Force set duration if missing (CRITICAL for 0xc0000094 prevention)
                    if not packet.duration and self.video_fps > 0:
                        # Use packet's current timebase to calculate duration
                        current_tb = packet.time_base or self._video_stream.time_base
                        if current_tb and current_tb.numerator > 0:
                            tb_val = current_tb.numerator / current_tb.denominator
                            packet.duration = int(1 / (self.video_fps * tb_val))
                            logger.debug(f"Flush video packet {flush_packet_idx}: Force set duration to {packet.duration} (tb={current_tb})")

                    # CRITICAL FIX: Ensure packet time_base matches stream time_base before muxing
                    # This prevents 0xc0000094 (Divide by Zero) crash in avformat
                    if packet.time_base != self._video_stream.time_base:
                        logger.debug(
                            f"Flush video packet {flush_packet_idx}: ⚠️ Timebase mismatch - "
                            f"packet: {packet.time_base}, stream: {self._video_stream.time_base}. Rescaling..."
                        )
                        if self._video_stream.time_base is None:
                            logger.warning(f"Flush video packet {flush_packet_idx}: Cannot rescale, stream timebase is None")
                        else:
                            # Manual conversion since PyAV 16.0.1 might lack rescale() or it failed
                            try:
                                if (packet.time_base and self._video_stream.time_base and 
                                    packet.time_base.denominator > 0 and 
                                    self._video_stream.time_base.denominator > 0 and
                                    packet.time_base.numerator > 0 and
                                    self._video_stream.time_base.numerator > 0):
                                    
                                    old_num = packet.time_base.numerator
                                    old_den = packet.time_base.denominator
                                    new_num = self._video_stream.time_base.numerator
                                    new_den = self._video_stream.time_base.denominator
                                    
                                    if packet.pts is not None:
                                        packet.pts = (packet.pts * old_num * new_den) // (old_den * new_num)
                                    
                                    if packet.dts is not None:
                                        packet.dts = (packet.dts * old_num * new_den) // (old_den * new_num)
                                        
                                    if packet.duration:
                                        packet.duration = (packet.duration * old_num * new_den) // (old_den * new_num)
                                        
                                    packet.time_base = self._video_stream.time_base
                                    logger.debug(f"Flush video packet {flush_packet_idx}: Manually converted packet - pts={packet.pts}, dts={packet.dts}, dur={packet.duration}, tb={packet.time_base}")
                                else:
                                    logger.warning(f"Flush video packet {flush_packet_idx}: Invalid timebase for manual conversion")

                            except Exception as e:
                                logger.error(f"Flush video packet {flush_packet_idx}: Manual conversion failed: {e}")
                    
                    # Ensure packet is associated with the correct stream
                    packet.stream = self._video_stream
                    
                    try:
                        self._output_container.mux(packet)
                        logger.debug(f"Flush video packet {flush_packet_idx}: ✅ Mux successful")
                    except Exception as e:
                        logger.critical(
                            f"Flush video packet {flush_packet_idx}: ❌ CRITICAL: Mux failed! "
                            f"pts={packet.pts}, dts={packet.dts}, tb={packet.time_base}, error={e}"
                        )
                        raise
                    flush_packet_idx += 1
            
            if self._audio_stream:
                flush_audio_packet_idx = 0
                for packet in self._audio_stream.encode():
                    logger.debug(
                        f"Flush audio packet {flush_audio_packet_idx}: "
                        f"pts={packet.pts}, dts={packet.dts}, "
                        f"packet_tb={packet.time_base}, stream_tb={self._audio_stream.time_base}"
                    )
                    
                    # Same fix for audio packets
                    if packet.time_base != self._audio_stream.time_base:
                        logger.debug(
                            f"Flush audio packet {flush_audio_packet_idx}: ⚠️ Timebase mismatch - "
                            f"packet: {packet.time_base}, stream: {self._audio_stream.time_base}. Rescaling..."
                        )
                        if self._audio_stream.time_base is None:
                            logger.warning(f"Flush audio packet {flush_audio_packet_idx}: Cannot rescale, stream timebase is None")
                        else:
                            try:
                                # Manual conversion for audio
                                if (packet.time_base and self._audio_stream.time_base and 
                                    packet.time_base.denominator > 0 and 
                                    self._audio_stream.time_base.denominator > 0 and
                                    packet.time_base.numerator > 0 and
                                    self._audio_stream.time_base.numerator > 0):
                                    
                                    old_num = packet.time_base.numerator
                                    old_den = packet.time_base.denominator
                                    new_num = self._audio_stream.time_base.numerator
                                    new_den = self._audio_stream.time_base.denominator
                                    
                                    if packet.pts is not None:
                                        packet.pts = (packet.pts * old_num * new_den) // (old_den * new_num)
                                    
                                    if packet.dts is not None:
                                        packet.dts = (packet.dts * old_num * new_den) // (old_den * new_num)
                                        
                                    if packet.duration:
                                        packet.duration = (packet.duration * old_num * new_den) // (old_den * new_num)
                                        
                                    packet.time_base = self._audio_stream.time_base
                                    logger.debug(f"Flush audio packet {flush_audio_packet_idx}: Manually converted packet - pts={packet.pts}, dts={packet.dts}, dur={packet.duration}, tb={packet.time_base}")
                                else:
                                    logger.warning(f"Flush audio packet {flush_audio_packet_idx}: Invalid timebase for manual conversion")
                            except Exception as e:
                                logger.error(f"Flush audio packet {flush_audio_packet_idx}: Manual conversion failed: {e}")

                    # Ensure packet is associated with the correct stream
                    packet.stream = self._audio_stream

                    try:
                        self._output_container.mux(packet)
                        logger.debug(f"Flush audio packet {flush_audio_packet_idx}: ✅ Mux successful")
                    except Exception as e:
                        logger.critical(
                            f"Flush audio packet {flush_audio_packet_idx}: ❌ CRITICAL: Mux failed! "
                            f"pts={packet.pts}, dts={packet.dts}, tb={packet.time_base}, error={e}"
                        )
                        raise
                    flush_audio_packet_idx += 1
            
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
                    # Flush the container before closing to ensure all data is written
                    # This is especially important for VP9 encoding
                    try:
                        self._output_container.flush()
                    except AttributeError:
                        # flush() might not be available in all PyAV versions
                        pass
                    self._output_container.close()
                    logger.info(f"Output container closed, temp file at: {self._output_file_path}")
                except Exception as e:
                    logger.error(f"Error closing output container: {e}", exc_info=True)
                self._output_container = None
            else:
                if self._output_file_path:
                    logger.debug(f"No container was initialized, temp file remains at: {self._output_file_path}")

    def _calculate_bitrate(self, width: int, height: int) -> int:
        """Calculate optimal bitrate based on resolution.
        
        Args:
            width: Video width in pixels.
            height: Video height in pixels.
            
        Returns:
            Optimal bitrate in bits per second.
        """
        if not self.auto_bitrate:
            return self.video_bitrate
        
        # Calculate pixels
        pixels = width * height
        
        # Bitrate recommendations based on resolution (bits per second per pixel * fps factor)
        # These are conservative estimates that work well for VP8/VP9
        # Adjust multipliers for different quality expectations
        
        # Base bitrate per megapixel at 30fps
        if pixels <= 640 * 480:  # VGA or smaller
            bitrate_per_megapixel = 2_000_000  # 2 Mbps per MP
        elif pixels <= 1280 * 720:  # 720p
            bitrate_per_megapixel = 3_000_000  # 3 Mbps per MP
        elif pixels <= 1920 * 1080:  # 1080p
            bitrate_per_megapixel = 5_000_000  # 5 Mbps per MP
        else:  # 1440p, 4K, etc.
            bitrate_per_megapixel = 8_000_000  # 8 Mbps per MP
        
        # Calculate base bitrate
        megapixels = pixels / 1_000_000
        calculated_bitrate = int(megapixels * bitrate_per_megapixel)
        
        # Apply quality multiplier
        quality_multipliers = {
            "low": 0.7,
            "medium": 1.0,
            "high": 1.5,
            "best": 2.0,
        }
        multiplier = quality_multipliers.get(self.video_quality, 1.0)
        calculated_bitrate = int(calculated_bitrate * multiplier)
        
        # Ensure minimum bitrate and use user's base bitrate as minimum
        min_bitrate = max(self.video_bitrate, 1_000_000)  # At least 1 Mbps
        return max(calculated_bitrate, min_bitrate)
    
    def _get_video_encoding_options(self, bitrate: int) -> dict[str, str]:
        """Get video encoding options based on codec and quality settings.
        
        Args:
            bitrate: Target bitrate in bits per second.
            
        Returns:
            Dictionary of encoding options for PyAV.
        """
        options: dict[str, str] = {
            "bitrate": str(bitrate),
        }
        
        if self.video_codec == "vp8":
            # VP8 quality/CPU tradeoff settings
            # cpu-used: 0-16, lower = better quality but slower
            cpu_used_map = {
                "low": "8",      # Fast encoding, lower quality
                "medium": "4",   # Balanced
                "high": "2",     # Slower encoding, better quality
                "best": "0",     # Slowest encoding, best quality
            }
            options["cpu-used"] = cpu_used_map.get(self.video_quality, "4")
            
            # Deadzone (noise sensitivity): 0-1000, lower = more sensitive to noise
            # Lower values preserve more detail but may introduce artifacts
            if self.video_quality in ("high", "best"):
                options["deadline"] = "goodquality"  # Better quality mode
                options["deadline_b"] = "600000"  # ~600ms per frame
            else:
                options["deadline"] = "realtime"  # Faster encoding
        
        elif self.video_codec == "vp9":
            # VP9 uses CRF (Constant Rate Factor) for quality-based encoding
            # CRF: 0-63, lower = better quality (0 = lossless, 31 = default, 63 = worst)
            crf_map = {
                "low": "45",     # Higher CRF = lower quality, smaller file
                "medium": "35",  # Default-like
                "high": "28",    # Better quality
                "best": "20",    # Very high quality
            }
            options["crf"] = crf_map.get(self.video_quality, "35")
            
            # CPU usage: 0-8, lower = better quality but slower
            cpu_used_map = {
                "low": "5",      # Faster encoding
                "medium": "3",   # Balanced
                "high": "1",     # Slower encoding, better quality
                "best": "0",     # Slowest encoding, best quality
            }
            options["cpu-used"] = cpu_used_map.get(self.video_quality, "3")
            
            # VP9 row-based multithreading (faster encoding)
            options["row-mt"] = "1"
            
            # For VP9, bitrate is used as max bitrate when CRF is set
            # The encoder will try to maintain quality while respecting bitrate limits
        
        return options

    def _encode_video_frame_incremental_sync(self, frame_event: VideoFrameEvent, frame_index: int, capture_time: float) -> None:
        """Encode a single video frame incrementally (synchronous) with timebase fix.
        
        CRITICAL FIX: Ensures packet time_base matches stream time_base before muxing
        to prevent 0xc0000094 (Divide by Zero) crash in avformat.
        """
        if not self._video_stream or not self._output_container:
            logger.warning(f"Frame {frame_index}: Missing stream or container - stream={self._video_stream}, container={self._output_container}")
            return
        
        # Log stream state
        if frame_index == 0 or frame_index % 100 == 0:
            logger.debug(
                f"Frame {frame_index}: Stream state - "
                f"stream_tb={self._video_stream.time_base}, "
                f"fps={self.video_fps}, codec={self.video_codec}"
            )
        
        # Calculate PTS
        # We calculate PTS in milliseconds (1/1000) to match the forced stream timebase.
        # We MUST also set pyav_frame.time_base = 1/1000 so the encoder knows these are ms, not frames.
        
        time_base_denominator = 1000
        time_base_numerator = 1
        
        stream_time_base = self._video_stream.time_base
        if stream_time_base:
            time_base_denominator = stream_time_base.denominator
            time_base_numerator = stream_time_base.numerator
            logger.debug(f"Using stream timebase for PTS calc: {stream_time_base}")
        else:
            logger.warning("Stream timebase missing for PTS calc, using 1/1000")

        # Use wall clock capture time for PTS to ensure sync with real-time recording
        # capture_time is system time.time() when frame was captured
        # start_time is system time.time() when recording started
        if self._start_time is not None:
            time_since_start_s = capture_time - self._start_time
            # Ensure non-negative
            if time_since_start_s < 0:
                 time_since_start_s = 0
            
            # Calculate PTS in stream timebase units
            # PTS = seconds * (den / num)
            pts = int(time_since_start_s * time_base_denominator / time_base_numerator)
            
            # Log drift between SDK timestamp and wall clock
            if self._first_video_frame_time is not None:
                sdk_duration = (frame_event.timestamp_us - self._first_video_frame_time) / 1_000_000
                drift = time_since_start_s - sdk_duration
                if abs(drift) > 0.1:  # Log significant drift
                    logger.debug(f"Frame {frame_index}: Time drift - Wall: {time_since_start_s:.3f}s, SDK: {sdk_duration:.3f}s, Drift: {drift:.3f}s")

            # Enforce strictly monotonic increasing timestamps
            if pts <= self._last_video_pts:
                 pts = self._last_video_pts + 1
        else:
            # Fallback if no timestamps: assume 30fps (33ms per frame)
            # Scale to stream timebase
            # 1 frame = 1/30 sec
            # PTS = index * (1/30) * (den / num)
            pts = (frame_index * time_base_denominator) // (30 * time_base_numerator)
        
        logger.debug(f"Calculated PTS: {pts} for frame {frame_index} (stream_tb={stream_time_base})")
        self._last_video_pts = pts
        
        # Convert and encode frame
        frame = frame_event.frame
        pyav_frame = self._convert_video_frame_to_pyav(frame, self._video_stream, pts)
        
        # Set time_base on the frame to match the stream
        if pyav_frame:
            if stream_time_base:
                pyav_frame.time_base = stream_time_base
            else:
                # Force 1/1000 if stream has no timebase (it will learn from frame)
                pyav_frame.time_base = Fraction(1, 1000)
        
        if pyav_frame and self._video_stream:
            # CRITICAL FIX: Ensure packet time_base matches stream time_base
            for packet_idx, packet in enumerate(self._video_stream.encode(pyav_frame)):
                # Log packet details before conversion
                logger.debug(
                    f"Frame {frame_index}, Packet {packet_idx}: "
                    f"pts={packet.pts}, dts={packet.dts}, "
                    f"packet_tb={packet.time_base}, stream_tb={self._video_stream.time_base}"
                )
                
                # Force set duration if missing (CRITICAL for 0xc0000094 prevention)
                if not packet.duration and self.video_fps > 0:
                    # Use packet's current timebase to calculate duration
                    current_tb = packet.time_base or self._video_stream.time_base
                    if current_tb and current_tb.numerator > 0:
                        tb_val = current_tb.numerator / current_tb.denominator
                        packet.duration = int(1 / (self.video_fps * tb_val))
                        logger.debug(f"Frame {frame_index}: Force set duration to {packet.duration} (tb={current_tb})")

                    # Fix timebase mismatch that causes 0xc0000094 crash
                    if packet.time_base != self._video_stream.time_base:
                        logger.debug(
                            f"Frame {frame_index}, Packet {packet_idx}: ⚠️ Timebase mismatch - "
                            f"packet: {packet.time_base}, stream: {self._video_stream.time_base}"
                        )
                        
                        # Manual conversion since PyAV 16.0.1 might lack rescale() or it failed
                        try:
                            if (packet.time_base and self._video_stream.time_base and 
                                packet.time_base.denominator > 0 and 
                                self._video_stream.time_base.denominator > 0 and
                                packet.time_base.numerator > 0 and
                                self._video_stream.time_base.numerator > 0):
                                
                                old_num = packet.time_base.numerator
                                old_den = packet.time_base.denominator
                                new_num = self._video_stream.time_base.numerator
                                new_den = self._video_stream.time_base.denominator
                                
                                if packet.pts is not None:
                                    # Standard rescaling formula: pts * (old_tb / new_tb)
                                    # Expanded: pts * (old_num / old_den) * (new_den / new_num)
                                    packet.pts = (packet.pts * old_num * new_den) // (old_den * new_num)
                                
                                if packet.dts is not None:
                                    packet.dts = (packet.dts * old_num * new_den) // (old_den * new_num)
                                    
                                if packet.duration:
                                    packet.duration = (packet.duration * old_num * new_den) // (old_den * new_num)
                                    
                                packet.time_base = self._video_stream.time_base
                                logger.debug(f"Frame {frame_index}: Manually converted packet - pts={packet.pts}, dts={packet.dts}, dur={packet.duration}, tb={packet.time_base}")
                            else:
                                logger.warning(f"Frame {frame_index}: Invalid timebase for manual conversion")

                        except Exception as e:
                            logger.error(f"Frame {frame_index}: Manual conversion failed: {e}")
                            # Fallback: trust PyAV/FFmpeg to handle it if manual fail
                            pass
                    
                    # CRITICAL: Re-verify duration after any conversion
                    # If duration became 0 during integer conversion (common for 1/30 -> 1/1000 conversion of small durations)
                    # we must restore it to at least 1 tick (1ms)
                    if not packet.duration or packet.duration <= 0:
                        if self.video_fps > 0:
                             packet.duration = int(1000 / self.video_fps) # e.g. 33ms for 30fps
                        else:
                             packet.duration = 33 # Default to 33ms
                        logger.debug(f"Frame {frame_index}: Restored duration to {packet.duration} after conversion")
                
                    # Ensure packet is associated with the correct stream
                    packet.stream = self._video_stream
                
                # Log before muxing
                logger.debug(
                    f"Frame {frame_index}, Packet {packet_idx}: Muxing packet - "
                    f"pts={packet.pts}, dts={packet.dts}, duration={packet.duration}, tb={packet.time_base}"
                )
                
                try:
                    self._output_container.mux(packet)
                    logger.debug(f"Frame {frame_index}, Packet {packet_idx}: ✅ Mux successful")
                except Exception as e:
                    logger.critical(
                        f"Frame {frame_index}, Packet {packet_idx}: ❌ CRITICAL: Mux failed! "
                        f"pts={packet.pts}, dts={packet.dts}, tb={packet.time_base}, error={e}"
                    )
                    raise
        
        # Release frame reference immediately
        del pyav_frame
        
        # Periodic GC
        if frame_index > 0 and frame_index % 50 == 0:
            gc.collect()

    async def _encode_video_frame_incremental(self, frame_event: VideoFrameEvent, frame_index: int, capture_time: float) -> None:
        """Encode a single video frame incrementally."""
        # Encoding is fast enough that we can do it directly in async context
        # PyAV encode operations are typically < 10ms per frame
        self._encode_video_frame_incremental_sync(frame_event, frame_index, capture_time)

    def _encode_audio_frame_incremental_sync(self, frame_event: AudioFrameEvent, capture_time: float) -> None:
        """Encode a single audio frame incrementally (synchronous)."""
        if not self._audio_stream or not self._output_container:
            return
        
        frame = frame_event.frame
        sample_rate = frame.sample_rate
        samples_per_channel = frame.samples_per_channel
        
        # Calculate Audio PTS based on Wall Clock Time
        # This ensures audio aligns with video and handles gaps (VFR) correctly
        # instead of "compressing" time by assuming continuous samples.
        
        # Use stream timebase (likely 1/sample_rate)
        time_base_denominator = sample_rate
        time_base_numerator = 1
        
        if self._audio_stream.time_base:
            time_base_denominator = self._audio_stream.time_base.denominator
            time_base_numerator = self._audio_stream.time_base.numerator
            
        if self._start_time is not None:
            time_since_start_s = capture_time - self._start_time
            if time_since_start_s < 0:
                time_since_start_s = 0
            
            # Calculate PTS: seconds * (den / num)
            audio_pts = int(time_since_start_s * time_base_denominator / time_base_numerator)
        else:
            # Fallback to cumulative samples if no start time (shouldn't happen)
            audio_pts = self._cumulative_audio_samples
        
        # Convert and encode frame
        pyav_frame = self._convert_audio_frame_to_pyav(frame, self._audio_stream, audio_pts)
        if pyav_frame and self._audio_stream:
            for packet_idx, packet in enumerate(self._audio_stream.encode(pyav_frame)):
                    # Fix timebase mismatch for audio packets too
                    if packet.time_base != self._audio_stream.time_base:
                        logger.debug(
                            f"Audio Frame, Packet {packet_idx}: ⚠️ Timebase mismatch - "
                            f"packet: {packet.time_base}, stream: {self._audio_stream.time_base}"
                        )
                        
                        if self._audio_stream.time_base is None:
                            logger.warning(f"Audio Frame, Packet {packet_idx}: Cannot rescale, stream timebase is None")
                        else:
                            try:
                                # Manual conversion for audio
                                if (packet.time_base and self._audio_stream.time_base and 
                                    packet.time_base.denominator > 0 and 
                                    self._audio_stream.time_base.denominator > 0 and
                                    packet.time_base.numerator > 0 and
                                    self._audio_stream.time_base.numerator > 0):
                                    
                                    old_num = packet.time_base.numerator
                                    old_den = packet.time_base.denominator
                                    new_num = self._audio_stream.time_base.numerator
                                    new_den = self._audio_stream.time_base.denominator
                                    
                                    if packet.pts is not None:
                                        packet.pts = (packet.pts * old_num * new_den) // (old_den * new_num)
                                    
                                    if packet.dts is not None:
                                        packet.dts = (packet.dts * old_num * new_den) // (old_den * new_num)
                                        
                                    if packet.duration:
                                        packet.duration = (packet.duration * old_num * new_den) // (old_den * new_num)
                                        
                                    packet.time_base = self._audio_stream.time_base
                                    logger.debug(f"Audio Frame, Packet {packet_idx}: Manually converted packet - pts={packet.pts}, dts={packet.dts}, dur={packet.duration}, tb={packet.time_base}")
                                else:
                                    logger.warning(f"Audio Frame, Packet {packet_idx}: Invalid timebase for manual conversion")
                            except Exception as e:
                                logger.error(f"Audio Frame, Packet {packet_idx}: Manual conversion failed: {e}")
                                pass

                    # Ensure packet is associated with the correct stream
                    packet.stream = self._audio_stream
                
                    try:
                        self._output_container.mux(packet)
                    except Exception as e:
                        logger.critical(
                            f"Audio mux failed: {e} - pts={packet.pts}, dts={packet.dts}, tb={packet.time_base}"
                        )
                        raise
            
            # Update cumulative samples for next frame
            self._cumulative_audio_samples += samples_per_channel
        
        # Release frame reference immediately
        del pyav_frame
        
        # Periodic GC (every ~4 seconds of audio)
        if self._cumulative_audio_samples % (sample_rate * 4) == 0:
            gc.collect()

    async def _encode_audio_frame_incremental(self, frame_event: AudioFrameEvent, capture_time: float) -> None:
        """Encode a single audio frame incrementally."""
        # Encoding is fast enough that we can do it directly in async context
        self._encode_audio_frame_incremental_sync(frame_event, capture_time)

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

            logger.info("Stopping recording... starting graceful drain.")

            # 1. Unsubscribe FIRST to stop new data from server
            if self._participant:
                await self._unsubscribe_from_participant_tracks(self._participant)

            # 2. Wait for buffers to drain (keep _is_recording=True during this time)
            # This allows the capture loops to pick up the final frames
            await asyncio.sleep(1.0)

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
            import time as time_module
            
            # Give a small delay to ensure file system writes are complete
            # This is especially important for large files or slow storage
            await asyncio.sleep(0.1)
            
            if self._output_file_path and os.path.exists(self._output_file_path):
                # Check if container was initialized (file should have content)
                file_size = os.path.getsize(self._output_file_path)
                logger.debug(f"Temp file exists: {self._output_file_path}, size: {file_size} bytes, container_initialized: {self._container_was_initialized}")
                
                if file_size > 0 or self._container_was_initialized:
                    # Double-check file size after a brief delay (for file system sync)
                    time_module.sleep(0.1)
                    final_size = os.path.getsize(self._output_file_path)
                    logger.info(f"Moving encoded file: {self._output_file_path} ({final_size} bytes) -> {output_path}")
                    
                    output_file_obj = Path(output_path).resolve()
                    output_file_obj.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(self._output_file_path, str(output_file_obj))
                    output_file = str(output_file_obj)
                    
                    # Verify the moved file exists and has content
                    if os.path.exists(output_file):
                        moved_size = os.path.getsize(output_file)
                        logger.info(f"Moved encoded file from {self._output_file_path} to {output_file} (size: {moved_size} bytes)")
                    else:
                        logger.error(f"File move failed: {output_file} does not exist after move")
                        raise RecordingError(f"Failed to move encoded file to {output_file}")
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
                logger.error(f"No output file was created. _output_file_path: {self._output_file_path}, container_initialized: {self._container_was_initialized}")
                if self._output_file_path:
                    logger.error(f"Temp file path was set but doesn't exist: {self._output_file_path}")
                    # Check if temp file exists in a different location
                    import glob
                    temp_pattern = os.path.join(os.path.dirname(self._output_file_path) if self._output_file_path else "/tmp", "livekit_recording_*.webm")
                    found_files = glob.glob(temp_pattern)
                    if found_files:
                        logger.info(f"Found potential temp files: {found_files}")
                        # Try using the most recent one
                        latest_file = max(found_files, key=os.path.getmtime)
                        file_size = os.path.getsize(latest_file)
                        if file_size > 0:
                            logger.info(f"Using found temp file: {latest_file} ({file_size} bytes)")
                            output_file_obj = Path(output_path).resolve()
                            output_file_obj.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(latest_file, str(output_file_obj))
                            output_file = str(output_file_obj)
                            logger.info(f"Moved found temp file to {output_file} (size: {file_size} bytes)")
                            return output_file
                
                output_file_obj = Path(output_path).resolve()
                output_file_obj.parent.mkdir(parents=True, exist_ok=True)
                output_file_obj.touch()
                output_file = str(output_file_obj)
                logger.warning("Created empty output file as fallback")

            # Clean up all references to allow proper resource release
            self._participant_identity = None
            self._participant = None
            self._subscribed_track_sids.clear()
            # Clear room reference to allow room to be disconnected
            self.room = None  # type: ignore

            logger.info(f"Recording saved to {output_file}")
            return output_file

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

        # Validate inputs before PyAV call
        if frame.width <= 0 or frame.height <= 0:
            logger.error(f"❌ Invalid frame dimensions: {frame.width}x{frame.height}")
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
            else:
                # Fallback: If stream has no timebase (yet), assume 1/1000 because
                # that's what we use for PTS calculation in _encode_video_frame_incremental_sync
                pyav_frame.time_base = Fraction(1, 1000)

            # Release converted frame reference if we created one
            # This helps GC free the conversion buffer sooner
            if converted_frame is not None:
                del converted_frame
            
            return pyav_frame
        except Exception as e:
            logger.critical(
                f"❌ Frame allocation failed: {frame.width}x{frame.height}, "
                f"PTS: {pts}, Error: {e}"
            )
            raise

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

    def debug_get_internal_state(self):
        return {
            "video_queue_size": self._video_queue.qsize(),
            "audio_queue_size": self._audio_queue.qsize(),
            "video_stream_init": self._video_stream_initialized,
            "frames_processed": self._stats.video_frames_recorded,
            "last_pts": getattr(self, '_last_processed_pts', None)
        }

    @property
    def is_recording(self) -> bool:
        """Check if recording is currently in progress."""
        return self._is_recording

