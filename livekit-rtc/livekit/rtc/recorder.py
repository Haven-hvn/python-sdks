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
from dataclasses import dataclass
from typing import Optional, Set
from pathlib import Path
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

        # Streams for video and audio
        self._video_stream: Optional[VideoStream] = None
        self._audio_stream: Optional[AudioStream] = None

        # Frame queues
        self._video_queue: asyncio.Queue[VideoFrameEvent] = asyncio.Queue()
        self._audio_queue: asyncio.Queue[AudioFrameEvent] = asyncio.Queue()

        # Background tasks for capturing frames
        self._video_capture_task: Optional[asyncio.Task[None]] = None
        self._audio_capture_task: Optional[asyncio.Task[None]] = None

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
            if self._video_stream is None:
                self._video_stream = VideoStream(track)
                self._video_capture_task = asyncio.create_task(
                    self._capture_video_frames()
                )
                logger.debug(f"Started video capture from track {track.sid}")

        elif track.kind == TrackKind.KIND_AUDIO and isinstance(
            track, RemoteAudioTrack
        ):
            if self._audio_stream is None:
                # Use Opus-compatible settings
                self._audio_stream = AudioStream.from_track(
                    track=track,
                    sample_rate=48000,  # Opus typically uses 48kHz
                    num_channels=2,  # Stereo
                )
                self._audio_capture_task = asyncio.create_task(
                    self._capture_audio_frames()
                )
                logger.debug(f"Started audio capture from track {track.sid}")

    async def _capture_video_frames(self) -> None:
        """Capture video frames from the video stream."""
        if not self._video_stream:
            return

        try:
            first_frame = True
            async for frame_event in self._video_stream:
                if not self._is_recording:
                    break
                # Store first frame timestamp as reference for synchronization
                if first_frame and self._start_time:
                    # Convert start_time (seconds) to microseconds for consistency
                    self._recording_start_time_us = int(self._start_time * 1_000_000)
                    first_frame = False
                await self._video_queue.put(frame_event)
                self._stats.video_frames_recorded += 1
        except Exception as e:
            logger.error(f"Error capturing video frames: {e}", exc_info=True)
        finally:
            if self._video_stream:
                await self._video_stream.aclose()
                self._video_stream = None

    async def _capture_audio_frames(self) -> None:
        """Capture audio frames from the audio stream."""
        if not self._audio_stream:
            return

        try:
            async for frame_event in self._audio_stream:
                if not self._is_recording:
                    break
                await self._audio_queue.put(frame_event)
                self._stats.audio_frames_recorded += 1
        except Exception as e:
            logger.error(f"Error capturing audio frames: {e}", exc_info=True)
        finally:
            if self._audio_stream:
                await self._audio_stream.aclose()
                self._audio_stream = None

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
            if self._video_stream:
                try:
                    await self._video_stream.aclose()
                except asyncio.CancelledError:
                    pass
                self._video_stream = None

            if self._audio_stream:
                try:
                    await self._audio_stream.aclose()
                except asyncio.CancelledError:
                    pass
                self._audio_stream = None

            # Encode to WebM
            output_file = await self._encode_to_webm(output_path)

            # Clean up all references to allow proper resource release
            self._participant_identity = None
            self._participant = None
            self._subscribed_track_sids.clear()
            # Clear room reference to allow room to be disconnected
            self.room = None  # type: ignore

            logger.info(f"Recording saved to {output_file}")
            return output_file

    async def _encode_to_webm(self, output_path: str) -> str:
        """Encode captured frames to a WebM file."""
        output_file = Path(output_path).resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Collect all frames before encoding
        video_frames: list[VideoFrameEvent] = []
        audio_frames: list[AudioFrameEvent] = []

        while True:
            try:
                frame_event = self._video_queue.get_nowait()
                video_frames.append(frame_event)
            except asyncio.QueueEmpty:
                break

        while True:
            try:
                frame_event = self._audio_queue.get_nowait()
                audio_frames.append(frame_event)
            except asyncio.QueueEmpty:
                break

        # Check if we have any frames to encode
        if not video_frames and not audio_frames:
            logger.warning(
                "No frames captured during recording. Creating empty file will be skipped."
            )
            # Create an empty file but mark it as such
            output_file.touch()
            return str(output_file)

        # Sort frames by timestamp to ensure proper synchronization order
        video_frames.sort(key=lambda e: e.timestamp_us)
        # Audio frames don't have timestamps, but we'll process them in order

        # Run encoding in a thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        absolute_path = await loop.run_in_executor(
            None,
            self._encode_to_webm_sync,
            str(output_file),
            video_frames,
            audio_frames,
        )

        # Clear frames from memory after encoding to prevent memory leaks
        del video_frames
        del audio_frames

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
                    
                    # Release frame reference to help GC
                    del pyav_frame

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
                    
                    # Release frame reference to help GC
                    del pyav_frame

            # Flush audio encoder
            if audio_stream:
                for packet in audio_stream.encode():
                    output_container.mux(packet)

        finally:
            output_container.close()
            # Explicitly clear frame lists to help GC
            video_frames.clear()
            audio_frames.clear()

        return output_path

    def _convert_video_frame_to_pyav(
        self,
        frame: VideoFrame,
        stream: Optional[av.VideoStream],
        pts: int,
    ) -> Optional[av.VideoFrame]:
        """Convert a VideoFrame to a PyAV VideoFrame."""
        if not stream:
            return None

        try:
            import numpy as np

            # Convert frame to I420 format if needed
            if frame.type != VideoBufferType.I420:
                frame = frame.convert(VideoBufferType.I420)

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
            import numpy as np
            
            # Calculate strides
            y_stride = len(y_plane) // frame.height
            pyav_y_stride = pyav_frame.planes[0].line_size
            chroma_height = frame.height // 2
            u_stride = len(u_plane) // chroma_height if chroma_height > 0 else 0
            v_stride = len(v_plane) // chroma_height if chroma_height > 0 else 0
            pyav_u_stride = pyav_frame.planes[1].line_size
            pyav_v_stride = pyav_frame.planes[2].line_size
            
            # Copy Y plane row by row, handling stride differences
            y_np = np.frombuffer(y_plane, dtype=np.uint8)
            y_data = y_np.reshape(frame.height, y_stride) if y_stride > 0 else y_np.reshape(frame.height, -1)
            pyav_y_data = bytearray(pyav_frame.planes[0].buffer_size)
            for row in range(frame.height):
                src_start = row * y_stride
                dst_start = row * pyav_y_stride
                copy_len = min(frame.width, min(y_stride, pyav_y_stride))
                pyav_y_data[dst_start:dst_start + copy_len] = y_data[row, :copy_len].tobytes()
            pyav_frame.planes[0].update(bytes(pyav_y_data))
            
            # Copy U plane row by row
            u_np = np.frombuffer(u_plane, dtype=np.uint8)
            chroma_width = frame.width // 2
            u_data = u_np.reshape(chroma_height, u_stride) if u_stride > 0 else u_np.reshape(chroma_height, -1)
            pyav_u_data = bytearray(pyav_frame.planes[1].buffer_size)
            for row in range(chroma_height):
                src_start = row * u_stride
                dst_start = row * pyav_u_stride
                copy_len = min(chroma_width, min(u_stride, pyav_u_stride))
                pyav_u_data[dst_start:dst_start + copy_len] = u_data[row, :copy_len].tobytes()
            pyav_frame.planes[1].update(bytes(pyav_u_data))
            
            # Copy V plane row by row
            v_np = np.frombuffer(v_plane, dtype=np.uint8)
            v_data = v_np.reshape(chroma_height, v_stride) if v_stride > 0 else v_np.reshape(chroma_height, -1)
            pyav_v_data = bytearray(pyav_frame.planes[2].buffer_size)
            for row in range(chroma_height):
                src_start = row * v_stride
                dst_start = row * pyav_v_stride
                copy_len = min(chroma_width, min(v_stride, pyav_v_stride))
                pyav_v_data[dst_start:dst_start + copy_len] = v_data[row, :copy_len].tobytes()
            pyav_frame.planes[2].update(bytes(pyav_v_data))
            
            pyav_frame.pts = pts
            # Set time_base only if stream has one and it's valid
            if stream and hasattr(stream, 'time_base') and stream.time_base is not None:
                try:
                    pyav_frame.time_base = stream.time_base
                except (AttributeError, ValueError, TypeError) as e:
                    logger.warning(f"Could not set time_base: {e}")
                    # Continue without time_base - PyAV may infer it

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

