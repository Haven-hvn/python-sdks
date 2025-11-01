#!/bin/bash
# Monitor memory usage of catalog_stream_types.py process

PID_FILE="/tmp/catalog_pid.txt"
LOG_FILE="/tmp/memory_monitor.log"

echo "Memory Monitor Started" > "$LOG_FILE"
echo "Timestamp,Memory_MB,CPU_Percent" >> "$LOG_FILE"

while true; do
    PID=$(pgrep -f catalog_stream_types.py | head -1)
    
    if [ -z "$PID" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S'),0,0" >> "$LOG_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') - No process found"
        sleep 5
        continue
    fi
    
    # Get memory in MB and CPU percentage
    MEM_KB=$(ps -p "$PID" -o rss= 2>/dev/null | tr -d ' ')
    MEM_MB=$((MEM_KB / 1024))
    CPU=$(ps -p "$PID" -o %cpu= 2>/dev/null | tr -d ' ')
    
    if [ -n "$MEM_KB" ] && [ -n "$CPU" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S'),${MEM_MB},${CPU}" >> "$LOG_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') - PID: $PID - Memory: ${MEM_MB} MB - CPU: ${CPU}%"
    fi
    
    sleep 5
done

