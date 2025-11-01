# Integration Tester Instructions: ParticipantRecorder

## Your Task

You are tasked with validating and iterating on the `ParticipantRecorder` implementation that was just created. Your goal is to ensure the recording functionality works correctly with live streams, validate the recorded output, and provide feedback/iterations to fix any issues found.

## What Was Implemented

The `ParticipantRecorder` class in `livekit/rtc/recorder.py` enables browserless recording of remote participant streams. It:

- Captures video and audio from a specific remote participant in a LiveKit room
- Encodes to WebM format with VP8/VP9 video and Opus audio codecs
- Works entirely in a headless backend environment (no browser/Electron)
- Saves recordings to disk as `.webm` files

## Key Files

- **Implementation**: `livekit/rtc/recorder.py` - The ParticipantRecorder class
- **Integration Test**: `tests/test_recorder_integration.py` - Test against live streams
- **Unit Tests**: `tests/test_recorder.py` - Unit tests (already passing)
- **Documentation**: `tests/RECORDER_INTEGRATION_TEST.md` - Test documentation

## Prerequisites Setup

### 1. Install Dependencies

```bash
cd livekit-rtc
pip install av>=11.0.0 numpy>=1.26
```

### 2. Install Optional Dependencies for Automatic Stream Discovery

For automatic stream discovery from pump.fun (recommended):

```bash
pip install httpx
```

This allows the test to automatically find and use a live stream from pump.fun without manual setup.

### 3. Set Environment Variables (Optional)

**Option A: Use pump.fun (Recommended - No Setup Required)**

The test will automatically discover a random live stream from pump.fun. Just run:

```bash
# No environment variables needed!
# Install httpx if not already installed: pip install httpx
export LIVEKIT_RECORDING_DURATION=5.0  # Optional: defaults to 5 seconds
```

**Option B: Use Custom LiveKit Server**

If you want to use your own LiveKit server instead:

```bash
export LIVEKIT_USE_PUMPFUN=false  # Disable pump.fun
export LIVEKIT_URL=wss://your-livekit-server.com
export LIVEKIT_API_KEY=your-api-key
export LIVEKIT_API_SECRET=your-api-secret
export LIVEKIT_ROOM_NAME=test-room  # Optional: defaults to "test-recording-room"
export LIVEKIT_PARTICIPANT_IDENTITY=participant-id  # Optional: records first participant if not set
export LIVEKIT_RECORDING_DURATION=5.0  # Optional: defaults to 5 seconds
```

**Note**: When using pump.fun, the test automatically:
- Finds a random live stream
- Gets a token for that stream
- Connects and records from it
- No manual setup needed!

## Running the Integration Test

### Quick Start (Using pump.fun - Recommended)

The test will automatically find a live stream from pump.fun:

```bash
cd livekit-rtc
pip install httpx  # If not already installed
python tests/test_recorder_integration.py
```

That's it! No configuration needed.

### Method 1: Direct Execution (Recommended for Iteration)

```bash
cd livekit-rtc
python tests/test_recorder_integration.py
```

This gives you the most detailed output and is easiest to iterate with.

**With pump.fun (default):**
- Automatically finds a random live stream
- No credentials needed
- Works out of the box

**With custom server:**
- Set `LIVEKIT_USE_PUMPFUN=false` and provide credentials

### Method 2: Pytest

```bash
cd livekit-rtc
pytest tests/test_recorder_integration.py -v -s
```

Use `-s` to see print statements and `-v` for verbose output.

## Validation Checklist

When running the test, verify these aspects:

### ✅ Basic Functionality

- [ ] Test connects to the LiveKit room successfully
- [ ] Test finds the participant (or waits for one)
- [ ] Test detects video and/or audio tracks
- [ ] Recording starts without errors
- [ ] Recording stops and saves successfully

### ✅ Frame Capture

- [ ] Video frames are being captured (check stats: `video_frames_recorded > 0` if video track available)
- [ ] Audio frames are being captured (check stats: `audio_frames_recorded > 0` if audio track available)
- [ ] Frame counts increase during recording duration

### ✅ Output File Validation

- [ ] Output file is created and not empty
- [ ] File size is reasonable (not 0 bytes)
- [ ] WebM validation passes (format is correct)
- [ ] Video stream exists if video track was available
- [ ] Audio stream exists if audio track was available
- [ ] Codecs are correct (VP8 or VP9 for video, Opus for audio)

### ✅ Playback Verification (Manual)

After the test completes, manually verify:
- [ ] Open the output `.webm` file in a media player (VLC, ffplay, etc.)
- [ ] Video plays correctly (if video was recorded)
- [ ] Audio plays correctly (if audio was recorded)
- [ ] Duration matches expected recording time
- [ ] No corruption or artifacts

## What to Look For: Common Issues

### Issue: No Frames Recorded

**Symptoms:**
- `video_frames_recorded: 0` or `audio_frames_recorded: 0`
- Empty or very small output file

**Possible Causes:**
1. Tracks not subscribed properly
2. VideoStream/AudioStream not capturing frames
3. Queue not receiving frames
4. Frame capture tasks not running

**Check:**
- Verify tracks are subscribed (`publication.subscribed == True`)
- Check that `VideoStream`/`AudioStream` are created correctly
- Ensure `_capture_video_frames()` and `_capture_audio_frames()` tasks are running

### Issue: Invalid WebM File

**Symptoms:**
- Validation fails with "Invalid format" or similar
- File doesn't play in media players

**Possible Causes:**
1. Incorrect frame conversion to PyAV format
2. Wrong codec settings
3. Missing frames or incorrect timestamps
4. Encoding errors

**Check:**
- Verify `_convert_video_frame_to_pyav()` creates valid PyAV frames
- Check codec parameters (VP8/VP9, Opus)
- Ensure PTS (presentation timestamps) are set correctly
- Verify frame encoding pipeline

### Issue: Empty or Corrupted Output

**Symptoms:**
- File size is 0 bytes or very small
- File exists but can't be opened
- Validation reports "File is empty"

**Possible Causes:**
1. No frames in queues when encoding starts
2. Encoding runs before frames are captured
3. File writing errors

**Check:**
- Ensure frames are queued before encoding
- Verify encoding waits for frames
- Check file write permissions

### Issue: Audio/Video Sync Issues

**Symptoms:**
- Video and audio play but are out of sync
- Stuttering or choppy playback

**Possible Causes:**
1. Incorrect PTS calculation
2. Missing frames
3. Frame rate mismatch

**Check:**
- Verify PTS calculation for both video and audio
- Ensure consistent frame timing
- Check frame rate settings match stream

## Iteration Process

### Step 1: Initial Test Run

1. Run the integration test with default settings
2. Review all output carefully
3. Note any errors, warnings, or validation failures
4. Check the output file if created

### Step 2: Analyze Results

For each issue found:
1. **Identify the root cause** - Look at error messages, stats, validation results
2. **Locate the problematic code** - Check `livekit/rtc/recorder.py`
3. **Understand the issue** - Review the implementation logic

### Step 3: Fix and Verify

1. **Make fixes** to `livekit/rtc/recorder.py`
2. **Re-run the test** immediately
3. **Compare results** - Did the fix work? Are there new issues?
4. **Repeat** until all validations pass

### Step 4: Edge Case Testing

Once basic functionality works, test edge cases:
- Record with only video (no audio)
- Record with only audio (no video)
- Record for different durations (1s, 10s, 30s)
- Record multiple participants sequentially
- Test with different video resolutions
- Test with different codec settings (VP8 vs VP9)

## Debugging Tips

### Enable Detailed Logging

The test already uses logging. To see more details, modify the logging level in the test file:

```python
logging.basicConfig(level=logging.DEBUG)  # Change from INFO to DEBUG
```

### Check Frame Queues

Add temporary logging in `recorder.py` to see what's happening:

```python
# In _capture_video_frames()
logger.debug(f"Captured video frame, queue size: {self._video_queue.qsize()}")

# In _capture_audio_frames()
logger.debug(f"Captured audio frame, queue size: {self._audio_queue.qsize()}")
```

### Inspect Recorded Files

Use command-line tools to inspect the output:

```bash
# Get file info
ffprobe output.webm

# Play the file
ffplay output.webm

# Extract frames
ffmpeg -i output.webm -vf "select='eq(n\,0)'" -vsync vfr frame_%03d.png
```

### Verify Frame Conversion

Add validation in frame conversion methods:

```python
# In _convert_video_frame_to_pyav()
if pyav_frame:
    logger.debug(f"Converted frame: {pyav_frame.width}x{pyav_frame.height}, pts={pyav_frame.pts}")
else:
    logger.warning("Failed to convert video frame")
```

## Expected Test Output (Success Case)

**With pump.fun (Automatic Stream Discovery):**

```
================================================================================
LiveKit ParticipantRecorder Integration Test
================================================================================

2024-01-01 12:00:00 - INFO - Using pump.fun for automatic stream discovery...
2024-01-01 12:00:01 - INFO - Fetching live streams with params: {...}
2024-01-01 12:00:02 - INFO - Found 45 live streams
2024-01-01 12:00:02 - INFO - Selected stream: MoonCoin (MOON) - 12 participants
2024-01-01 12:00:02 - INFO - Requesting token for mint_id: Abc123...
2024-01-01 12:00:03 - INFO - Successfully obtained token for pump.fun stream
2024-01-01 12:00:03 - INFO - Using pump.fun provided token
2024-01-01 12:00:03 - INFO - Connecting to room: pump.fun stream
2024-01-01 12:00:04 - INFO - Participant found: streamer_xyz
2024-01-01 12:00:04 - INFO - Tracks available - Video: True, Audio: True
2024-01-01 12:00:04 - INFO - Starting recording for 5.0 seconds...
2024-01-01 12:00:09 - INFO - Recording stats: {'video_frames_recorded': 150, 'audio_frames_recorded': 240}
2024-01-01 12:00:09 - INFO - Stopping recording and saving to: /tmp/recording_xyz.webm
2024-01-01 12:00:10 - INFO - ✓ Recording validation passed
2024-01-01 12:00:10 - INFO -   File size: 245760 bytes
2024-01-01 12:00:10 - INFO -   Streams: 2
2024-01-01 12:00:10 - INFO -   - video stream: vp8
2024-01-01 12:00:10 - INFO -   - audio stream: opus

================================================================================
Test Results
================================================================================
Success: True
Source: pumpfun
Stream: MoonCoin (MOON)
  Mint ID: Abc123...
  Participants: 12
Room: room_abc123
Participant: streamer_xyz

Final Statistics:
  Video frames: 150
  Audio frames: 240
  Duration: 5.00s
  File size: 245760 bytes

Validation: PASSED
  File size: 245760 bytes
  Streams: 2
    - video: vp8
    - audio: opus

Output file: /tmp/recording_xyz.webm
================================================================================
```

**With Custom LiveKit Server:**

```
================================================================================
LiveKit ParticipantRecorder Integration Test
================================================================================

2024-01-01 12:00:00 - INFO - Connecting to room: test-room
2024-01-01 12:00:01 - INFO - Participant found: user123
2024-01-01 12:00:01 - INFO - Tracks available - Video: True, Audio: True
2024-01-01 12:00:01 - INFO - Starting recording for 5.0 seconds...
2024-01-01 12:00:06 - INFO - Recording stats: {'video_frames_recorded': 150, 'audio_frames_recorded': 240}
2024-01-01 12:00:06 - INFO - Stopping recording and saving to: /tmp/recording_xyz.webm
2024-01-01 12:00:07 - INFO - ✓ Recording validation passed
2024-01-01 12:00:07 - INFO -   File size: 245760 bytes
2024-01-01 12:00:07 - INFO -   Streams: 2
2024-01-01 12:00:07 - INFO -   - video stream: vp8
2024-01-01 12:00:07 - INFO -   - audio stream: opus

================================================================================
Test Results
================================================================================
Success: True
Source: custom
Room: test-room
Participant: user123

Final Statistics:
  Video frames: 150
  Audio frames: 240
  Duration: 5.00s
  File size: 245760 bytes

Validation: PASSED
  File size: 245760 bytes
  Streams: 2
    - video: vp8
    - audio: opus

Output file: /tmp/recording_xyz.webm
================================================================================
```

## Success Criteria

The test is successful when:

1. ✅ **Connection**: Successfully connects to LiveKit room
2. ✅ **Participant**: Finds and identifies the participant
3. ✅ **Tracks**: Detects available tracks (video/audio)
4. ✅ **Recording**: Starts and stops recording without errors
5. ✅ **Frames**: Captures frames (count > 0 for available tracks)
6. ✅ **Output**: Creates non-empty output file
7. ✅ **Validation**: Passes all WebM validation checks
8. ✅ **Playback**: Output file can be played in media players (manual check)
9. ✅ **Stats**: Statistics match expected values (duration, frame counts)

## Reporting Issues

When reporting issues, include:

1. **Error messages** - Full traceback if available
2. **Test output** - Complete console output
3. **Statistics** - Frame counts, file sizes, durations
4. **Validation results** - What passed/failed
5. **Steps to reproduce** - Exact commands and environment
6. **Screenshots** - If relevant (e.g., playback issues)

## Next Steps After Successful Validation

Once the test passes and all validations succeed:

1. **Document any fixes** you made
2. **Note any edge cases** discovered
3. **Suggest improvements** if any
4. **Verify unit tests still pass**: `pytest tests/test_recorder.py`
5. **Confirm code follows style guidelines** (type hints, no `any` types)

## Quick Reference Commands

```bash
# Run integration test
cd livekit-rtc && python tests/test_recorder_integration.py

# Run with pytest
cd livekit-rtc && pytest tests/test_recorder_integration.py -v -s

# Run unit tests
cd livekit-rtc && pytest tests/test_recorder.py -v

# Check file with ffprobe
ffprobe output.webm

# Play recorded file
ffplay output.webm

# Set environment variable for duration
export LIVEKIT_RECORDING_DURATION=10.0
```

## Important Notes

- **Direct Imports**: The test uses direct imports from the local codebase to avoid version conflicts. Make sure you're in the `livekit-rtc` directory when running.
- **Live Stream Required**: This test requires an actual live stream. Ensure a participant is streaming before running.
- **File Cleanup**: The test creates temporary files. They may need manual cleanup if the test crashes.
- **Iterative Development**: Run the test frequently while making changes. Don't wait until all fixes are done.

---

**Remember**: Your goal is to ensure the recording implementation works correctly in a real-world scenario. Be thorough, test edge cases, and provide clear feedback on any issues found.

Good luck! 🎬

