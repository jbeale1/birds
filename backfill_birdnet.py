#!/usr/bin/env python3
"""Backfill BirdNET detections for a specific date into birdnet.db.
Reuses the same processing logic as ingest_birdnet.py, but targets
an explicit calendar date instead of 'recent segments near now'.
"""

import sys
import sqlite3
import subprocess
import csv
import argparse
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path("/home/jbeale/birdnet/birdnet.db")
SCHEMA_PATH = Path("/home/jbeale/birdnet/schema.sql")
OUTPUT_ROOT = Path("/home/jbeale/birdnet/db_out")

LAT, LON = 45.42, -122.67
MIN_CONF = 0.5
SEGMENT_MINUTES = 15

STATIONS = [
    {"station": "A", "dir": Path("/mnt/minix/Audio1"), "prefix": "ChA"},
    {"station": "B", "dir": Path("/mnt/minix/Audio2"), "prefix": "ChB"},
]


def birdnet_week(dt: datetime) -> int:
    week_in_month = min(4, (dt.day - 1) // 7 + 1)
    return (dt.month - 1) * 4 + week_in_month


def day_segment_starts(date: datetime):
    """All 96 segment-start times for a given calendar date."""
    n_segments = (24 * 60) // SEGMENT_MINUTES
    return [date + timedelta(minutes=SEGMENT_MINUTES * i) for i in range(n_segments)]


def flac_path(station_dir: Path, prefix: str, segment_start: datetime) -> Path:
    return station_dir / f"{prefix}_{segment_start.strftime('%Y-%m-%d_%H-%M-%S')}.flac"


def already_processed(conn, station: str, segment_start: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_segments WHERE station=? AND segment_start=?",
        (station, segment_start.isoformat()),
    ).fetchone()
    return row is not None


def run_analyzer(flac_file: Path, out_dir: Path, segment_start: datetime):
    out_dir.mkdir(parents=True, exist_ok=True)
    week = birdnet_week(segment_start)
    subprocess.run(
        [
            sys.executable, "-m", "birdnet_analyzer.analyze", str(flac_file),
            "-o", str(out_dir),
            "--lat", str(LAT), "--lon", str(LON), "--week", str(week),
            "--min_conf", str(MIN_CONF),
        ],
        check=True, capture_output=True, text=True,
    )


def parse_selection_table(txt_path: Path, segment_start: datetime):
    rows = []
    with open(txt_path, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            offset_s = float(row["Begin Time (s)"])
            rows.append({
                "species": row["Common Name"],
                "species_code": row["Species Code"],
                "confidence": float(row["Confidence"]),
                "offset_s": offset_s,
                "detected_at": (segment_start + timedelta(seconds=offset_s)).isoformat(),
            })
    return rows


def process_segment(conn, station: str, station_dir: Path, prefix: str, segment_start: datetime):
    flac_file = flac_path(station_dir, prefix, segment_start)
    if not flac_file.exists():
        return None  # no data for this station at this time — not an error

    out_dir = OUTPUT_ROOT / station / segment_start.strftime("%Y-%m-%d_%H-%M-%S")
    try:
        run_analyzer(flac_file, out_dir, segment_start)
    except subprocess.CalledProcessError as e:
        print(f"ERROR analyzing {flac_file}: {e.stderr}")
        return False

    txt_files = list(out_dir.glob("*.BirdNET.selection.table.txt"))
    if not txt_files:
        print(f"WARNING: no selection table produced for {flac_file}")
        return False

    detections = parse_selection_table(txt_files[0], segment_start)

    conn.execute(
        "INSERT INTO processed_segments (station, segment_start, processed_at, num_detections, output_dir) "
        "VALUES (?, ?, ?, ?, ?)",
        (station, segment_start.isoformat(), datetime.now().isoformat(),
         len(detections), str(out_dir)),
    )
    conn.executemany(
        "INSERT INTO detections (station, segment_start, species, species_code, confidence, offset_s, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(station, segment_start.isoformat(), d["species"], d["species_code"],
          d["confidence"], d["offset_s"], d["detected_at"]) for d in detections],
    )
    conn.commit()
    return True


def main():
    parser = argparse.ArgumentParser(description="Backfill BirdNET detections for a specific date.")
    parser.add_argument("date", help="Date to process, format YYYY-MM-DD")
    args = parser.parse_args()

    date = datetime.strptime(args.date, "%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript(SCHEMA_PATH.read_text())

    segment_starts = day_segment_starts(date)

    for st in STATIONS:
        processed = skipped = missing = failed = 0
        for segment_start in segment_starts:
            if already_processed(conn, st["station"], segment_start):
                skipped += 1
                continue
            result = process_segment(conn, st["station"], st["dir"], st["prefix"], segment_start)
            if result is None:
                missing += 1
            elif result is True:
                processed += 1
            else:
                failed += 1
        print(f"Station {st['station']}: processed={processed} skipped={skipped} "
              f"missing={missing} failed={failed}")

    conn.close()


if __name__ == "__main__":
    main()
