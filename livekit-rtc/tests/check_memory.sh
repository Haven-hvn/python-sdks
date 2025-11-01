#!/bin/bash
# Quick check of memory usage

PID=$(pgrep -f catalog_stream_types.py | head -1)

if [ -z "$PID" ]; then
    echo "No catalog process running"
    exit 1
fi

echo "=== Memory Usage Monitor ==="
echo "PID: $PID"
echo ""

# Current memory
MEM_KB=$(ps -p "$PID" -o rss= | tr -d ' ')
MEM_MB=$((MEM_KB / 1024))
CPU=$(ps -p "$PID" -o %cpu= | tr -d ' ')
TIME=$(ps -p "$PID" -o time= | tr -d ' ')

echo "Current:"
echo "  Memory: ${MEM_MB} MB"
echo "  CPU: ${CPU}%"
echo "  CPU Time: ${TIME}"
echo ""

# Memory trend from log
if [ -f /tmp/memory_monitor.log ]; then
    echo "Recent Memory Trend (last 10 entries):"
    tail -10 /tmp/memory_monitor.log | awk -F',' '{printf "  %s: %s MB (CPU: %s%%)\n", $1, $2, $3}'
fi

echo ""
echo "Full log: tail -f /tmp/memory_monitor.log"

