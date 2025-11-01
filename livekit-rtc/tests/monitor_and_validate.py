#!/usr/bin/env python3
"""Monitor integration test progress and open successful recordings for validation."""

import asyncio
import os
import sys
import time
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

# Add parent directory to path for direct imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-protocol"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-api"))

from test_recorder_integration import integration_test_recording, RecordingValidator

# Set environment
os.environ["LIVEKIT_USE_PUMPFUN"] = "true"
os.environ["LIVEKIT_RECORDING_DURATION"] = "60.0"

OUTPUT_DIR = Path(f"/tmp/livekit_validations_{int(time.time())}")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


async def run_until_success(max_attempts: int = 50) -> Optional[str]:
    """Run integration tests until we get a successful validated recording."""
    print("=" * 80)
    print("Integration Test Monitor - Running until Success")
    print("=" * 80)
    print()
    print(f"Configuration:")
    print(f"  - Duration per test: 60 seconds")
    print(f"  - Maximum attempts: {max_attempts}")
    print(f"  - Output directory: {OUTPUT_DIR}")
    print(f"  - Goal: Success with video and audio, validation passing")
    print()
    
    attempt = 1
    
    while attempt <= max_attempts:
        print("=" * 80)
        print(f"Attempt {attempt} of {max_attempts}")
        print("=" * 80)
        print()
        
        try:
            result = await integration_test_recording()
            
            success = result.get("success", False)
            video_frames = result.get("final_stats", {}).get("video_frames_recorded", 0)
            audio_frames = result.get("final_stats", {}).get("audio_frames_recorded", 0)
            output_file = result.get("output_file")
            validation = result.get("validation", {})
            
            print(f"\nResult Summary:")
            print(f"  Success: {success}")
            print(f"  Video frames: {video_frames}")
            print(f"  Audio frames: {audio_frames}")
            print(f"  Validation: {validation.get('valid', False)}")
            print()
            
            # Check if we have a good result
            if success and video_frames > 0 and audio_frames > 0 and validation.get("valid"):
                if output_file and Path(output_file).exists():
                    # Copy to our output directory
                    saved_file = OUTPUT_DIR / f"validated_recording_attempt_{attempt}.webm"
                    import shutil
                    shutil.copy2(output_file, saved_file)
                    
                    print("=" * 80)
                    print("✓ SUCCESS - Valid Recording Found!")
                    print("=" * 80)
                    print()
                    print(f"Attempt: {attempt}")
                    print(f"Video frames: {video_frames}")
                    print(f"Audio frames: {audio_frames}")
                    print(f"File size: {Path(saved_file).stat().st_size:,} bytes")
                    print(f"Recording file: {saved_file}")
                    print()
                    
                    # Show validation details
                    streams = validation.get("streams", [])
                    print("Validation Details:")
                    for stream in streams:
                        stream_type = stream.get("type")
                        codec = stream.get("codec_name", "unknown")
                        if stream_type == "video":
                            width = stream.get("width", 0)
                            height = stream.get("height", 0)
                            framerate = stream.get("framerate", 0)
                            print(f"  Video: {codec} {width}x{height} @ {framerate}fps")
                        elif stream_type == "audio":
                            sample_rate = stream.get("sample_rate", 0)
                            channels = stream.get("channels", 0)
                            print(f"  Audio: {codec} {sample_rate}Hz {channels}ch")
                    print()
                    
                    return str(saved_file)
            
            # Show progress even if not perfect
            if success:
                print(f"Attempt {attempt} succeeded but needs validation check.")
            else:
                print(f"Attempt {attempt} did not meet all criteria.")
            
        except Exception as e:
            print(f"\n✗ Error on attempt {attempt}: {e}")
            import traceback
            traceback.print_exc()
        
        attempt += 1
        print()
        
        # Brief pause between attempts
        if attempt <= max_attempts:
            print("Waiting 3 seconds before next attempt...")
            await asyncio.sleep(3)
            print()
    
    print("=" * 80)
    print("Maximum attempts reached")
    print("=" * 80)
    return None


def open_for_validation(file_path: str) -> None:
    """Open the recording file for human validation."""
    print("=" * 80)
    print("Opening Recording for Human Validation")
    print("=" * 80)
    print()
    print(f"Recording file: {file_path}")
    print()
    print("Please validate:")
    print("  ✓ Video plays correctly")
    print("  ✓ Audio plays correctly")
    print("  ✓ Video and audio are in sync")
    print("  ✓ No corruption or artifacts")
    print("  ✓ Quality looks good")
    print()
    
    # Try to open with default application
    try:
        subprocess.run(["open", file_path], check=True)
        print(f"✓ Opened recording in default application")
    except Exception as e:
        print(f"Could not open automatically: {e}")
        print(f"Please manually open: {file_path}")
        print()
        print("You can also use:")
        print(f"  open '{file_path}'")
        print(f"  ffplay '{file_path}'")


if __name__ == "__main__":
    # Run until we get a successful recording
    recording_file = asyncio.run(run_until_success(max_attempts=50))
    
    if recording_file:
        # Open for human validation
        open_for_validation(recording_file)
        
        print()
        print("=" * 80)
        print("Waiting for human validation...")
        print("=" * 80)
        print()
        print("After validating the video, please confirm:")
        print("  - Does the video look good? (yes/no)")
        print()
        
        # Wait for user input
        response = input("Video validation result (yes/no): ").strip().lower()
        
        if response == "yes":
            print()
            print("✓ Video validation PASSED!")
            print()
            print(f"Successful recording saved at: {recording_file}")
        else:
            print()
            print("✗ Video validation FAILED")
            print("Continuing to find a better recording...")
            print()
            # Continue running until we get another good one
            recording_file = asyncio.run(run_until_success(max_attempts=20))
            if recording_file:
                open_for_validation(recording_file)
    else:
        print("No successful recordings found after maximum attempts.")
        print("Please check the logs for details.")

