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

"""Unit tests for the recorder module."""

import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock, patch, call
from typing import Any
import pytest
import tempfile
import os
from pathlib import Path

from livekit import rtc
from livekit.rtc.recorder import (
    ParticipantRecorder,
    RecordingStats,
    RecordingError,
    ParticipantNotFoundError,
    TrackNotFoundError,
    WebMEncoderNotAvailableError,
)
from livekit.rtc.video_frame import VideoFrame
from livekit.rtc.audio_frame import AudioFrame
from livekit.rtc.video_stream import VideoFrameEvent
from livekit.rtc.audio_stream import AudioFrameEvent
from livekit.rtc._proto.track_pb2 import TrackKind
from livekit.rtc._proto.video_frame_pb2 import VideoBufferType


@pytest.fixture
def mock_room() -> Mock:
    """Create a mock Room instance."""
    room = Mock(spec=rtc.Room)
    room.remote_participants = {}
    return room


@pytest.fixture
def mock_participant() -> Mock:
    """Create a mock RemoteParticipant instance."""
    participant = Mock(spec=rtc.RemoteParticipant)
    participant.identity = "test_participant"
    participant.sid = "test_sid"
    participant.track_publications = {}
    return participant


@pytest.fixture
def mock_video_track() -> Mock:
    """Create a mock RemoteVideoTrack instance."""
    track = Mock(spec=rtc.RemoteVideoTrack)
    track.sid = "video_track_sid"
    track.kind = TrackKind.KIND_VIDEO
    track.name = "video_track"
    return track


@pytest.fixture
def mock_audio_track() -> Mock:
    """Create a mock RemoteAudioTrack instance."""
    track = Mock(spec=rtc.RemoteAudioTrack)
    track.sid = "audio_track_sid"
    track.kind = TrackKind.KIND_AUDIO
    track.name = "audio_track"
    return track


@pytest.fixture
def mock_video_publication(mock_video_track: Mock) -> Mock:
    """Create a mock RemoteTrackPublication for video."""
    publication = Mock(spec=rtc.RemoteTrackPublication)
    publication.sid = "video_track_sid"
    publication.kind = TrackKind.KIND_VIDEO
    publication.subscribed = False
    publication.track = None
    return publication


@pytest.fixture
def mock_audio_publication(mock_audio_track: Mock) -> Mock:
    """Create a mock RemoteTrackPublication for audio."""
    publication = Mock(spec=rtc.RemoteTrackPublication)
    publication.sid = "audio_track_sid"
    publication.kind = TrackKind.KIND_AUDIO
    publication.subscribed = False
    publication.track = None
    return publication


@pytest.fixture
def mock_video_frame() -> VideoFrame:
    """Create a mock VideoFrame instance."""
    # Create a minimal I420 frame (Y plane + U plane + V plane)
    width = 640
    height = 480
    y_size = width * height
    uv_size = (width // 2) * (height // 2)
    total_size = y_size + uv_size * 2
    data = bytearray(total_size)
    # Fill with test data
    for i in range(total_size):
        data[i] = i % 256
    return VideoFrame(width, height, VideoBufferType.I420, data)


@pytest.fixture
def mock_audio_frame() -> AudioFrame:
    """Create a mock AudioFrame instance."""
    sample_rate = 48000
    num_channels = 2
    samples_per_channel = 480  # 10ms at 48kHz
    data_size = num_channels * samples_per_channel * 2  # 2 bytes per int16
    data = bytearray(data_size)
    # Fill with test data
    for i in range(data_size // 2):
        value = (i % 32767) - 16383
        data[i * 2 : i * 2 + 2] = value.to_bytes(2, byteorder="little", signed=True)
    return AudioFrame(data, sample_rate, num_channels, samples_per_channel)


@pytest.mark.asyncio
async def test_recorder_init_success(mock_room: Mock) -> None:
    """Test successful recorder initialization."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        assert recorder.room == mock_room
        assert recorder.video_codec == "vp8"
        assert recorder.audio_codec == "opus"
        assert not recorder.is_recording


@pytest.mark.asyncio
async def test_recorder_init_without_av(mock_room: Mock) -> None:
    """Test recorder initialization without PyAV raises error."""
    with patch("livekit.rtc.recorder.HAS_AV", False):
        with pytest.raises(WebMEncoderNotAvailableError):
            ParticipantRecorder(mock_room)


@pytest.mark.asyncio
async def test_recorder_init_invalid_video_codec(mock_room: Mock) -> None:
    """Test recorder initialization with invalid video codec."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        with pytest.raises(ValueError, match="video_codec must be 'vp8' or 'vp9'"):
            ParticipantRecorder(mock_room, video_codec="h264")


@pytest.mark.asyncio
async def test_start_recording_participant_not_found(mock_room: Mock) -> None:
    """Test starting recording when participant is not found."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        mock_room.remote_participants = {}
        
        with pytest.raises(ParticipantNotFoundError):
            await recorder.start_recording("nonexistent_participant")


@pytest.mark.asyncio
async def test_start_recording_already_recording(
    mock_room: Mock, mock_participant: Mock
) -> None:
    """Test starting recording when already recording."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        mock_room.remote_participants = {"test_participant": mock_participant}
        mock_participant.track_publications = {}
        
        # Start recording first time
        await recorder.start_recording("test_participant")
        
        # Try to start again - should fail
        with pytest.raises(RecordingError, match="Recording is already in progress"):
            await recorder.start_recording("test_participant")


@pytest.mark.asyncio
async def test_stop_recording_not_started(mock_room: Mock) -> None:
    """Test stopping recording when not started."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        with pytest.raises(RecordingError, match="Recording is not in progress"):
            await recorder.stop_recording("output.webm")


@pytest.mark.asyncio
async def test_start_recording_subscribes_to_tracks(
    mock_room: Mock,
    mock_participant: Mock,
    mock_video_publication: Mock,
    mock_audio_publication: Mock,
) -> None:
    """Test that starting recording subscribes to participant tracks."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        mock_room.remote_participants = {"test_participant": mock_participant}
        mock_participant.track_publications = {
            "video_track_sid": mock_video_publication,
            "audio_track_sid": mock_audio_publication,
        }
        
        # Mock the async sleep and track capture
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch.object(recorder, "_wait_for_tracks_and_start_capture", new_callable=AsyncMock):
                await recorder.start_recording("test_participant")
                
                # Verify tracks were subscribed
                assert mock_video_publication.set_subscribed.called
                assert mock_audio_publication.set_subscribed.called
                assert recorder.is_recording


@pytest.mark.asyncio
async def test_capture_video_frames(
    mock_room: Mock,
    mock_participant: Mock,
    mock_video_track: Mock,
    mock_video_frame: VideoFrame,
) -> None:
    """Test capturing video frames."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        # Create a mock video stream
        mock_stream = AsyncMock(spec=rtc.VideoStream)
        frame_event = VideoFrameEvent(
            frame=mock_video_frame,
            timestamp_us=1000000,
            rotation=0,
        )
        mock_stream.__aiter__.return_value = [frame_event].__iter__()
        mock_stream.aclose = AsyncMock()
        
        recorder._video_stream = mock_stream
        
        # Run capture
        capture_task = asyncio.create_task(recorder._capture_video_frames())
        await asyncio.sleep(0.1)  # Allow frame to be captured
        recorder._is_recording = False  # Stop recording
        
        await capture_task
        
        # Verify frame was queued
        assert not recorder._video_queue.empty()
        queued_event = await recorder._video_queue.get()
        assert queued_event.frame == mock_video_frame


@pytest.mark.asyncio
async def test_capture_audio_frames(
    mock_room: Mock,
    mock_audio_frame: AudioFrame,
) -> None:
    """Test capturing audio frames."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        # Create a mock audio stream
        mock_stream = AsyncMock(spec=rtc.AudioStream)
        frame_event = AudioFrameEvent(frame=mock_audio_frame)
        mock_stream.__aiter__.return_value = [frame_event].__iter__()
        mock_stream.aclose = AsyncMock()
        
        recorder._audio_stream = mock_stream
        
        # Run capture
        capture_task = asyncio.create_task(recorder._capture_audio_frames())
        await asyncio.sleep(0.1)  # Allow frame to be captured
        recorder._is_recording = False  # Stop recording
        
        await capture_task
        
        # Verify frame was queued
        assert not recorder._audio_queue.empty()
        queued_event = await recorder._audio_queue.get()
        assert queued_event.frame == mock_audio_frame


@pytest.mark.asyncio
async def test_convert_video_frame_to_pyav(
    mock_room: Mock,
    mock_video_frame: VideoFrame,
) -> None:
    """Test converting VideoFrame to PyAV frame."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        # Mock PyAV components
        mock_stream = Mock()
        mock_stream.time_base = Mock(numerator=1, denominator=30)
        
        with patch("livekit.rtc.recorder.av") as mock_av:
            mock_av_frame = Mock()
            mock_av.VideoFrame.from_ndarray.return_value = mock_av_frame
            
            result = recorder._convert_video_frame_to_pyav(mock_video_frame, mock_stream, 0)
            
            assert result is not None
            assert mock_av.VideoFrame.from_ndarray.called
            assert result.width == mock_video_frame.width
            assert result.height == mock_video_frame.height


@pytest.mark.asyncio
async def test_convert_audio_frame_to_pyav(
    mock_room: Mock,
    mock_audio_frame: AudioFrame,
) -> None:
    """Test converting AudioFrame to PyAV frame."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        # Mock PyAV components
        mock_stream = Mock()
        mock_stream.time_base = Mock(numerator=1, denominator=48000)
        
        with patch("livekit.rtc.recorder.av") as mock_av:
            mock_av_frame = Mock()
            mock_av.AudioFrame.from_ndarray.return_value = mock_av_frame
            
            result = recorder._convert_audio_frame_to_pyav(mock_audio_frame, mock_stream, 0)
            
            assert result is not None
            assert mock_av.AudioFrame.from_ndarray.called
            assert result.sample_rate == mock_audio_frame.sample_rate


@pytest.mark.asyncio
async def test_get_stats(mock_room: Mock) -> None:
    """Test getting recording statistics."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        # Set some stats manually
        recorder._stats.video_frames_recorded = 100
        recorder._stats.audio_frames_recorded = 200
        recorder._stats.recording_duration_seconds = 10.5
        recorder._stats.output_file_size_bytes = 1024000
        
        stats = recorder.get_stats()
        
        assert stats.video_frames_recorded == 100
        assert stats.audio_frames_recorded == 200
        assert stats.recording_duration_seconds == 10.5
        assert stats.output_file_size_bytes == 1024000


@pytest.mark.asyncio
async def test_stop_recording_encodes_to_webm(
    mock_room: Mock,
    mock_participant: Mock,
    mock_video_frame: VideoFrame,
    mock_audio_frame: AudioFrame,
) -> None:
    """Test stopping recording encodes frames to WebM."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        mock_room.remote_participants = {"test_participant": mock_participant}
        mock_participant.track_publications = {}
        
        # Start recording (mock the async operations)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch.object(recorder, "_wait_for_tracks_and_start_capture", new_callable=AsyncMock):
                await recorder.start_recording("test_participant")
        
        # Add some frames to queues
        video_event = VideoFrameEvent(
            frame=mock_video_frame,
            timestamp_us=1000000,
            rotation=0,
        )
        audio_event = AudioFrameEvent(frame=mock_audio_frame)
        await recorder._video_queue.put(video_event)
        await recorder._audio_queue.put(audio_event)
        
        # Mock PyAV encoding
        with patch("livekit.rtc.recorder.av") as mock_av:
            mock_container = Mock()
            mock_av.open.return_value = mock_container
            
            mock_video_stream = Mock()
            mock_video_stream.time_base = Mock(numerator=1, denominator=30)
            mock_video_stream.encode.return_value = []
            mock_video_stream.rate = 30
            mock_video_stream.base_rate = 1
            
            mock_audio_stream = Mock()
            mock_audio_stream.time_base = Mock(numerator=1, denominator=48000)
            mock_audio_stream.encode.return_value = []
            
            mock_container.add_stream.side_effect = [mock_video_stream, mock_audio_stream]
            mock_container.close = Mock()
            
            # Create temporary file for output
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                output_path = tmp.name
            
            try:
                result_path = await recorder.stop_recording(output_path)
                
                assert result_path == output_path
                assert not recorder.is_recording
                assert mock_av.open.called
            finally:
                # Cleanup
                if os.path.exists(output_path):
                    os.remove(output_path)


@pytest.mark.asyncio
async def test_encode_to_webm_no_frames(mock_room: Mock, mock_participant: Mock) -> None:
    """Test encoding to WebM when no frames are available."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        mock_room.remote_participants = {"test_participant": mock_participant}
        mock_participant.track_publications = {}
        
        # Start recording
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch.object(recorder, "_wait_for_tracks_and_start_capture", new_callable=AsyncMock):
                await recorder.start_recording("test_participant")
        
        # Mock PyAV encoding with no frames
        with patch("livekit.rtc.recorder.av") as mock_av:
            mock_container = Mock()
            mock_av.open.return_value = mock_container
            mock_container.close = Mock()
            
            # Create temporary file
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                output_path = tmp.name
            
            try:
                result_path = await recorder.stop_recording(output_path)
                assert result_path == output_path
            finally:
                if os.path.exists(output_path):
                    os.remove(output_path)


@pytest.mark.asyncio
async def test_recorder_properties(mock_room: Mock) -> None:
    """Test recorder property accessors."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(
            mock_room,
            video_codec="vp9",
            audio_codec="opus",
            video_bitrate=4000000,
            audio_bitrate=256000,
            video_fps=60,
        )
        
        assert recorder.video_codec == "vp9"
        assert recorder.audio_codec == "opus"
        assert recorder.video_bitrate == 4000000
        assert recorder.audio_bitrate == 256000
        assert recorder.video_fps == 60
        assert not recorder.is_recording


def test_recording_stats_dataclass() -> None:
    """Test RecordingStats dataclass."""
    stats = RecordingStats(
        video_frames_recorded=50,
        audio_frames_recorded=100,
        recording_duration_seconds=5.0,
        output_file_size_bytes=512000,
    )
    
    assert stats.video_frames_recorded == 50
    assert stats.audio_frames_recorded == 100
    assert stats.recording_duration_seconds == 5.0
    assert stats.output_file_size_bytes == 512000


def test_error_classes() -> None:
    """Test that error classes are properly defined."""
    assert issubclass(RecordingError, Exception)
    assert issubclass(ParticipantNotFoundError, RecordingError)
    assert issubclass(TrackNotFoundError, RecordingError)
    assert issubclass(WebMEncoderNotAvailableError, RecordingError)


@pytest.mark.asyncio
async def test_start_capture_for_video_track(
    mock_room: Mock,
    mock_video_track: Mock,
    mock_video_publication: Mock,
) -> None:
    """Test starting capture for a video track."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        with patch("livekit.rtc.recorder.VideoStream") as mock_video_stream_class:
            mock_stream = Mock()
            mock_video_stream_class.return_value = mock_stream
            
            await recorder._start_capture_for_track(mock_video_track, mock_video_publication)
            
            assert recorder._video_stream is not None
            assert mock_video_stream_class.called


@pytest.mark.asyncio
async def test_start_capture_for_audio_track(
    mock_room: Mock,
    mock_audio_track: Mock,
    mock_audio_publication: Mock,
) -> None:
    """Test starting capture for an audio track."""
    with patch("livekit.rtc.recorder.HAS_AV", True):
        recorder = ParticipantRecorder(mock_room)
        
        with patch("livekit.rtc.recorder.AudioStream") as mock_audio_stream_class:
            mock_stream = Mock()
            mock_audio_stream_class.from_track.return_value = mock_stream
            
            await recorder._start_capture_for_track(mock_audio_track, mock_audio_publication)
            
            assert recorder._audio_stream is not None
            assert mock_audio_stream_class.from_track.called

