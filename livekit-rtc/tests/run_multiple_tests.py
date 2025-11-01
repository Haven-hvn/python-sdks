#!/usr/bin/env python3
"""Run integration tests multiple times for 60 seconds each until we get a good recording."""

import asyncio
import os
import sys
import time
import tempfile
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

# Add parent directory to path for direct imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-protocol"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-api"))

from test_recorder_integration import integration_test_recording

# Set environment
os.environ["LIVEKIT_USE_PUMPFUN"] = "true"
os.environ["LIVEKIT_RECORDING_DURATION"] = "60.0"

OUTPUT_DIR = Path(f"/tmp/livekit_recordings_{int(time.time())}")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


async def run_test_until_success(max_attempts: int = 20) -> None:
    """Run integration tests until we get a successful recording with both video and audio."""
    print("=" * 80)
    print("LiveKit ParticipantRecorder - Multiple Test Runner")
    print("=" * 80)
    print()
    print(f"Configuration:")
    print(f"  - Duration per test: 60 seconds")
    print(f"  - Output directory: {OUTPUT_DIR}")
    print(f"  - Maximum attempts: {max_attempts}")
    print(f"  - Looking for: Success with BOTH video and audio frames")
    print()
    
    attempt = 1
    best_result: Optional[Dict[str, Any]] = None
    best_score = 0
    
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
            
            # Score: prioritize tests with both video and audio
            score = 0
            if success:
                score += 10
            if video_frames > 0:
                score += 50
            if audio_frames > 0:
                score += 30
            if validation.get("valid"):
                score += 10
            
            print(f"\nResult Summary:")
            print(f"  Success: {success}")
            print(f"  Video frames: {video_frames}")
            print(f"  Audio frames: {audio_frames}")
            print(f"  Score: {score}")
            print()
            
            # Save the recording file
            if output_file and Path(output_file).exists():
                saved_file = OUTPUT_DIR / f"attempt_{attempt:03d}_recording.webm"
                shutil.copy2(output_file, saved_file)
                print(f"  Saved recording to: {saved_file}")
                
                # Track best result
                if score > best_score:
                    best_score = score
                    best_result = result.copy()
                    best_result["saved_file"] = str(saved_file)
                    best_result["attempt"] = attempt
            
            # Check if we have a good result (both video and audio)
            if success and video_frames > 0 and audio_frames > 0 and validation.get("valid"):
                print()
                print("=" * 80)
                print("✓ EXCELLENT RESULT FOUND!")
                print("=" * 80)
                print()
                print(f"Attempt: {attempt}")
                print(f"Video frames: {video_frames}")
                print(f"Audio frames: {audio_frames}")
                print(f"Output file: {saved_file}")
                print()
                print("Please validate the video quality:")
                print(f"  open '{saved_file}'")
                print(f"  or")
                print(f"  ffplay '{saved_file}'")
                print()
                break
            
            # Also check if we've had enough good attempts
            if attempt >= 5 and best_score >= 90:  # At least 5 attempts with good quality
                print()
                print("=" * 80)
                print(f"✓ GOOD RESULT FOUND (Score: {best_score})")
                print("=" * 80)
                print()
                print(f"Best attempt: {best_result.get('attempt', 'N/A')}")
                print(f"Best file: {best_result.get('saved_file', 'N/A')}")
                print()
                print("Please validate the video quality:")
                if best_result.get("saved_file"):
                    print(f"  open '{best_result['saved_file']}'")
                print()
                break
            
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
    print("Test Run Complete")
    print("=" * 80)
    print()
    print(f"Total attempts: {attempt - 1}")
    print(f"Best score: {best_score}")
    if best_result:
        print(f"Best attempt: {best_result.get('attempt', 'N/A')}")
        print(f"Best file: {best_result.get('saved_file', 'N/A')}")
    print(f"\nAll recordings saved to: {OUTPUT_DIR}")
    print()
    
    # List all recordings
    recordings = sorted(OUTPUT_DIR.glob("attempt_*_recording.webm"))
    if recordings:
        print("All recordings:")
        for rec in recordings:
            size = rec.stat().st_size
            print(f"  - {rec.name} ({size:,} bytes)")
        print()
    
    if best_result and best_result.get("saved_file"):
        print("Please validate the best recording:")
        print(f"  open '{best_result['saved_file']}'")
        print()


if __name__ == "__main__":
    asyncio.run(run_test_until_success(max_attempts=20))

