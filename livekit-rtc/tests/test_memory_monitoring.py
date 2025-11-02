#!/usr/bin/env python3
"""Memory monitoring test for ParticipantRecorder.

This test runs a 20-minute recording session and monitors memory usage every 60 seconds.
It helps identify memory leaks or memory growth issues during long recordings.
"""

import asyncio
import os
import sys
import time
import tempfile
import logging
import json
import subprocess
import gc
import tracemalloc
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict

# Try to import psutil, fall back to system commands if not available
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# Add parent directory to path for direct imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-protocol"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "livekit-api"))

from livekit import rtc
from livekit import api as livekit_api
from livekit.rtc.recorder import (
    ParticipantRecorder,
    ParticipantNotFoundError,
    RecordingError,
)

# Import integration test utilities
try:
    from .test_recorder_integration import wait_for_participant, wait_for_tracks
    from .pumpfun_service import PumpFunService
    HAS_PUMPFUN = True
except ImportError:
    try:
        from test_recorder_integration import wait_for_participant, wait_for_tracks
        from pumpfun_service import PumpFunService
        HAS_PUMPFUN = True
    except ImportError:
        HAS_PUMPFUN = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class MemorySnapshot:
    """Memory usage snapshot at a specific time."""
    timestamp: float
    elapsed_seconds: float
    rss_mb: float  # Resident Set Size in MB
    vms_mb: float  # Virtual Memory Size in MB
    percent: float  # Percentage of system memory
    available_mb: float  # Available system memory in MB
    video_frames: int
    audio_frames: int
    recording_duration: float
    # Profiling data
    tracemalloc_current_mb: Optional[float] = None
    tracemalloc_peak_mb: Optional[float] = None
    top_allocations: Optional[List[Dict[str, Any]]] = None
    object_counts: Optional[Dict[str, int]] = None


def get_memory_usage_system(pid: int) -> Dict[str, float]:
    """Get memory usage using system commands (fallback when psutil not available)."""
    try:
        # Use ps command on macOS/Linux
        result = subprocess.run(
            ["ps", "-o", "rss,vsz,%mem", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) < 2:
            return {"rss_mb": 0.0, "vms_mb": 0.0, "percent": 0.0, "available_mb": 0.0}
        
        # Parse output: RSS VSZ %MEM
        parts = lines[1].split()
        if len(parts) >= 3:
            rss_kb = float(parts[0])
            vms_kb = float(parts[1])
            percent = float(parts[2])
            
            # Get system memory using vm_stat or sysctl
            try:
                if sys.platform == "darwin":  # macOS
                    result = subprocess.run(
                        ["sysctl", "hw.memsize"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    total_mem = int(result.stdout.split(":")[1].strip())
                    # Rough estimate of available memory (this is simplified)
                    available_mb = total_mem / 1024 / 1024 * 0.5  # Assume 50% available
                else:  # Linux
                    with open("/proc/meminfo", "r") as f:
                        meminfo = f.read()
                        for line in meminfo.split("\n"):
                            if "MemAvailable:" in line:
                                available_kb = int(line.split()[1])
                                available_mb = available_kb / 1024
                                break
                        else:
                            available_mb = 0
            except:
                available_mb = 0
            
            return {
                "rss_mb": rss_kb / 1024,  # Convert KB to MB
                "vms_mb": vms_kb / 1024,  # Convert KB to MB
                "percent": percent,
                "available_mb": available_mb,
            }
    except Exception as e:
        logger.warning(f"Error getting memory usage via system commands: {e}")
    
    return {"rss_mb": 0.0, "vms_mb": 0.0, "percent": 0.0, "available_mb": 0.0}


def get_memory_usage(process_or_pid) -> Dict[str, float]:
    """Get current memory usage for a process."""
    if HAS_PSUTIL:
        try:
            process = process_or_pid if hasattr(process_or_pid, "memory_info") else psutil.Process(process_or_pid)
            memory_info = process.memory_info()
            memory_percent = process.memory_percent()
            
            # Get system memory
            system_memory = psutil.virtual_memory()
            
            return {
                "rss_mb": memory_info.rss / 1024 / 1024,  # MB
                "vms_mb": memory_info.vms / 1024 / 1024,  # MB
                "percent": memory_percent,
                "available_mb": system_memory.available / 1024 / 1024,  # MB
            }
        except Exception as e:
            logger.warning(f"Error getting memory usage via psutil: {e}")
            # Fall back to system commands
            pid = process_or_pid if isinstance(process_or_pid, int) else process_or_pid.pid
            return get_memory_usage_system(pid)
    else:
        # Use system commands
        pid = process_or_pid if isinstance(process_or_pid, int) else process_or_pid.pid
        return get_memory_usage_system(pid)


def get_tracemalloc_stats() -> Dict[str, Any]:
    """Get tracemalloc statistics if tracing is active."""
    if not tracemalloc.is_tracing():
        return {}
    
    try:
        current, peak = tracemalloc.get_traced_memory()
        current_mb = current / 1024 / 1024
        peak_mb = peak / 1024 / 1024
        
        # Get top 10 allocations by size
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics('lineno')
        
        top_allocations = []
        for index, stat in enumerate(top_stats[:10], 1):
            top_allocations.append({
                "rank": index,
                "size_mb": stat.size / 1024 / 1024,
                "count": stat.count,
                "filename": stat.traceback[0].filename if stat.traceback else "unknown",
                "lineno": stat.traceback[0].lineno if stat.traceback else 0,
            })
        
        return {
            "current_mb": current_mb,
            "peak_mb": peak_mb,
            "top_allocations": top_allocations,
        }
    except Exception as e:
        logger.warning(f"Error getting tracemalloc stats: {e}")
        return {}


def get_object_counts() -> Dict[str, int]:
    """Count objects by type in memory."""
    try:
        gc.collect()  # Force collection before counting
        objects = gc.get_objects()
        
        type_counts: Dict[str, int] = {}
        for obj in objects:
            obj_type = type(obj).__name__
            type_counts[obj_type] = type_counts.get(obj_type, 0) + 1
        
        # Sort by count and return top 20
        sorted_counts = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        return dict(sorted_counts)
    except Exception as e:
        logger.warning(f"Error counting objects: {e}")
        return {}


async def monitor_memory(
    process_or_pid: Any,
    recorder: ParticipantRecorder,
    start_time: float,
    duration_seconds: float,
    interval_seconds: float = 60.0,
    snapshots: List[MemorySnapshot] = None,
    enable_profiling: bool = True,
) -> List[MemorySnapshot]:
    """Monitor memory usage at regular intervals during recording."""
    if snapshots is None:
        snapshots = []
    
    # Take initial snapshot at start
    memory_info = get_memory_usage(process_or_pid)
    stats = recorder.get_stats()
    
    # Get profiling data if enabled
    tracemalloc_stats = get_tracemalloc_stats() if enable_profiling else {}
    object_counts = get_object_counts() if enable_profiling else {}
    
    initial_snapshot = MemorySnapshot(
        timestamp=start_time,
        elapsed_seconds=0.0,
        rss_mb=memory_info["rss_mb"],
        vms_mb=memory_info["vms_mb"],
        percent=memory_info["percent"],
        available_mb=memory_info["available_mb"],
        video_frames=stats.video_frames_recorded,
        audio_frames=stats.audio_frames_recorded,
        recording_duration=stats.recording_duration_seconds,
        tracemalloc_current_mb=tracemalloc_stats.get("current_mb"),
        tracemalloc_peak_mb=tracemalloc_stats.get("peak_mb"),
        top_allocations=tracemalloc_stats.get("top_allocations"),
        object_counts=object_counts,
    )
    snapshots.append(initial_snapshot)
    
    log_msg = (
        f"[0.0s] Initial Memory: RSS={initial_snapshot.rss_mb:.1f}MB, "
        f"VMS={initial_snapshot.vms_mb:.1f}MB, Percent={initial_snapshot.percent:.1f}%"
    )
    if enable_profiling and tracemalloc_stats:
        log_msg += f", Traced: {tracemalloc_stats.get('current_mb', 0):.1f}MB"
    logger.info(log_msg)
    
    elapsed = 0.0
    check_count = 0
    
    while elapsed < duration_seconds:
        await asyncio.sleep(interval_seconds)
        elapsed = time.time() - start_time
        
        if elapsed >= duration_seconds:
            break
        
        check_count += 1
        memory_info = get_memory_usage(process_or_pid)
        stats = recorder.get_stats()
        
        # Get profiling data if enabled
        tracemalloc_stats = get_tracemalloc_stats() if enable_profiling else {}
        object_counts = get_object_counts() if enable_profiling else {}
        
        snapshot = MemorySnapshot(
            timestamp=time.time(),
            elapsed_seconds=elapsed,
            rss_mb=memory_info["rss_mb"],
            vms_mb=memory_info["vms_mb"],
            percent=memory_info["percent"],
            available_mb=memory_info["available_mb"],
            video_frames=stats.video_frames_recorded,
            audio_frames=stats.audio_frames_recorded,
            recording_duration=stats.recording_duration_seconds,
            tracemalloc_current_mb=tracemalloc_stats.get("current_mb"),
            tracemalloc_peak_mb=tracemalloc_stats.get("peak_mb"),
            top_allocations=tracemalloc_stats.get("top_allocations"),
            object_counts=object_counts,
        )
        snapshots.append(snapshot)
        
        log_msg = (
            f"[{elapsed:.1f}s] Memory: RSS={snapshot.rss_mb:.1f}MB, "
            f"VMS={snapshot.vms_mb:.1f}MB, Percent={snapshot.percent:.1f}%, "
            f"Frames: {snapshot.video_frames} video, {snapshot.audio_frames} audio"
        )
        if enable_profiling and tracemalloc_stats:
            log_msg += f", Traced: {tracemalloc_stats.get('current_mb', 0):.1f}MB"
        logger.info(log_msg)
        
        # Log top allocations periodically (every 5 minutes)
        if enable_profiling and check_count % 5 == 0 and tracemalloc_stats.get("top_allocations"):
            logger.info("  Top 5 allocations:")
            for alloc in tracemalloc_stats["top_allocations"][:5]:
                logger.info(
                    f"    {alloc['filename']}:{alloc['lineno']} - "
                    f"{alloc['size_mb']:.2f}MB ({alloc['count']} allocations)"
                )
        
        # Log top object types periodically (every 5 minutes)
        if enable_profiling and check_count % 5 == 0 and object_counts:
            logger.info("  Top 5 object types:")
            for obj_type, count in list(object_counts.items())[:5]:
                logger.info(f"    {obj_type}: {count:,} objects")
    
    return snapshots


def analyze_memory_usage(snapshots: List[MemorySnapshot]) -> Dict[str, Any]:
    """Analyze memory usage trends from snapshots."""
    if not snapshots:
        return {"error": "No snapshots available"}
    
    rss_values = [s.rss_mb for s in snapshots]
    vms_values = [s.vms_mb for s in snapshots]
    percent_values = [s.percent for s in snapshots]
    
    # Calculate trends
    initial_rss = rss_values[0] if rss_values else 0
    final_rss = rss_values[-1] if rss_values else 0
    rss_growth = final_rss - initial_rss
    rss_growth_percent = (rss_growth / initial_rss * 100) if initial_rss > 0 else 0
    
    # Find peak values
    peak_rss = max(rss_values) if rss_values else 0
    peak_vms = max(vms_values) if vms_values else 0
    peak_percent = max(percent_values) if percent_values else 0
    
    # Calculate average growth rate (MB per minute)
    if len(snapshots) > 1:
        time_span_minutes = (snapshots[-1].elapsed_seconds - snapshots[0].elapsed_seconds) / 60.0
        growth_rate_mb_per_min = rss_growth / time_span_minutes if time_span_minutes > 0 else 0
    else:
        growth_rate_mb_per_min = 0
    
    return {
        "initial_rss_mb": initial_rss,
        "final_rss_mb": final_rss,
        "peak_rss_mb": peak_rss,
        "peak_vms_mb": peak_vms,
        "peak_percent": peak_percent,
        "rss_growth_mb": rss_growth,
        "rss_growth_percent": rss_growth_percent,
        "growth_rate_mb_per_min": growth_rate_mb_per_min,
        "snapshot_count": len(snapshots),
        "time_span_minutes": (snapshots[-1].elapsed_seconds - snapshots[0].elapsed_seconds) / 60.0 if len(snapshots) > 1 else 0,
    }


async def run_memory_test(
    duration_minutes: float = 20.0,
    monitoring_interval_seconds: float = 60.0,
) -> Dict[str, Any]:
    """Run a long-duration recording test with memory monitoring."""
    duration_seconds = duration_minutes * 60.0
    
    logger.info("=" * 80)
    logger.info("Memory Monitoring Test for ParticipantRecorder")
    logger.info("=" * 80)
    logger.info(f"Duration: {duration_minutes} minutes ({duration_seconds} seconds)")
    logger.info(f"Monitoring interval: {monitoring_interval_seconds} seconds")
    
    # Enable profiling
    enable_profiling = os.getenv("LIVEKIT_ENABLE_PROFILING", "true").lower() == "true"
    if enable_profiling:
        tracemalloc.start()
        logger.info("Memory profiling enabled (tracemalloc + object counting)")
    else:
        logger.info("Memory profiling disabled")
    logger.info("=" * 80)
    
    # Get current process ID
    process_id = os.getpid()
    if HAS_PSUTIL:
        process = psutil.Process(process_id)
    else:
        process = process_id
    
    results: Dict[str, Any] = {
        "success": False,
        "errors": [],
        "warnings": [],
        "snapshots": [],
        "memory_analysis": {},
        "final_stats": {},
    }
    
    room = None
    recorder = None
    
    try:
        # Get room credentials
        if os.getenv("LIVEKIT_USE_PUMPFUN", "false").lower() == "true" and HAS_PUMPFUN:
            pumpfun = PumpFunService()
            stream_info = await pumpfun.get_random_live_stream(
                min_participants=1,
                exclude_nsfw=True,
            )
            if not stream_info:
                raise RecordingError("Failed to get stream from pump.fun")
            
            mint_id = stream_info.get("mint")
            if not mint_id:
                raise RecordingError("Selected stream has no mint ID")
            
            logger.info(f"Getting token for stream: {stream_info.get('name', 'Unknown')} ({mint_id})")
            token_str = await pumpfun.get_livestream_token(mint_id, role="viewer")
            if not token_str:
                raise RecordingError("Failed to get token from pump.fun")
            
            url = pumpfun.get_livekit_url()
            room_name = "pump.fun stream"
            logger.info(f"Using pump.fun stream: {stream_info.get('name', 'Unknown')}")
        else:
            url = os.getenv("LIVEKIT_URL")
            api_key = os.getenv("LIVEKIT_API_KEY")
            api_secret = os.getenv("LIVEKIT_API_SECRET")
            room_name = os.getenv("LIVEKIT_ROOM_NAME", "test-memory-room")
            
            if not url or not api_key or not api_secret:
                raise RecordingError("Missing LIVEKIT_URL, LIVEKIT_API_KEY, or LIVEKIT_API_SECRET")
            
            token_opts = livekit_api.CreateTokenOptions()
            token_opts.identity = "memory-test-client"
            token = livekit_api.AccessToken(api_key, api_secret, options=token_opts)
            token.grant_room_join(room_name)
            token_str = token.to_jwt()
        
        # Connect to room
        logger.info(f"Connecting to room: {room_name}")
        room = rtc.Room()
        await room.connect(url, token_str)
        logger.info("Connected to room")
        
        # Wait for participant
        logger.info("Waiting for participant...")
        participant = await wait_for_participant(room, timeout=30.0)
        if not participant:
            raise RecordingError("No participant found in room")
        
        logger.info(f"Participant found: {participant.identity}")
        
        # Wait for tracks
        has_video, has_audio = await wait_for_tracks(participant, timeout=20.0)
        logger.info(f"Tracks available - Video: {has_video}, Audio: {has_audio}")
        
        if not has_video and not has_audio:
            results["warnings"].append("No video or audio tracks found")
        
        # Create recorder
        recorder = ParticipantRecorder(room)
        logger.info("Recorder created")
        
        # Start recording
        logger.info(f"Starting recording for {duration_minutes} minutes...")
        await recorder.start_recording(participant.identity)
        
        start_time = time.time()
        snapshots: List[MemorySnapshot] = []
        
        # Start memory monitoring task
        monitoring_task = asyncio.create_task(
            monitor_memory(
                process,
                recorder,
                start_time,
                duration_seconds,
                monitoring_interval_seconds,
                snapshots,
                enable_profiling=enable_profiling,
            )
        )
        
        # Record for specified duration
        await asyncio.sleep(duration_seconds)
        
        # Wait for monitoring task to complete
        await monitoring_task
        
        # Get final stats
        stats = recorder.get_stats()
        results["final_stats"] = {
            "video_frames_recorded": stats.video_frames_recorded,
            "audio_frames_recorded": stats.audio_frames_recorded,
            "recording_duration_seconds": stats.recording_duration_seconds,
        }
        
        logger.info(f"Final stats: {results['final_stats']}")
        
        # Stop recording
        logger.info("Stopping recording...")
        output_file = tempfile.NamedTemporaryFile(suffix=".webm", delete=False).name
        await recorder.stop_recording(output_file)
        results["output_file"] = output_file
        results["output_file_size_bytes"] = os.path.getsize(output_file) if os.path.exists(output_file) else 0
        
        # Get final profiling snapshot
        if enable_profiling:
            final_tracemalloc_stats = get_tracemalloc_stats()
            final_object_counts = get_object_counts()
            results["final_tracemalloc_stats"] = final_tracemalloc_stats
            results["final_object_counts"] = final_object_counts
            
            if final_tracemalloc_stats:
                logger.info(
                    f"Final tracemalloc: {final_tracemalloc_stats.get('current_mb', 0):.1f}MB "
                    f"(peak: {final_tracemalloc_stats.get('peak_mb', 0):.1f}MB)"
                )
        
        # Analyze memory usage
        results["snapshots"] = [asdict(s) for s in snapshots]
        results["memory_analysis"] = analyze_memory_usage(snapshots)
        
        results["success"] = True
        
    except Exception as e:
        logger.error(f"Error during test: {e}", exc_info=True)
        results["errors"].append(str(e))
        results["success"] = False
    
    finally:
        if enable_profiling and tracemalloc.is_tracing():
            tracemalloc.stop()
        
        if recorder:
            try:
                await recorder.stop_recording()
            except:
                pass
        
        if room:
            try:
                await room.disconnect()
            except:
                pass
    
    return results


def print_memory_report(results: Dict[str, Any]) -> None:
    """Print a formatted memory usage report."""
    print("\n" + "=" * 80)
    print("Memory Usage Report")
    print("=" * 80)
    
    if not results.get("success"):
        print("❌ Test failed")
        if results.get("errors"):
            print("\nErrors:")
            for error in results["errors"]:
                print(f"  - {error}")
        return
    
    analysis = results.get("memory_analysis", {})
    snapshots = results.get("snapshots", [])
    
    print(f"\nTest Duration: {results['final_stats'].get('recording_duration_seconds', 0) / 60:.1f} minutes")
    print(f"Monitoring Intervals: {len(snapshots)} snapshots")
    
    if analysis:
        print("\nMemory Statistics:")
        print(f"  Initial RSS: {analysis.get('initial_rss_mb', 0):.1f} MB")
        print(f"  Final RSS: {analysis.get('final_rss_mb', 0):.1f} MB")
        print(f"  Peak RSS: {analysis.get('peak_rss_mb', 0):.1f} MB")
        print(f"  Peak VMS: {analysis.get('peak_vms_mb', 0):.1f} MB")
        print(f"  Peak %: {analysis.get('peak_percent', 0):.1f}%")
        
        print("\nMemory Growth:")
        growth_mb = analysis.get('rss_growth_mb', 0)
        growth_percent = analysis.get('rss_growth_percent', 0)
        growth_rate = analysis.get('growth_rate_mb_per_min', 0)
        
        print(f"  Total Growth: {growth_mb:+.1f} MB ({growth_percent:+.1f}%)")
        print(f"  Growth Rate: {growth_rate:+.2f} MB/minute")
        
        if abs(growth_mb) > 100 or abs(growth_rate) > 5:
            print("\n⚠️  WARNING: Significant memory growth detected!")
        elif abs(growth_mb) < 50 and abs(growth_rate) < 2:
            print("\n✓ Memory usage appears stable")
    
    print("\nTimeline (every 60 seconds):")
    print(f"{'Time':<10} {'RSS (MB)':<12} {'VMS (MB)':<12} {'%':<8} {'Video':<10} {'Audio':<10}")
    print("-" * 80)
    
    for snapshot in snapshots:
        elapsed_min = snapshot["elapsed_seconds"] / 60.0
        print(
            f"{elapsed_min:>6.1f}m  "
            f"{snapshot['rss_mb']:>10.1f}  "
            f"{snapshot['vms_mb']:>10.1f}  "
            f"{snapshot['percent']:>6.1f}  "
            f"{snapshot['video_frames']:>8}  "
            f"{snapshot['audio_frames']:>8}"
        )
    
    print("\nFinal Recording Stats:")
    stats = results.get("final_stats", {})
    print(f"  Video frames: {stats.get('video_frames_recorded', 0):,}")
    print(f"  Audio frames: {stats.get('audio_frames_recorded', 0):,}")
    print(f"  File size: {results.get('output_file_size_bytes', 0):,} bytes")
    print(f"  Output file: {results.get('output_file', 'N/A')}")
    
    # Print profiling results if available
    if results.get("final_tracemalloc_stats"):
        print("\n" + "=" * 80)
        print("Memory Profiling Results")
        print("=" * 80)
        
        tracemalloc_stats = results["final_tracemalloc_stats"]
        print(f"\nTracemalloc Statistics:")
        print(f"  Current traced: {tracemalloc_stats.get('current_mb', 0):.1f} MB")
        print(f"  Peak traced: {tracemalloc_stats.get('peak_mb', 0):.1f} MB")
        
        if tracemalloc_stats.get("top_allocations"):
            print(f"\n  Top 10 Allocations by Size:")
            for alloc in tracemalloc_stats["top_allocations"]:
                print(
                    f"    {alloc['rank']}. {alloc['filename']}:{alloc['lineno']} - "
                    f"{alloc['size_mb']:.2f} MB ({alloc['count']:,} allocations)"
                )
    
    if results.get("final_object_counts"):
        print(f"\n  Top 10 Object Types by Count:")
        for obj_type, count in list(results["final_object_counts"].items())[:10]:
            print(f"    {obj_type}: {count:,} objects")
    
    print("=" * 80)


async def main():
    """Main entry point."""
    duration_minutes = float(os.getenv("LIVEKIT_RECORDING_DURATION", "20.0"))
    monitoring_interval = float(os.getenv("LIVEKIT_MEMORY_INTERVAL", "60.0"))
    
    results = await run_memory_test(
        duration_minutes=duration_minutes,
        monitoring_interval_seconds=monitoring_interval,
    )
    
    print_memory_report(results)
    
    # Save results to JSON file
    output_file = Path(f"/tmp/memory_test_results_{int(time.time())}.json")
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\nDetailed results saved to: {output_file}")
    
    if not results.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

