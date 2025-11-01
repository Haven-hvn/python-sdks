#!/bin/bash

# Integration test runner - runs tests for 60 seconds until successful

set -e

cd "$(dirname "$0")/.."
TEST_DIR="$(pwd)"
OUTPUT_DIR="/tmp/livekit_recordings_$(date +%s)"
mkdir -p "$OUTPUT_DIR"

echo "=================================================================================="
echo "LiveKit ParticipantRecorder Integration Test Runner"
echo "=================================================================================="
echo ""
echo "Configuration:"
echo "  - Duration per test: 60 seconds"
echo "  - Output directory: $OUTPUT_DIR"
echo "  - Will run until successful"
echo ""

# Set environment variables
export LIVEKIT_USE_PUMPFUN=true
export LIVEKIT_RECORDING_DURATION=60.0

# Track attempts
ATTEMPT=1
MAX_ATTEMPTS=50
SUCCESS=false

while [ $ATTEMPT -le $MAX_ATTEMPTS ] && [ "$SUCCESS" = false ]; do
    echo "=================================================================================="
    echo "Attempt $ATTEMPT of $MAX_ATTEMPTS"
    echo "=================================================================================="
    echo ""
    
    # Run the test and capture output
    OUTPUT_FILE="$OUTPUT_DIR/attempt_${ATTEMPT}_output.txt"
    RECORDING_FILE="$OUTPUT_DIR/attempt_${ATTEMPT}_recording.webm"
    
    # Set output file path (test will create its own, but we can track it)
    export TEST_OUTPUT_DIR="$OUTPUT_DIR"
    
    # Run test and capture both stdout and stderr
    if python "$TEST_DIR/tests/test_recorder_integration.py" > "$OUTPUT_FILE" 2>&1; then
        echo ""
        echo "✓ Test PASSED on attempt $ATTEMPT!"
        SUCCESS=true
        
        # Find the most recent webm file created
        LATEST_RECORDING=$(find "$OUTPUT_DIR" -name "*.webm" -type f -newest "$OUTPUT_FILE" 2>/dev/null | head -1)
        if [ -n "$LATEST_RECORDING" ]; then
            # Copy to a more friendly name
            cp "$LATEST_RECORDING" "$RECORDING_FILE" 2>/dev/null || true
        fi
        
        # Extract output file path from test output
        OUTPUT_WEBM=$(grep -i "Output file:" "$OUTPUT_FILE" | tail -1 | sed 's/.*Output file: //' | xargs)
        if [ -n "$OUTPUT_WEBM" ] && [ -f "$OUTPUT_WEBM" ]; then
            cp "$OUTPUT_WEBM" "$RECORDING_FILE" 2>/dev/null || true
        fi
        
        echo ""
        echo "=================================================================================="
        echo "SUCCESS - Test completed successfully!"
        echo "=================================================================================="
        echo ""
        echo "Attempt: $ATTEMPT"
        echo "Recording file: $RECORDING_FILE"
        if [ -f "$RECORDING_FILE" ]; then
            FILE_SIZE=$(ls -lh "$RECORDING_FILE" | awk '{print $5}')
            echo "File size: $FILE_SIZE"
        fi
        echo ""
        echo "Please validate the video quality manually by playing:"
        echo "  open '$RECORDING_FILE'"
        echo "  or"
        echo "  ffplay '$RECORDING_FILE'"
        echo ""
        echo "All output files saved to: $OUTPUT_DIR"
        echo ""
        break
    else
        echo ""
        echo "✗ Test FAILED on attempt $ATTEMPT"
        echo "  See output in: $OUTPUT_FILE"
        echo ""
        
        # Show last few lines of output for debugging
        echo "Last 10 lines of output:"
        tail -10 "$OUTPUT_FILE" | sed 's/^/  /'
        echo ""
        
        ATTEMPT=$((ATTEMPT + 1))
        
        # Brief pause between attempts
        sleep 2
    fi
done

if [ "$SUCCESS" = false ]; then
    echo "=================================================================================="
    echo "FAILED - All $MAX_ATTEMPTS attempts exhausted"
    echo "=================================================================================="
    echo ""
    echo "Check output files in: $OUTPUT_DIR"
    exit 1
fi

echo "All done! Output files in: $OUTPUT_DIR"
exit 0

