#!/bin/bash
start="2026-05-09"
end="2026-07-12"
PYTHON="/home/jbeale/birdnet/venv-birdnet/bin/python"

d="$start"
while [ "$(date -d "$d" +%Y%m%d)" -le "$(date -d "$end" +%Y%m%d)" ]; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Processing $d..."
    "$PYTHON" backfill_birdnet.py "$d"
    d=$(date -d "$d + 1 day" +%Y-%m-%d)
done
