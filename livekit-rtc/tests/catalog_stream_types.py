#!/usr/bin/env python3
"""Catalog stream types by running integration tests on multiple random streams.

This script runs multiple 60-second recordings from different streams and
documents each unique stream type combination found (audio format, video format, etc.).
"""

import asyncio
import os
import sys
import time
import json
import tempfile
import shutil
import gc
from pathlib import Path
from typing import Optional, Dict, Any, Set
from dataclasses import dataclass, asdict
from datetime import datetime
from collections import defaultdict

# Add parent directory to path for direct imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-protocol"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-api"))

from test_recorder_integration import integration_test_recording, RecordingValidator

# Set environment
os.environ["LIVEKIT_USE_PUMPFUN"] = "true"
os.environ["LIVEKIT_RECORDING_DURATION"] = "30.0"  # Reduced to 30s to avoid encoding bottlenecks

OUTPUT_DIR = Path(f"/tmp/livekit_stream_catalog_{int(time.time())}")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TYPES_DIR = OUTPUT_DIR / "stream_types"
TYPES_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class AudioStreamType:
    """Represents an audio stream type configuration."""
    sample_rate: int
    channels: int
    codec: str
    layout: str  # mono, stereo, etc.
    
    @property
    def type_id(self) -> str:
        """Unique identifier for this audio type."""
        return f"audio_{self.codec}_{self.sample_rate}Hz_{self.channels}ch_{self.layout}"


@dataclass
class VideoStreamType:
    """Represents a video stream type configuration."""
    width: int
    height: int
    pixel_format: str  # yuv420p, etc.
    framerate: float
    codec: str
    
    @property
    def type_id(self) -> str:
        """Unique identifier for this video type."""
        return f"video_{self.codec}_{self.width}x{self.height}_{self.pixel_format}_{int(self.framerate)}fps"


@dataclass
class StreamTypeCombination:
    """Represents a unique combination of audio and video stream types."""
    audio: Optional[AudioStreamType]
    video: Optional[VideoStreamType]
    stream_count: int
    
    @property
    def type_id(self) -> str:
        """Unique identifier for this combination."""
        audio_id = self.audio.type_id if self.audio else "no_audio"
        video_id = self.video.type_id if self.video else "no_video"
        return f"{audio_id}__{video_id}"


class StreamTypeCatalog:
    """Catalogs stream types found during testing."""
    
    def __init__(self) -> None:
        self.found_types: Dict[str, StreamTypeCombination] = {}
        self.type_samples: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
        self.total_tests = 0
        self.successful_tests = 0
        
    def add_recording(
        self,
        result: Dict[str, Any],
        recording_file: str,
    ) -> Optional[StreamTypeCombination]:
        """Analyze a recording and add its type to the catalog.
        
        Returns the StreamTypeCombination if successfully analyzed.
        """
        validation = result.get("validation", {})
        if not validation.get("valid"):
            return None
        
        streams_info = validation.get("streams", [])
        if not streams_info:
            return None
        
        # Extract audio stream type
        audio_type: Optional[AudioStreamType] = None
        video_type: Optional[VideoStreamType] = None
        
        for stream_info in streams_info:
            stream_type = stream_info.get("type")
            codec = stream_info.get("codec_name", "unknown")
            
            if stream_type == "audio":
                sample_rate = stream_info.get("sample_rate", 0)
                channels = stream_info.get("channels", 0)
                
                # Get layout from validation results if available, otherwise determine
                layout = stream_info.get("layout")
                if not layout:
                    if channels == 1:
                        layout = "mono"
                    elif channels == 2:
                        layout = "stereo"
                    else:
                        layout = f"{channels}ch"
                
                audio_type = AudioStreamType(
                    sample_rate=sample_rate,
                    channels=channels,
                    codec=codec,
                    layout=layout,
                )
            
            elif stream_type == "video":
                width = stream_info.get("width", 0)
                height = stream_info.get("height", 0)
                framerate = stream_info.get("framerate", 0.0) or 0.0
                
                # Get pixel format from validation results
                pixel_format = stream_info.get("pixel_format", "yuv420p")  # Default for VP8/VP9 in WebM
                if not pixel_format:
                    pixel_format = "yuv420p"  # Fallback default
                
                video_type = VideoStreamType(
                    width=width,
                    height=height,
                    pixel_format=pixel_format,
                    framerate=framerate,
                    codec=codec,
                )
        
        # Create combination
        combination = StreamTypeCombination(
            audio=audio_type,
            video=video_type,
            stream_count=len(streams_info),
        )
        
        type_id = combination.type_id
        
        # Add to catalog
        if type_id not in self.found_types:
            self.found_types[type_id] = combination
        
        # Extract timing/PTS information
        timing_info = {}
        validation = result.get("validation", {})
        timing_validation = validation.get("timing_validation", {})
        
        if timing_validation.get("video"):
            video_timing = timing_validation["video"]
            timing_info["video"] = {
                "container_duration": video_timing.get("container_duration"),
                "calculated_duration": video_timing.get("calculated_duration_from_frames"),
                "duration_match": video_timing.get("duration_match"),
                "duration_diff_seconds": video_timing.get("duration_diff_seconds"),
                "framerate": video_timing.get("framerate"),
            }
        
        # Get time_base info from streams
        streams_info = validation.get("streams", [])
        for stream_info in streams_info:
            if stream_info.get("type") == "video":
                timing_info["video_time_base"] = stream_info.get("time_base")
            elif stream_info.get("type") == "audio":
                timing_info["audio_time_base"] = stream_info.get("time_base")
        
        # Add sample recording info with timing data
        self.type_samples[type_id].append({
            "recording_file": recording_file,
            "video_frames": result.get("final_stats", {}).get("video_frames_recorded", 0),
            "audio_frames": result.get("final_stats", {}).get("audio_frames_recorded", 0),
            "file_size": result.get("final_stats", {}).get("output_file_size_bytes", 0),
            "duration": result.get("final_stats", {}).get("recording_duration_seconds", 0),
            "timing_validation": timing_info,
            "stream_info": result.get("stream_info", {}),
            "participant": result.get("participant_identity", "unknown"),
            "timestamp": datetime.now().isoformat(),
        })
        
        return combination
    
    def save_documentation(self) -> None:
        """Save documentation for all found stream types."""
        catalog_file = OUTPUT_DIR / "stream_types_catalog.json"
        
        catalog_data = {
            "metadata": {
                "total_tests": self.total_tests,
                "successful_tests": self.successful_tests,
                "unique_types_found": len(self.found_types),
                "generated_at": datetime.now().isoformat(),
            },
            "stream_types": {}
        }
        
        # Document each type
        for type_id, combination in sorted(self.found_types.items()):
            samples = self.type_samples[type_id]
            
            type_doc = {
                "type_id": type_id,
                "audio": asdict(combination.audio) if combination.audio else None,
                "video": asdict(combination.video) if combination.video else None,
                "stream_count": combination.stream_count,
                "sample_count": len(samples),
                "samples": samples,
            }
            
            catalog_data["stream_types"][type_id] = type_doc
            
            # Save individual type document
            type_file = TYPES_DIR / f"{type_id}.json"
            with open(type_file, "w") as f:
                json.dump(type_doc, f, indent=2)
            
            # Create markdown documentation
            self._create_type_markdown(type_id, type_doc)
        
        # Save full catalog
        with open(catalog_file, "w") as f:
            json.dump(catalog_data, f, indent=2)
        
        # Create summary markdown
        self._create_summary_markdown(catalog_data)
        
        print(f"\n✓ Catalog saved to: {OUTPUT_DIR}")
        print(f"  - Full catalog: {catalog_file}")
        print(f"  - Individual types: {TYPES_DIR}")
        print(f"  - Summary: {OUTPUT_DIR / 'STREAM_TYPES_SUMMARY.md'}")
    
    def _create_type_markdown(self, type_id: str, type_doc: Dict[str, Any]) -> None:
        """Create markdown documentation for a stream type."""
        md_file = TYPES_DIR / f"{type_id}.md"
        
        lines = [
            f"# Stream Type: {type_id}",
            "",
            "## Configuration",
            "",
        ]
        
        if type_doc["audio"]:
            audio = type_doc["audio"]
            lines.extend([
                "### Audio Stream",
                f"- **Codec**: {audio['codec']}",
                f"- **Sample Rate**: {audio['sample_rate']} Hz",
                f"- **Channels**: {audio['channels']} ({audio['layout']})",
                "",
            ])
        else:
            lines.extend([
                "### Audio Stream",
                "- **Status**: No audio stream",
                "",
            ])
        
        if type_doc["video"]:
            video = type_doc["video"]
            lines.extend([
                "### Video Stream",
                f"- **Codec**: {video['codec']}",
                f"- **Resolution**: {video['width']}x{video['height']}",
                f"- **Pixel Format**: {video['pixel_format']}",
                f"- **Frame Rate**: {video['framerate']} fps",
                "",
            ])
        
        # Add timing/PTS validation information
        if type_doc.get("samples"):
            sample = type_doc["samples"][0]  # Use first sample
            timing = sample.get("timing_validation", {})
            
            if timing:
                lines.extend([
                    "## Timing/PTS Validation",
                    "",
                ])
                
                if timing.get("video"):
                    video_timing = timing["video"]
                    lines.extend([
                        "### Video Timing",
                    ])
                    
                    container_dur = video_timing.get("container_duration")
                    calculated_dur = video_timing.get("calculated_duration")
                    duration_match = video_timing.get("duration_match")
                    framerate = video_timing.get("framerate")
                    
                    if container_dur is not None:
                        lines.append(f"- **Container Duration**: {container_dur:.3f} seconds")
                    if calculated_dur is not None:
                        lines.append(f"- **Calculated Duration** (from frames): {calculated_dur:.3f} seconds")
                        lines.append(f"  - Formula: `video_frames / framerate`")
                        if framerate:
                            lines.append(f"  - Example: For 900 frames @ {framerate} fps = {900/framerate:.3f} seconds")
                    if duration_match is not None:
                        match_status = "✓ PASS" if duration_match else "✗ FAIL"
                        lines.append(f"- **Duration Match**: {match_status}")
                        if video_timing.get("duration_diff_seconds") is not None:
                            diff = video_timing["duration_diff_seconds"]
                            lines.append(f"  - Difference: {diff:.3f} seconds")
                    
                    lines.append("")
                
                if timing.get("video_time_base"):
                    time_base = timing["video_time_base"]
                    lines.extend([
                        "### Video Time Base (PTS Calculation)",
                        f"- **Time Base**: {time_base.get('numerator')}/{time_base.get('denominator')} = {time_base.get('value'):.6f}",
                    ])
                    
                    framerate = type_doc["video"].get("framerate") if type_doc.get("video") else None
                    if framerate and time_base.get("denominator"):
                        # Calculate frame_interval as used in recorder
                        # frame_interval = time_base_denominator / (fps * time_base_numerator)
                        denom = time_base.get("denominator")
                        numer = time_base.get("numerator")
                        frame_interval = int(denom / (framerate * numer))
                        
                        lines.extend([
                        f"- **Frame Interval** (PTS units per frame): {frame_interval}",
                        f"  - Formula: `time_base_denominator / (fps * time_base_numerator)`",
                        f"  - For {time_base.get('denominator')}/{time_base.get('numerator')} @ {framerate} fps: {frame_interval}",
                        "",
                        "**PTS Calculation Method:**",
                        f"- Frame 0: PTS = 0",
                        f"- Frame 30: PTS = {30 * frame_interval} (~1 second at {framerate} fps)",
                        f"- Frame {int(30 * framerate)}: PTS = {int(30 * framerate) * frame_interval} (~30 seconds)",
                        "",
                        "This ensures:",
                        "- ✓ Correct duration (matches recording time)",
                        "- ✓ Proper frame timing based on frame rate",
                        "- ✓ Synchronized playback",
                        "",
                    ])
                
                if timing.get("audio_time_base"):
                    audio_tb = timing["audio_time_base"]
                    lines.extend([
                        "### Audio Time Base (PTS Calculation)",
                        f"- **Time Base**: {audio_tb.get('numerator')}/{audio_tb.get('denominator')} = {audio_tb.get('value'):.6f}",
                        f"  - Typically 1/sample_rate for audio streams",
                        "",
                    ])
        
        lines.extend([
            "## Samples",
            "",
            f"Found {type_doc['sample_count']} sample(s) of this stream type:",
            "",
        ])
        
        for i, sample in enumerate(type_doc["samples"], 1):
            lines.extend([
                f"### Sample {i}",
                f"- **Recording File**: `{sample['recording_file']}`",
                f"- **Video Frames**: {sample['video_frames']}",
                f"- **Audio Frames**: {sample['audio_frames']}",
                f"- **File Size**: {sample['file_size']:,} bytes",
                f"- **Duration**: {sample['duration']:.2f} seconds",
                f"- **Participant**: {sample['participant']}",
                f"- **Timestamp**: {sample['timestamp']}",
                "",
            ])
        
        with open(md_file, "w") as f:
            f.write("\n".join(lines))
    
    def _create_summary_markdown(self, catalog_data: Dict[str, Any]) -> None:
        """Create summary markdown documentation."""
        md_file = OUTPUT_DIR / "STREAM_TYPES_SUMMARY.md"
        
        metadata = catalog_data["metadata"]
        stream_types = catalog_data["stream_types"]
        
        lines = [
            "# LiveKit Stream Types Catalog",
            "",
            f"Generated: {metadata['generated_at']}",
            "",
            "## Summary",
            "",
            f"- **Total Tests Run**: {metadata['total_tests']}",
            f"- **Successful Tests**: {metadata['successful_tests']}",
            f"- **Unique Stream Types Found**: {metadata['unique_types_found']}",
            "",
            "## Stream Types",
            "",
        ]
        
        for type_id, type_doc in sorted(stream_types.items()):
            audio = type_doc["audio"]
            video = type_doc["video"]
            
            lines.append(f"### {type_id}")
            lines.append("")
            
            if audio:
                lines.append(f"- **Audio**: {audio['codec']} @ {audio['sample_rate']}Hz, {audio['channels']}ch ({audio['layout']})")
            else:
                lines.append("- **Audio**: None")
            
            if video:
                lines.append(f"- **Video**: {video['codec']} {video['width']}x{video['height']} @ {video['framerate']}fps ({video['pixel_format']})")
            else:
                lines.append("- **Video**: None")
            
            lines.append(f"- **Samples**: {type_doc['sample_count']}")
            lines.append(f"- **Details**: See [`{type_id}.md`](stream_types/{type_id}.md)")
            lines.append("")
        
        lines.extend([
            "## Recommendations",
            "",
            "Based on the collected stream types:",
            "",
            "1. Ensure the recorder handles all found audio configurations:",
        ])
        
        # Collect unique audio configs
        audio_configs: Set[str] = set()
        for type_doc in stream_types.values():
            if type_doc["audio"]:
                audio = type_doc["audio"]
                audio_configs.add(f"{audio['channels']}ch @ {audio['sample_rate']}Hz")
        
        for config in sorted(audio_configs):
            lines.append(f"   - {config}")
        
        lines.extend([
            "",
            "2. Ensure the recorder handles all found video configurations:",
        ])
        
        # Collect unique video configs
        video_configs: Set[str] = set()
        for type_doc in stream_types.values():
            if type_doc["video"]:
                video = type_doc["video"]
                video_configs.add(f"{video['width']}x{video['height']} @ {video['framerate']}fps")
        
        for config in sorted(video_configs):
            lines.append(f"   - {config}")
        
        lines.append("")
        
        with open(md_file, "w") as f:
            f.write("\n".join(lines))


async def run_catalog_tests(max_attempts: int = 30) -> None:
    """Run multiple integration tests and catalog all stream types found."""
    print("=" * 80)
    print("LiveKit Stream Types Catalog")
    print("=" * 80)
    print()
    print(f"Configuration:")
    print(f"  - Duration per test: 30 seconds (reduced for faster encoding)")
    print(f"  - Maximum attempts: {max_attempts}")
    print(f"  - Output directory: {OUTPUT_DIR}")
    print(f"  - Goal: Catalog all unique stream type combinations")
    print()
    
    catalog = StreamTypeCatalog()
    attempt = 1
    last_success = 0
    
    while attempt <= max_attempts:
        print("=" * 80)
        print(f"Attempt {attempt} of {max_attempts}")
        print("=" * 80)
        print()
        
        catalog.total_tests += 1
        
        try:
            result = await integration_test_recording()
            
            success = result.get("success", False)
            output_file = result.get("output_file")
            
            if success and output_file and Path(output_file).exists():
                catalog.successful_tests += 1
                last_success = attempt
                
                # Save recording with type identifier
                saved_file = OUTPUT_DIR / f"recording_{attempt:03d}.webm"
                shutil.copy2(output_file, saved_file)
                
                # Analyze and catalog
                combination = catalog.add_recording(result, str(saved_file))
                
                if combination:
                    print(f"\n✓ Test {attempt} SUCCESS - Stream type: {combination.type_id}")
                    print(f"  - Audio: {combination.audio.type_id if combination.audio else 'none'}")
                    print(f"  - Video: {combination.video.type_id if combination.video else 'none'}")
                    print(f"  - Saved to: {saved_file}")
                    
                    # Show stats
                    stats = result.get("final_stats", {})
                    print(f"  - Video frames: {stats.get('video_frames_recorded', 0)}")
                    print(f"  - Audio frames: {stats.get('audio_frames_recorded', 0)}")
                    print(f"  - File size: {stats.get('output_file_size_bytes', 0):,} bytes")
                else:
                    print(f"\n✓ Test {attempt} SUCCESS but type analysis failed")
            else:
                print(f"\n✗ Test {attempt} FAILED")
                if output_file:
                    print(f"  Output file: {output_file}")
                errors = result.get("errors", [])
                if errors:
                    print(f"  Errors: {errors[0] if errors else 'Unknown'}")
        
        except Exception as e:
            print(f"\n✗ Test {attempt} ERROR: {e}")
            import traceback
            traceback.print_exc()
        
        attempt += 1
        
        # Force cleanup - release all references and collect garbage
        result = None  # Release reference to result
        
        # Give async cleanup more time to complete (FFI server needs time to fully release resources)
        await asyncio.sleep(1.0)
        
        # Force garbage collection to release memory from frames and connections
        gc.collect()
        
        # Additional wait to ensure FFI resources are fully released
        await asyncio.sleep(0.5)
        
        # Show progress
        if attempt <= max_attempts:
            print()
            print(f"Progress: {catalog.successful_tests}/{catalog.total_tests} successful, "
                  f"{len(catalog.found_types)} unique types found")
            print("Waiting 3 seconds before next attempt...")
            await asyncio.sleep(3)
            print()
    
    print()
    print("=" * 80)
    print("Catalog Complete")
    print("=" * 80)
    print()
    print(f"Total tests: {catalog.total_tests}")
    print(f"Successful: {catalog.successful_tests}")
    print(f"Unique stream types found: {len(catalog.found_types)}")
    print()
    
    if catalog.found_types:
        print("Found stream types:")
        for type_id in sorted(catalog.found_types.keys()):
            sample_count = len(catalog.type_samples[type_id])
            print(f"  - {type_id} ({sample_count} sample(s))")
        print()
    
    # Save documentation
    catalog.save_documentation()
    
    # Show best recordings for human validation
    if catalog.found_types:
        print("=" * 80)
        print("Recordings for Human Validation")
        print("=" * 80)
        print()
        print("Please validate one recording from each stream type:")
        print()
        
        for type_id, combination in sorted(catalog.found_types.items()):
            samples = catalog.type_samples[type_id]
            if samples:
                best_sample = max(samples, key=lambda s: s["video_frames"] + s["audio_frames"])
                recording_file = best_sample["recording_file"]
                print(f"Type: {type_id}")
                print(f"  Recording: {recording_file}")
                print(f"  Video frames: {best_sample['video_frames']}")
                print(f"  Audio frames: {best_sample['audio_frames']}")
                print(f"  To validate: open '{recording_file}'")
                print()


if __name__ == "__main__":
    asyncio.run(run_catalog_tests(max_attempts=30))

