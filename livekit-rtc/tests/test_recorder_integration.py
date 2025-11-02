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

"""Integration test for ParticipantRecorder with live streams.

This test connects to an actual LiveKit room and records from a live participant
stream. It validates the recorded output to ensure the recording implementation is correct.

Usage:
    # Set environment variables
    export LIVEKIT_URL=wss://your-livekit-server.com
    export LIVEKIT_API_KEY=your-api-key
    export LIVEKIT_API_SECRET=your-api-secret
    
    # Run the test
    python -m pytest tests/test_recorder_integration.py -v -s
    
    # Or run directly
    python tests/test_recorder_integration.py
"""

import asyncio
import os
import sys
import time
import tempfile
import logging
from pathlib import Path
from typing import Optional, Any, Dict

# Add parent directory to path for direct imports
sys.path.insert(0, str(Path(__file__).parent.parent))
# Add livekit-api and livekit-protocol directories to path for API imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-protocol"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-api"))

# Direct imports to avoid version conflicts with pip-installed livekit
from livekit import rtc
from livekit import api as livekit_api
from livekit.rtc.recorder import (
    ParticipantRecorder,
    RecordingStats,
    ParticipantNotFoundError,
    RecordingError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Import pump.fun service for automatic stream discovery
try:
    from .pumpfun_service import PumpFunService
    HAS_PUMPFUN = True
except ImportError:
    try:
        from pumpfun_service import PumpFunService
        HAS_PUMPFUN = True
    except ImportError:
        HAS_PUMPFUN = False
        logger.warning("PumpFunService not available. Install httpx for automatic stream discovery.")

# For WebM validation
try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False
    print("WARNING: PyAV not available. WebM validation will be skipped.")


class RecordingValidator:
    """Validates recorded WebM files."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path

    def validate(self) -> dict[str, Any]:
        """Validate the recorded WebM file.

        Returns:
            Dictionary with validation results.
        """
        if not HAS_AV:
            return {"valid": False, "error": "PyAV not available"}

        if not os.path.exists(self.file_path):
            return {"valid": False, "error": "File does not exist"}

        file_size = os.path.getsize(self.file_path)
        if file_size == 0:
            return {"valid": False, "error": "File is empty"}

        try:
            container = av.open(self.file_path)
            streams_info = []

            for stream in container.streams:
                stream_info = {
                    "type": str(stream.type),
                    "codec_name": stream.codec.name if stream.codec else None,
                    "duration": float(stream.duration * stream.time_base) if stream.duration else None,
                    "bitrate": stream.bitrate if hasattr(stream, "bitrate") else None,
                }

                if stream.type == "video":
                    # Get framerate from codec_context or calculate from average_rate
                    framerate = None
                    if hasattr(stream, "average_rate") and stream.average_rate:
                        framerate = float(stream.average_rate)
                    elif hasattr(stream.codec_context, "framerate") and stream.codec_context.framerate:
                        rate = stream.codec_context.framerate
                        framerate = float(rate.numerator) / float(rate.denominator) if rate.denominator else None
                    
                    # Get pixel format from codec context or first frame
                    pixel_format = None
                    if hasattr(stream.codec_context, "pix_fmt") and stream.codec_context.pix_fmt:
                        pixel_format = str(stream.codec_context.pix_fmt)
                    
                    # Get time base for PTS calculation validation
                    time_base = None
                    if stream.time_base:
                        time_base = {
                            "numerator": stream.time_base.numerator,
                            "denominator": stream.time_base.denominator,
                            "value": float(stream.time_base),
                        }
                    
                    # Calculate expected duration from frame count and frame rate
                    # This helps validate PTS calculation accuracy
                    calculated_duration = None
                    if framerate and framerate > 0:
                        frame_counts = self._count_frames()
                        video_frame_count = frame_counts.get("video_frames", 0)
                        if video_frame_count > 0:
                            calculated_duration = video_frame_count / framerate
                    
                    stream_info.update({
                        "width": stream.width,
                        "height": stream.height,
                        "framerate": framerate,
                        "pixel_format": pixel_format,
                        "time_base": time_base,
                        "calculated_duration_from_frames": calculated_duration,
                    })

                if stream.type == "audio":
                    # Determine audio layout
                    layout = None
                    if stream.channels == 1:
                        layout = "mono"
                    elif stream.channels == 2:
                        layout = "stereo"
                    else:
                        layout = f"{stream.channels}ch"
                    
                    # Get sample format if available
                    sample_format = None
                    if hasattr(stream.codec_context, "sample_fmt") and stream.codec_context.sample_fmt:
                        sample_format = str(stream.codec_context.sample_fmt)
                    
                    # Get time base for audio PTS calculation validation
                    audio_time_base = None
                    if stream.time_base:
                        audio_time_base = {
                            "numerator": stream.time_base.numerator,
                            "denominator": stream.time_base.denominator,
                            "value": float(stream.time_base),
                        }
                    elif stream.rate and stream.rate > 0:
                        # Audio time_base is typically 1/sample_rate
                        audio_time_base = {
                            "numerator": 1,
                            "denominator": stream.rate,
                            "value": 1.0 / stream.rate,
                        }
                    
                    stream_info.update({
                        "sample_rate": stream.rate,
                        "channels": stream.channels,
                        "layout": layout,
                        "sample_format": sample_format,
                        "time_base": audio_time_base,
                    })

                streams_info.append(stream_info)

            container.close()

            # Count frames by seeking through the file
            frame_counts = self._count_frames()

            # Calculate timing validation metrics
            timing_validation = {}
            video_stream = next((s for s in streams_info if s.get("type") == "video"), None)
            audio_stream = next((s for s in streams_info if s.get("type") == "audio"), None)
            
            if video_stream:
                container_duration = video_stream.get("duration")
                calculated_duration = video_stream.get("calculated_duration_from_frames")
                framerate = video_stream.get("framerate")
                
                timing_validation["video"] = {
                    "container_duration": container_duration,
                    "calculated_duration_from_frames": calculated_duration,
                    "framerate": framerate,
                    "duration_match": None,
                }
                
                # Check if container duration matches calculated duration
                if container_duration and calculated_duration:
                    duration_diff = abs(container_duration - calculated_duration)
                    duration_tolerance = 0.1  # 100ms tolerance
                    timing_validation["video"]["duration_match"] = duration_diff < duration_tolerance
                    timing_validation["video"]["duration_diff_seconds"] = duration_diff
            
            return {
                "valid": True,
                "file_size_bytes": file_size,
                "streams": streams_info,
                "frame_counts": frame_counts,
                "timing_validation": timing_validation,
            }
        except Exception as e:
            return {"valid": False, "error": str(e), "exception": e}

    def _count_frames(self) -> dict[str, int]:
        """Count frames in the video file."""
        if not HAS_AV:
            return {}

        try:
            container = av.open(self.file_path)
            video_frames = 0
            audio_frames = 0

            for packet in container.demux():
                for frame in packet.decode():
                    if isinstance(frame, av.VideoFrame):
                        video_frames += 1
                    elif isinstance(frame, av.AudioFrame):
                        audio_frames += 1

            container.close()

            return {
                "video_frames": video_frames,
                "audio_frames": audio_frames,
            }
        except Exception as e:
            logger.warning(f"Error counting frames: {e}")
            return {}


async def wait_for_participant(
    room: rtc.Room,
    participant_identity: Optional[str] = None,
    timeout: float = 30.0,
) -> rtc.RemoteParticipant:
    """Wait for a remote participant to join the room.

    Args:
        room: The LiveKit room.
        participant_identity: Optional specific participant identity to wait for.
        timeout: Maximum time to wait in seconds.

    Returns:
        The remote participant.

    Raises:
        TimeoutError: If no participant joins within the timeout.
    """
    start_time = time.time()
    participant_event = asyncio.Event()
    found_participant: Optional[rtc.RemoteParticipant] = None

    def on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        nonlocal found_participant
        if participant_identity is None or participant.identity == participant_identity:
            found_participant = participant
            if not participant_event.is_set():
                participant_event.set()

    room.on("participant_connected", on_participant_connected)

    # Check if participant already exists
    if participant_identity:
        if participant_identity in room.remote_participants:
            found_participant = room.remote_participants[participant_identity]
            participant_event.set()
    else:
        if room.remote_participants:
            found_participant = list(room.remote_participants.values())[0]
            participant_event.set()

    # Wait for participant if not already found
    if found_participant is None:
        try:
            await asyncio.wait_for(participant_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"No participant joined within {timeout} seconds"
            )

    if found_participant is None:
        raise ValueError("Participant not found")

    # Wait a bit for tracks to be published
    await asyncio.sleep(2.0)

    return found_participant


async def wait_for_tracks(
    participant: rtc.RemoteParticipant,
    timeout: float = 10.0,
) -> tuple[bool, bool]:
    """Wait for participant to publish video and/or audio tracks.

    Args:
        participant: The remote participant.
        timeout: Maximum time to wait in seconds.

    Returns:
        Tuple of (has_video, has_audio).
    """
    start_time = time.time()
    has_video = False
    has_audio = False

    # Check existing tracks
    for publication in participant.track_publications.values():
        if publication.kind == rtc.TrackKind.KIND_VIDEO:
            has_video = True
        elif publication.kind == rtc.TrackKind.KIND_AUDIO:
            has_audio = True

    if has_video and has_audio:
        return (has_video, has_audio)

    # Wait for tracks to be published
    track_event = asyncio.Event()

    def on_track_published(
        publication: rtc.RemoteTrackPublication,
        p: rtc.RemoteParticipant,
    ) -> None:
        nonlocal has_video, has_audio
        if p.identity != participant.identity:
            return

        if publication.kind == rtc.TrackKind.KIND_VIDEO:
            has_video = True
        elif publication.kind == rtc.TrackKind.KIND_AUDIO:
            has_audio = True

        if has_video or has_audio:
            track_event.set()

    # Note: In a real scenario, you'd register this on the room
    # For testing, we'll poll
    while time.time() - start_time < timeout:
        for publication in participant.track_publications.values():
            if publication.kind == rtc.TrackKind.KIND_VIDEO:
                has_video = True
            elif publication.kind == rtc.TrackKind.KIND_AUDIO:
                has_audio = True

        if has_video or has_audio:
            break

        await asyncio.sleep(0.5)

    return (has_video, has_audio)


async def integration_test_recording() -> dict[str, Any]:
    """Run integration test for recording.

    Automatically discovers a live stream from pump.fun if no credentials provided,
    otherwise uses the provided LiveKit credentials.

    Returns:
        Dictionary with test results.
    """
    # Check if using pump.fun or custom LiveKit server
    use_pumpfun = os.getenv("LIVEKIT_USE_PUMPFUN", "true").lower() == "true"
    
    livekit_url = os.getenv("LIVEKIT_URL")
    livekit_api_key = os.getenv("LIVEKIT_API_KEY")
    livekit_api_secret = os.getenv("LIVEKIT_API_SECRET")
    room_name = os.getenv("LIVEKIT_ROOM_NAME")
    participant_identity = os.getenv("LIVEKIT_PARTICIPANT_IDENTITY")  # Optional
    recording_duration = float(os.getenv("LIVEKIT_RECORDING_DURATION", "5.0"))

    # Use pump.fun if enabled and no custom credentials provided
    pumpfun_service: Optional[PumpFunService] = None
    stream_info: Optional[Dict[str, Any]] = None
    mint_id: Optional[str] = None
    use_token_directly = False
    custom_token: Optional[str] = None

    if (use_pumpfun and HAS_PUMPFUN and 
        (not livekit_url or not livekit_api_key or not livekit_api_secret)):
        logger.info("Using pump.fun for automatic stream discovery...")
        try:
            pumpfun_service = PumpFunService()
            livekit_url = pumpfun_service.get_livekit_url()
            
            # Get a random live stream
            stream_info = await pumpfun_service.get_random_live_stream(
                min_participants=1,  # At least 1 participant (the streamer)
                exclude_nsfw=True,  # Exclude NSFW for testing
            )
            
            if not stream_info:
                return {
                    "success": False,
                    "error": "No live streams found on pump.fun. Try again later.",
                    "source": "pumpfun",
                }
            
            mint_id = stream_info.get("mint")
            if not mint_id:
                return {
                    "success": False,
                    "error": "Selected stream has no mint ID",
                    "source": "pumpfun",
                }
            
            # Get token for the stream
            logger.info(f"Getting token for stream: {stream_info.get('name')} ({mint_id})")
            token = await pumpfun_service.get_livestream_token(mint_id, role="viewer")
            
            if not token:
                return {
                    "success": False,
                    "error": f"Failed to get token for stream {mint_id}",
                    "source": "pumpfun",
                    "mint_id": mint_id,
                }
            
            logger.info(f"Successfully obtained token for pump.fun stream")
            results = {
                "success": False,
                "source": "pumpfun",
                "stream_info": {
                    "mint_id": mint_id,
                    "name": stream_info.get("name"),
                    "symbol": stream_info.get("symbol"),
                    "participants": stream_info.get("num_participants", 0),
                },
                "errors": [],
                "warnings": [],
            }
            
            # Use the token directly (it's already a JWT)
            use_token_directly = True
            custom_token = token
            
        except Exception as e:
            logger.error(f"Error setting up pump.fun: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to set up pump.fun service: {e}",
                "source": "pumpfun",
            }
    else:
        # Use custom LiveKit server
        if not livekit_url or not livekit_api_key or not livekit_api_secret:
            return {
                "success": False,
                "error": (
                    "Missing required environment variables: "
                    "LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET. "
                    "Or set LIVEKIT_USE_PUMPFUN=false and provide custom credentials."
                ),
            }
        
        use_token_directly = False
        custom_token = None
        results = {
            "success": False,
            "source": "custom",
            "errors": [],
            "warnings": [],
        }

    if not HAS_AV:
        return {
            "success": False,
            "error": "PyAV is required for recording. Install with: pip install av",
        }

    results = {
        "success": False,
        "room_name": room_name,
        "participant_identity": participant_identity,
        "recording_duration_seconds": recording_duration,
        "errors": [],
        "warnings": [],
    }

    room = rtc.Room()
    recorder: Optional[ParticipantRecorder] = None
    output_file: Optional[str] = None

    try:
        # Create access token
        if use_token_directly:
            # Use the token from pump.fun directly
            token = custom_token
            logger.info("Using pump.fun provided token")
        else:
            # Create token for custom LiveKit server
            token = (
                livekit_api.AccessToken(livekit_api_key, livekit_api_secret)
                .with_identity("recorder-test-agent")
                .with_name("Recording Test Agent")
                .with_grants(
                    livekit_api.VideoGrants(
                        room_join=True,
                        room=room_name or "test-recording-room",
                        room_record=True,
                    )
                )
                .to_jwt()
            )

        logger.info(f"Connecting to room: {room_name or 'pump.fun stream'}")
        try:
            # Add timeout to prevent hanging indefinitely
            await asyncio.wait_for(
                room.connect(livekit_url, token),
                timeout=30.0  # 30 second timeout for connection
            )
        except asyncio.TimeoutError:
            error_msg = "Room connection timed out after 30 seconds - FFI server may be in bad state"
            logger.error(error_msg)
            raise rtc.ConnectError(error_msg)
        except Exception as e:
            logger.error(f"Error connecting to room: {e}")
            raise
        
        results["connected"] = True
        results["room_name"] = room.name

        logger.info("Waiting for participant to join...")
        participant = await wait_for_participant(
            room,
            participant_identity=participant_identity,
            timeout=30.0,
        )
        results["participant_found"] = True
        results["participant_identity"] = participant.identity
        results["participant_sid"] = participant.sid

        logger.info(f"Participant found: {participant.identity}")

        # Wait for tracks with timeout to prevent hanging
        try:
            has_video, has_audio = await asyncio.wait_for(
                wait_for_tracks(participant, timeout=20.0),
                timeout=25.0  # Overall timeout including wait_for_tracks internal timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for tracks, proceeding with no tracks")
            has_video = False
            has_audio = False
        results["has_video"] = has_video
        results["has_audio"] = has_audio

        if not has_video and not has_audio:
            results["warnings"].append("No video or audio tracks found")

        logger.info(f"Tracks available - Video: {has_video}, Audio: {has_audio}")

        # Create recorder
        # Use VP9 with best quality for highest video quality
        recorder = ParticipantRecorder(
            room,
            video_codec="vp9",
            video_quality="best",
            auto_bitrate=True,
        )
        results["recorder_created"] = True

        # Start recording with timeout
        logger.info(f"Starting recording for {recording_duration} seconds...")
        try:
            await asyncio.wait_for(
                recorder.start_recording(participant.identity),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            raise RecordingError("Timeout starting recording - participant may have disconnected")
        results["recording_started"] = True
        results["recording_start_time"] = time.time()

        # Record for specified duration
        await asyncio.sleep(recording_duration)

        # Get stats during recording
        stats = recorder.get_stats()
        results["stats_during_recording"] = {
            "video_frames_recorded": stats.video_frames_recorded,
            "audio_frames_recorded": stats.audio_frames_recorded,
            "recording_duration_seconds": stats.recording_duration_seconds,
        }

        logger.info(f"Recording stats: {results['stats_during_recording']}")

        # Stop recording and save
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            output_file = tmp.name

        logger.info(f"Stopping recording and saving to: {output_file}")
        saved_path = await recorder.stop_recording(output_file)
        results["recording_stopped"] = True
        results["output_file"] = saved_path

        # Get final stats
        final_stats = recorder.get_stats()
        results["final_stats"] = {
            "video_frames_recorded": final_stats.video_frames_recorded,
            "audio_frames_recorded": final_stats.audio_frames_recorded,
            "recording_duration_seconds": final_stats.recording_duration_seconds,
            "output_file_size_bytes": final_stats.output_file_size_bytes,
        }

        logger.info(f"Final stats: {results['final_stats']}")

        # Validate output file
        logger.info("Validating recorded file...")
        validator = RecordingValidator(saved_path)
        validation_result = validator.validate()
        results["validation"] = validation_result

        if validation_result.get("valid"):
            logger.info("✓ Recording validation passed")
            logger.info(f"  File size: {validation_result.get('file_size_bytes')} bytes")
            logger.info(f"  Streams: {len(validation_result.get('streams', []))}")

            for stream_info in validation_result.get("streams", []):
                stream_type = stream_info.get("type")
                codec = stream_info.get("codec_name")
                logger.info(f"  - {stream_type} stream: {codec}")

            frame_counts = validation_result.get("frame_counts", {})
            if frame_counts:
                logger.info(f"  - Video frames: {frame_counts.get('video_frames', 0)}")
                logger.info(f"  - Audio frames: {frame_counts.get('audio_frames', 0)}")
        else:
            logger.error(f"✗ Recording validation failed: {validation_result.get('error')}")
            results["errors"].append(f"Validation failed: {validation_result.get('error')}")

        # Basic checks
        if final_stats.video_frames_recorded == 0 and has_video:
            results["warnings"].append("No video frames were recorded despite video track being available")

        if final_stats.audio_frames_recorded == 0 and has_audio:
            results["warnings"].append("No audio frames were recorded despite audio track being available")

        if final_stats.output_file_size_bytes == 0:
            results["errors"].append("Output file is empty")

        results["success"] = (
            len(results["errors"]) == 0
            and validation_result.get("valid", False)
            and final_stats.output_file_size_bytes > 0
        )

    except rtc.ConnectError as e:
        error_msg = f"Connection error: {e}"
        logger.error(error_msg)
        results["errors"].append(error_msg)
        results["success"] = False
    except ParticipantNotFoundError as e:
        error_msg = f"Participant not found: {e}"
        logger.error(error_msg)
        results["errors"].append(error_msg)
        results["success"] = False
    except RecordingError as e:
        error_msg = f"Recording error: {e}"
        logger.error(error_msg)
        results["errors"].append(error_msg)
        results["success"] = False
    except TimeoutError as e:
        error_msg = f"Timeout: {e}"
        logger.error(error_msg)
        results["errors"].append(error_msg)
        results["success"] = False
    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg, exc_info=True)
        results["errors"].append(error_msg)
        results["success"] = False
    finally:
        # Cleanup - ensure all resources are released
        if recorder:
            try:
                if recorder.is_recording:
                    await recorder.stop_recording(output_file or "cleanup.webm")
            except Exception:
                pass
            finally:
                # Explicitly clear recorder reference
                recorder = None

        if room:
            try:
                # Wait for disconnect to complete fully with timeout
                await asyncio.wait_for(room.disconnect(), timeout=5.0)
                # Give async cleanup a moment to complete
                await asyncio.sleep(0.2)
            except asyncio.TimeoutError:
                logger.warning("Room disconnect timed out, forcing cleanup")
            except Exception as e:
                logger.warning(f"Error during room disconnect: {e}")
            finally:
                # Explicitly clear room reference
                room = None

        if pumpfun_service:
            try:
                await pumpfun_service.close()
            except Exception:
                pass
            finally:
                # Explicitly clear pumpfun_service reference
                pumpfun_service = None

        # Force garbage collection to ensure all references are released
        import gc
        gc.collect()

    return results


async def main() -> None:
    """Main entry point for running the integration test."""
    print("=" * 80)
    print("LiveKit ParticipantRecorder Integration Test")
    print("=" * 80)
    print()

    results = await integration_test_recording()

    print()
    print("=" * 80)
    print("Test Results")
    print("=" * 80)
    print(f"Success: {results.get('success', False)}")
    print(f"Source: {results.get('source', 'unknown')}")
    
    if results.get('stream_info'):
        stream_info = results['stream_info']
        print(f"Stream: {stream_info.get('name', 'Unknown')} ({stream_info.get('symbol', 'N/A')})")
        print(f"  Mint ID: {stream_info.get('mint_id')}")
        print(f"  Participants: {stream_info.get('participants', 0)}")
    
    print(f"Room: {results.get('room_name', 'N/A')}")
    print(f"Participant: {results.get('participant_identity', 'N/A')}")

    if results.get("errors"):
        print("\nErrors:")
        for error in results["errors"]:
            print(f"  - {error}")

    if results.get("warnings"):
        print("\nWarnings:")
        for warning in results["warnings"]:
            print(f"  - {warning}")

    if results.get("final_stats"):
        stats = results["final_stats"]
        print("\nFinal Statistics:")
        print(f"  Video frames: {stats.get('video_frames_recorded', 0)}")
        print(f"  Audio frames: {stats.get('audio_frames_recorded', 0)}")
        print(f"  Duration: {stats.get('recording_duration_seconds', 0):.2f}s")
        print(f"  File size: {stats.get('output_file_size_bytes', 0)} bytes")

    if results.get("validation"):
        validation = results["validation"]
        if validation.get("valid"):
            print("\nValidation: PASSED")
            print(f"  File size: {validation.get('file_size_bytes')} bytes")
            print(f"  Streams: {len(validation.get('streams', []))}")
            for stream in validation.get("streams", []):
                stream_type = stream.get("type")
                codec = stream.get("codec_name")
                print(f"    - {stream_type}: {codec}")
        else:
            print("\nValidation: FAILED")
            print(f"  Error: {validation.get('error')}")

    if results.get("output_file"):
        print(f"\nOutput file: {results['output_file']}")

    print()
    print("=" * 80)

    # Exit with appropriate code
    sys.exit(0 if results.get("success") else 1)


# Pytest-compatible test function
def test_recorder_integration() -> None:
    """Pytest-compatible integration test."""
    results = asyncio.run(integration_test_recording())
    assert results.get("success", False), f"Integration test failed: {results.get('errors', [])}"


if __name__ == "__main__":
    asyncio.run(main())

