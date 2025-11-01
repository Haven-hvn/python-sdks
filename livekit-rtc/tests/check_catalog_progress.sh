#!/bin/bash
# Quick script to check catalog progress

echo "=== Stream Catalog Progress ==="
echo ""

# Check if process is running
if pgrep -f catalog_stream_types.py > /dev/null; then
    echo "✓ Catalog script is running"
    PID=$(pgrep -f catalog_stream_types.py | head -1)
    echo "  PID: $PID"
else
    echo "✗ Catalog script is not running"
fi

echo ""

# Check latest log
if [ -f /tmp/stream_catalog.log ]; then
    echo "Latest log entries (last 20 lines):"
    echo "---"
    tail -20 /tmp/stream_catalog.log
    echo "---"
else
    echo "No log file found at /tmp/stream_catalog.log"
fi

echo ""

# Check for output directory
OUTPUT_DIRS=$(find /tmp -maxdepth 1 -type d -name "livekit_stream_catalog_*" 2>/dev/null | sort -r | head -1)

if [ -n "$OUTPUT_DIRS" ]; then
    echo "Output directory: $OUTPUT_DIRS"
    echo ""
    
    # Count recordings
    REC_COUNT=$(find "$OUTPUT_DIRS" -name "recording_*.webm" 2>/dev/null | wc -l | tr -d ' ')
    echo "Recordings found: $REC_COUNT"
    
    # Check for catalog files
    if [ -f "$OUTPUT_DIRS/stream_types_catalog.json" ]; then
        echo "✓ Catalog JSON exists"
        TYPES_FOUND=$(python3 -c "import json; f=open('$OUTPUT_DIRS/stream_types_catalog.json'); d=json.load(f); print(len(d.get('stream_types', {})))" 2>/dev/null)
        if [ -n "$TYPES_FOUND" ]; then
            echo "  Unique stream types found: $TYPES_FOUND"
        fi
    fi
    
    if [ -f "$OUTPUT_DIRS/STREAM_TYPES_SUMMARY.md" ]; then
        echo "✓ Summary markdown exists"
    fi
    
    # Show type files
    TYPE_COUNT=$(find "$OUTPUT_DIRS/stream_types" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$TYPE_COUNT" -gt 0 ]; then
        echo "  Type documentation files: $TYPE_COUNT"
        echo ""
        echo "Found types:"
        find "$OUTPUT_DIRS/stream_types" -name "*.json" 2>/dev/null | sed 's|.*/||' | sed 's|\.json||' | sed 's/^/  - /'
    fi
else
    echo "No output directory found yet"
fi

echo ""
echo "To view full log: tail -f /tmp/stream_catalog.log"
echo "To stop: pkill -f catalog_stream_types.py"

