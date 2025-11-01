# ParticipantRecorder Integration Test

This integration test validates the `ParticipantRecorder` implementation by connecting to a live LiveKit room and recording an actual participant stream.

## Prerequisites

1. **LiveKit Server**: Access to a LiveKit server with API credentials
2. **PyAV**: Required for recording (`pip install av`)
3. **Active Stream**: A participant must be streaming in the target room

## Setup

1. Install dependencies:
```bash
pip install av numpy
```

2. Set environment variables:
```bash
export LIVEKIT_URL=wss://your-livekit-server.com
export LIVEKIT_API_KEY=your-api-key
export LIVEKIT_API_SECRET=your-api-secret
export LIVEKIT_ROOM_NAME=test-room  # Optional, defaults to "test-recording-room"
export LIVEKIT_PARTICIPANT_IDENTITY=participant-id  # Optional, records first participant if not specified
export LIVEKIT_RECORDING_DURATION=5.0  # Optional, defaults to 5.0 seconds
```

## Running the Test

### Direct Execution

```bash
cd livekit-rtc
python tests/test_recorder_integration.py
```

### With Pytest

```bash
cd livekit-rtc
pytest tests/test_recorder_integration.py -v -s
```

## What the Test Does

1. **Connects** to the specified LiveKit room
2. **Waits** for a participant to join (or uses specified participant)
3. **Waits** for tracks (video/audio) to be published
4. **Starts recording** the participant's stream
5. **Records** for the specified duration
6. **Stops recording** and saves to a temporary WebM file
7. **Validates** the output file:
   - Checks file exists and has content
   - Verifies WebM container format
   - Validates video/audio codecs (VP8/VP9, Opus)
   - Counts frames
   - Checks stream properties (resolution, sample rate, etc.)

## Output

The test provides detailed output including:
- Connection status
- Participant information
- Track availability (video/audio)
- Recording statistics (frame counts, duration, file size)
- Validation results
- Any errors or warnings

## Iteration Workflow

1. **Run the test** against a live stream
2. **Review the output** for validation results
3. **Check the recorded file** if saved (verify playback)
4. **Fix any issues** in the recorder implementation
5. **Re-run the test** to verify fixes
6. **Repeat** until validation passes

## Troubleshooting

### No Participant Found
- Ensure someone is actively streaming in the room
- Check the room name is correct
- Verify participant identity if specified

### No Frames Recorded
- Ensure the participant has published tracks
- Check that tracks are subscribed
- Verify video/audio tracks are not muted

### Validation Failures
- Check PyAV installation: `pip install av`
- Verify the output file was created
- Check file permissions for output directory

### Import Errors
The test uses direct imports from the local codebase to avoid version conflicts. If you see import errors:
- Ensure you're running from the `livekit-rtc` directory
- Verify the local `livekit` package is available
- Check that all dependencies are installed

## Test Output Example

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

