#!/usr/bin/env python3
"""bird_dashboard.py — interactive BirdNET detection browser.

Run:  python bird_dashboard.py [--db /path/to/birdnet.db] [--port 5000]
Then open http://localhost:5000 (or http://<host-ip>:5000 from another machine).
"""

import argparse
import io
import json
import re
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template_string, send_from_directory

app = Flask(__name__)

DB_PATH = "/home/jbeale/birdnet/birdnet.db"

STATIONS = {
    "A": {"dir": Path("/mnt/minix/Audio1"), "prefix": "ChA"},
    "B": {"dir": Path("/mnt/minix/Audio2"), "prefix": "ChB"},
}

MIN_COUNT = 5             # same minimum-detections rule as species_summary.sh
DEFAULT_CLIP_LENGTH_S = 3.0
DEFAULT_GAIN_DB = 35

VERSION = "1.14.0"

# Species that repeatedly get high-confidence hits but have a known,
# specific non-bird (or wrong-bird) cause. Shown in a separate section
# at the bottom of the species list rather than mixed in with real
# detections, but still fully clickable for charts/audio review.
MISCLASSIFICATION_NOTES = {
    "Snow Goose": "construction noise",
    "Common Raven": "domestic chickens",
    "Ring-necked Pheasant": "domestic chickens",
    "Eurasian Collared-Dove": "Barred Owl ('who cooks for you')",
    "Wild Turkey": "construction noise",
    "Mallard": "distant distorted noise",
    "Black-crowned Night-Heron": "nonspecific too-short fragments",
    "Rough-legged Hawk": "squirrel scolding",
}

# ---------- Local species-image cache ----------
# BASE_DIR: same directory as this script, so the cache travels with it.
BASE_DIR = Path(__file__).resolve().parent
IMAGE_CACHE_DIR = BASE_DIR / "species_images"
IMAGE_DOWNLOAD_DIR = IMAGE_CACHE_DIR / "cache"      # auto-downloaded thumbnails
IMAGE_MANUAL_DIR = IMAGE_CACHE_DIR / "manual"       # drop your own images here
MANIFEST_PATH = IMAGE_CACHE_DIR / "manifest.json"

IMAGE_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_MANUAL_DIR.mkdir(parents=True, exist_ok=True)


def safe_species_filename(species: str) -> str:
    """Turn a species name into a filesystem-safe base filename,
    e.g. 'Wild Turkey' -> 'Wild_Turkey'. Used both for the auto-download
    cache and to know what to name a manually-supplied image."""
    return re.sub(r"[^A-Za-z0-9]+", "_", species).strip("_")


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_manifest():
    MANIFEST_PATH.write_text(json.dumps(_image_cache, indent=2))


def find_manual_image(species: str):
    """Return a Path to a manually-supplied image for this species, if one
    exists in species_images/manual/, else None. Checked live on every
    request (cheap glob) so you can drop in or swap a file anytime without
    restarting the server."""
    base = safe_species_filename(species)
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        candidate = IMAGE_MANUAL_DIR / f"{base}{ext}"
        if candidate.exists():
            return candidate
    return None


# Loaded once at startup; persisted back to manifest.json on every new
# auto-download so results (including "no image found") survive restarts.
_image_cache = load_manifest()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def flac_path(station: str, segment_start: str) -> Path:
    dt = datetime.fromisoformat(segment_start)
    cfg = STATIONS[station]
    return cfg["dir"] / f"{cfg['prefix']}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.flac"


def date_filter_clause(args):
    """Build an optional SQL clause + params restricting detected_at to
    [start_date, end_date] (inclusive), both YYYY-MM-DD strings. Either or
    both may be absent, meaning no bound on that side."""
    start = args.get("start_date")
    end = args.get("end_date")
    clauses = []
    params = []
    if start:
        clauses.append("date(detected_at) >= ?")
        params.append(start)
    if end:
        clauses.append("date(detected_at) <= ?")
        params.append(end)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


# A few species names don't match their actual Wikipedia article title
# (capitalization disputes, disambiguation pages, etc). "Wild Turkey" is
# the classic case: the title-case page is a disambiguation page (the
# bourbon brand also uses that exact capitalization), while the real bird
# article lives at lowercase "Wild turkey". Add entries here as needed.
WIKI_TITLE_OVERRIDES = {
    "Wild Turkey": "Wild turkey",
}


def fetch_species_thumbnail(species: str) -> dict:
    # 1. Manual override always wins, and is checked live (not cached in
    #    memory) so you can add/replace a file at any time without a
    #    restart — e.g. species_images/manual/Wild_Turkey.jpg
    manual = find_manual_image(species)
    if manual:
        return {
            "thumbnail": f"/species_image_file/manual/{manual.name}",
            "page_url": None,
            "source": "manual",
        }

    # 2. Permanent on-disk cache (loaded from manifest.json at startup).
    #    This includes species with NO available image (thumbnail: null),
    #    so we don't re-query Wikipedia for known-missing ones like
    #    Wild Turkey on every restart.
    if species in _image_cache:
        return _image_cache[species]

    # 3. Not seen before — fetch from Wikipedia and persist locally.
    wiki_name = WIKI_TITLE_OVERRIDES.get(species, species)
    title = urllib.parse.quote(wiki_name.replace(" ", "_"))
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "BirdNET-Dashboard/1.0 (personal backyard monitoring project)"},
    )
    result = {"thumbnail": None, "page_url": None, "source": "wikipedia"}
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        remote_thumb = data.get("thumbnail", {}).get("source")
        result["page_url"] = data.get("content_urls", {}).get("desktop", {}).get("page")

        if remote_thumb:
            # Download the actual image bytes so future loads never need
            # to hit Wikipedia again, even for the image itself.
            ext = Path(urllib.parse.urlparse(remote_thumb).path).suffix or ".jpg"
            local_name = f"{safe_species_filename(species)}{ext}"
            local_path = IMAGE_DOWNLOAD_DIR / local_name
            img_req = urllib.request.Request(
                remote_thumb,
                headers={"User-Agent": "BirdNET-Dashboard/1.0 (personal backyard monitoring project)"},
            )
            with urllib.request.urlopen(img_req, timeout=10) as img_resp:
                local_path.write_bytes(img_resp.read())
            result["thumbnail"] = f"/species_image_file/cache/{local_name}"
    except Exception:
        pass  # leave thumbnail as None — species just won't show an image

    _image_cache[species] = result
    save_manifest()
    return result


# ---------- API ----------

@app.route("/api/species")
def api_species():
    min_conf = float(request.args.get("min_conf", 0.85))
    date_sql, date_params = date_filter_clause(request.args)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT species,
               MAX(species_code) AS species_code,
               SUM(CASE WHEN station='A' THEN 1 ELSE 0 END) AS n_back,
               SUM(CASE WHEN station='B' THEN 1 ELSE 0 END) AS n_front,
               MAX(confidence) AS max_conf
        FROM detections
        WHERE species != 'nocall' AND confidence > ?{date_sql}
        GROUP BY species
        HAVING COUNT(*) >= ?
        ORDER BY (n_back + n_front) DESC
    """, (min_conf, *date_params, MIN_COUNT)).fetchall()

    # SQLite has no built-in MEDIAN(); this is the standard window-function
    # trick — rank each species' confidences, then average the middle one
    # (odd count) or middle two (even count). Computed over the same
    # confidence > min_conf population as everything else here, so it
    # reflects "median among those that cleared the current threshold."
    median_rows = conn.execute(f"""
        WITH filtered AS (
            SELECT species, confidence,
                   ROW_NUMBER() OVER (PARTITION BY species ORDER BY confidence) AS rn,
                   COUNT(*) OVER (PARTITION BY species) AS cnt
            FROM detections
            WHERE species != 'nocall' AND confidence > ?{date_sql}
        )
        SELECT species, AVG(confidence) AS median_conf
        FROM filtered
        WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
        GROUP BY species
    """, (min_conf, *date_params)).fetchall()
    median_map = {r["species"]: r["median_conf"] for r in median_rows}

    # Per-species, per-day breakdown (both stations combined) — used to
    # derive "days detected" and "date with most detections" for export.
    day_rows = conn.execute(f"""
        SELECT species, date(detected_at) AS d, COUNT(*) AS n
        FROM detections
        WHERE species != 'nocall' AND confidence > ?{date_sql}
        GROUP BY species, d
    """, (min_conf, *date_params)).fetchall()
    day_stats = {}
    for r in day_rows:
        sp = r["species"]
        stat = day_stats.setdefault(sp, {"days_detected": 0, "top_day": None, "top_day_count": 0})
        stat["days_detected"] += 1
        if r["n"] > stat["top_day_count"]:
            stat["top_day_count"] = r["n"]
            stat["top_day"] = r["d"]

    conn.close()

    species_list = []
    flagged_list = []
    for r in rows:
        d = dict(r)
        d["median_conf"] = median_map.get(d["species"])
        stat = day_stats.get(d["species"], {"days_detected": 0, "top_day": None, "top_day_count": 0})
        d["days_detected"] = stat["days_detected"]
        d["top_day"] = stat["top_day"]
        d["top_day_count"] = stat["top_day_count"]
        note = MISCLASSIFICATION_NOTES.get(d["species"])
        if note:
            d["note"] = note
            flagged_list.append(d)
        else:
            species_list.append(d)

    return jsonify({"species": species_list, "flagged": flagged_list})


@app.route("/api/hourly")
def api_hourly():
    species = request.args.get("species")
    min_conf = float(request.args.get("min_conf", 0.85))
    date_sql, date_params = date_filter_clause(request.args)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT strftime('%H', detected_at) AS hr, station, COUNT(*) AS n
        FROM detections
        WHERE species = ? AND confidence > ?{date_sql}
        GROUP BY hr, station
    """, (species, min_conf, *date_params)).fetchall()

    # Earliest/latest clock time of day observed (ignores the date, just
    # the time-of-day component), for labeling the hourly chart's x axis.
    time_bounds = conn.execute(f"""
        SELECT MIN(strftime('%H:%M', detected_at)) AS earliest,
               MAX(strftime('%H:%M', detected_at)) AS latest
        FROM detections
        WHERE species = ? AND confidence > ?{date_sql}
    """, (species, min_conf, *date_params)).fetchone()
    conn.close()

    hours_a = [0] * 24
    hours_b = [0] * 24
    for r in rows:
        h = int(r["hr"])
        if r["station"] == "A":
            hours_a[h] = r["n"]
        elif r["station"] == "B":
            hours_b[h] = r["n"]
    return jsonify({
        "back": hours_a,
        "front": hours_b,
        "earliest_time": time_bounds["earliest"],
        "latest_time": time_bounds["latest"],
    })


@app.route("/api/top_clips")
def api_top_clips():
    species = request.args.get("species")
    min_conf = float(request.args.get("min_conf", 0.85))
    limit = int(request.args.get("limit", 6))
    date_sql, date_params = date_filter_clause(request.args)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT id, station, segment_start, offset_s, detected_at, confidence
        FROM detections
        WHERE species = ? AND confidence > ?{date_sql}
        ORDER BY confidence DESC
        LIMIT ?
    """, (species, min_conf, *date_params, limit)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/daily")
def api_daily():
    """Per-day detection counts by station for one species, within the
    currently selected date range. If no explicit start/end is given,
    falls back to the full span of dates present in the data, so the
    frontend always knows the bounds to bucket against."""
    species = request.args.get("species")
    min_conf = float(request.args.get("min_conf", 0.85))
    date_sql, date_params = date_filter_clause(request.args)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT date(detected_at) AS d, station, COUNT(*) AS n
        FROM detections
        WHERE species = ? AND confidence > ?{date_sql}
        GROUP BY d, station
        ORDER BY d
    """, (species, min_conf, *date_params)).fetchall()

    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        bounds = conn.execute(
            "SELECT MIN(date(detected_at)) AS lo, MAX(date(detected_at)) AS hi "
            "FROM detections WHERE species != 'nocall'"
        ).fetchone()
        start = start or bounds["lo"]
        end = end or bounds["hi"]
    conn.close()

    counts = {}
    for r in rows:
        day = counts.setdefault(r["d"], {"A": 0, "B": 0})
        day[r["station"]] = r["n"]

    return jsonify({"counts": counts, "start_date": start, "end_date": end})


@app.route("/api/dates")
def api_dates():
    """Per-day detection counts, used to render the calendar heatmap and
    to bound the date-range picker. Ignores min_conf so the calendar
    doesn't jump around as the confidence slider moves."""
    conn = get_db()
    rows = conn.execute("""
        SELECT date(detected_at) AS d, COUNT(*) AS n
        FROM detections
        WHERE species != 'nocall'
        GROUP BY d
        ORDER BY d
    """).fetchall()
    conn.close()
    counts = {r["d"]: r["n"] for r in rows}
    min_date = rows[0]["d"] if rows else None
    max_date = rows[-1]["d"] if rows else None
    return jsonify({"counts": counts, "min_date": min_date, "max_date": max_date})


@app.route("/api/species_image")
def api_species_image():
    species = request.args.get("species", "")
    if not species:
        return jsonify({"thumbnail": None, "page_url": None})
    return jsonify(fetch_species_thumbnail(species))


@app.route("/species_image_file/<subdir>/<path:filename>")
def species_image_file(subdir, filename):
    if subdir == "manual":
        directory = IMAGE_MANUAL_DIR
    elif subdir == "cache":
        directory = IMAGE_DOWNLOAD_DIR
    else:
        return "Not found", 404
    return send_from_directory(directory, filename)


def extract_clip_wav(detection_id, gain, length, lead_in=0.0):
    """Extract a trimmed, gain-boosted WAV clip for one detection.
    'lead_in' shifts the start earlier by that many seconds (clamped to
    not go before 0 within the source file) and extends the total length
    by the same amount, so the originally-visible content isn't lost.
    Returns (wav_bytes, error_message, status_code). On success,
    error_message is None."""
    conn = get_db()
    row = conn.execute(
        "SELECT station, segment_start, offset_s FROM detections WHERE id=?",
        (detection_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None, "Detection not found", 404

    src = flac_path(row["station"], row["segment_start"])
    if not src.exists():
        return None, f"Source file not found: {src}", 404

    lead_in = max(0.0, float(lead_in))
    start = max(0.0, float(row["offset_s"]) - lead_in)
    total_length = float(length) + lead_in

    try:
        result = subprocess.run(
            [
                "sox", str(src), "-t", "wav", "-",
                "trim", str(start), str(total_length),
                "vol", f"{gain}dB",
            ],
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        return None, f"sox error: {e.stderr.decode(errors='replace')}", 500

    return result.stdout, None, 200


def compute_stft(y, n_fft=2048, hop=256):
    import numpy as np
    window = np.hanning(n_fft)
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    n_frames = 1 + (len(y) - n_fft) // hop
    frames = np.stack([y[i * hop:i * hop + n_fft] * window for i in range(n_frames)])
    spec = np.fft.rfft(frames, axis=1)
    return spec.T  # (freq_bins, time_frames)


def render_spectrogram(wav_bytes, scale="log", title=""):
    """Render a spectrogram PNG from WAV bytes entirely in memory.
    Uses only numpy + matplotlib + the stdlib 'wave' module — deliberately
    avoids librosa/soundfile so it works even if those aren't installed.
    Returns PNG bytes, or None if numpy/matplotlib aren't available."""
    try:
        import wave
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sampwidth, np.int16)
    data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    data = data / float(np.iinfo(dtype).max)

    n_fft, hop = 2048, 256
    spec = compute_stft(data, n_fft, hop)
    mag = np.abs(spec)
    db = 20 * np.log10(np.maximum(mag, 1e-6))
    db -= db.max()

    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    times = np.arange(db.shape[1]) * hop / sr

    # 12 kHz top matches the convention used by Cornell's Macaulay Library
    # online spectrogram displays. Never exceed Nyquist for low sample rates.
    freq_top = min(12000, sr / 2)
    freq_bottom_log = 100  # little meaningful content below this

    fig, ax = plt.subplots(figsize=(8, 5))
    pcm = ax.pcolormesh(times, freqs, db, shading="auto", cmap="magma", vmin=-80, vmax=0)
    if scale == "log":
        ax.set_yscale("log")
        ax.set_ylim(freq_bottom_log, freq_top)
    else:
        ax.set_ylim(0, freq_top)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_title(title)
    fig.colorbar(pcm, ax=ax, label="dB")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


@app.route("/api/clip/<int:detection_id>")
def api_clip(detection_id):
    gain = request.args.get("gain", str(DEFAULT_GAIN_DB))
    length = request.args.get("length", str(DEFAULT_CLIP_LENGTH_S))
    lead_in = request.args.get("lead_in", "0")
    wav_bytes, err, status = extract_clip_wav(detection_id, gain, length, lead_in)
    if err:
        return err, status

    return Response(wav_bytes, mimetype="audio/wav")


@app.route("/api/spectrogram/<int:detection_id>")
def api_spectrogram(detection_id):
    gain = request.args.get("gain", str(DEFAULT_GAIN_DB))
    length = request.args.get("length", str(DEFAULT_CLIP_LENGTH_S))
    lead_in = request.args.get("lead_in", "0")
    scale = request.args.get("scale", "log")
    if scale not in ("log", "linear"):
        scale = "log"

    wav_bytes, err, status = extract_clip_wav(detection_id, gain, length, lead_in)
    if err:
        return err, status

    conn = get_db()
    row = conn.execute(
        "SELECT species, station, detected_at, confidence FROM detections WHERE id=?",
        (detection_id,),
    ).fetchone()
    conn.close()
    title = f"{row['species']}  \u2014  Station {row['station']}  {row['detected_at']}  conf {row['confidence']:.3f}" if row else ""

    png_bytes = render_spectrogram(wav_bytes, scale=scale, title=title)
    if png_bytes is None:
        return ("Spectrogram generation requires numpy and matplotlib, which "
                "aren't available in this Python environment. "
                "Try: pip install numpy matplotlib"), 501

    return Response(png_bytes, mimetype="image/png")


# ---------- Frontend ----------

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Timberline Birds</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  body { font-family: sans-serif; margin: 20px; }

  .page-header { display: flex; justify-content: space-between; align-items: flex-start; }
  .page-header h2 { margin: 0; }
  .subtitle { font-size: 0.5em; font-weight: normal; }
  .version { font-size: 0.75em; color: #999; white-space: nowrap; margin-top: 4px; }

  .top-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; margin-bottom: 15px; }

  .species-thumb { width: 150px; text-align: center; flex-shrink: 0; }
  .species-thumb img {
    width: 100%;
    border-radius: 6px;
    box-shadow: 0 1px 5px rgba(0,0,0,0.35);
    display: block;
  }
  .species-thumb a {
    font-size: 11px;
    color: #666;
    text-decoration: none;
    display: block;
    margin-top: 4px;
  }
  .species-thumb a:hover { text-decoration: underline; }
  .species-thumb .thumb-caption {
    font-size: 12px;
    font-weight: bold;
    color: #333;
    margin-top: 4px;
  }

  .controls-stacked { display: flex; flex-direction: column; gap: 10px; margin-bottom: 18px; max-width: 320px; }
  .controls-stacked label { display: block; }
  .controls-bottom { margin-top: 20px; margin-bottom: 0; padding-top: 14px; border-top: 1px solid #eee; }
  .export-row { margin-top: 12px; display: flex; gap: 8px; }
  .export-row button { cursor: pointer; font-size: 0.8em; padding: 4px 10px; }

  .layout { display: flex; gap: 30px; align-items: flex-start; }

  .left { flex: 0 0 460px; }
  .left table { border-collapse: collapse; width: 100%; }
  .left th, .left td { text-align: left; padding: 4px 10px; border-bottom: 1px solid #ddd; }
  .left tr.species-row { cursor: pointer; }
  .left tr.species-row:hover { background: #f0f0f0; }
  .left tr.species-row.selected { background: #dbe9ff; }
  .left-scroll { max-height: 85vh; overflow-y: auto; border: 1px solid #eee; }
  .left thead th { position: sticky; top: 0; background: #fff; }

  .right { flex: 1 1 auto; min-width: 400px; position: sticky; top: 20px; }
  .clip-group { border: 1px solid #ddd; border-radius: 8px; padding: 10px 12px; margin: 10px 0; background: #fff; }
  .clip-group:hover { border-color: #bbb; }
  .clip { display: flex; align-items: center; gap: 10px; margin: 0; flex-wrap: wrap; }
  .clip span { width: 220px; font-family: monospace; }
  .clip select, .clip button.spec-btn { font-size: 0.8em; cursor: pointer; }
  .spec-panel { display: none; margin: 10px 0 0 0; padding: 8px; background: #fafafa; border: 1px solid #eee; border-radius: 6px; }
  .spec-panel.visible { display: block; }
  .spec-img { max-width: 100%; display: block; }
  .spec-save-link { font-size: 0.8em; color: #2563eb; text-decoration: none; }
  .spec-save-link:hover { text-decoration: underline; }
  .wav-save-link { font-size: 0.8em; color: #2563eb; text-decoration: none; }
  .wav-save-link:hover { text-decoration: underline; }
  canvas { max-width: 100%; }

  /* --- flagged / likely-misclassified species --- */
  .flagged-section { margin-top: 14px; }
  .flagged-section h4 { margin: 0 0 4px 0; font-size: 0.8em; color: #a15c00; }
  .flagged-section table { border-collapse: collapse; width: 100%; }
  .flagged-section th, .flagged-section td { text-align: left; padding: 3px 8px; border-bottom: 1px solid #f0e0c0; font-size: 0.85em; }
  .flagged-section tr.species-row { cursor: pointer; }
  .flagged-section tr.species-row:hover { background: #fff6e6; }
  .flagged-section tr.species-row.selected { background: #ffe8b8; }
  .flagged-section .note { color: #a15c00; font-style: italic; }

  /* --- date range / calendar (up to 4 months side by side) --- */
  .daterange { border: 1px solid #eee; padding: 8px 10px; border-radius: 6px; flex: 1 1 auto; min-width: 0; }
  .daterange-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px; }
  .daterange-header button { cursor: pointer; }
  .daterange-status { font-size: 0.7em; color: #444; margin-bottom: 6px; min-height: 1.2em; }
  .daterange-status a { cursor: pointer; color: #2563eb; }
  .cal-multi { display: flex; gap: 14px; }
  .cal-month-block { flex: 1 1 0; min-width: 0; }
  .cal-month-name { font-size: 0.62em; text-align: center; font-weight: bold; color: #555; margin-bottom: 2px; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 1.5px; }
  .cal-dow { font-size: 0.55em; text-align: center; color: #888; }
  /* Outer cell: transparent by default. Its background only ever shows
     in the padding gap around cal-day-fill, so it's free to use for a
     species-match frame without ever competing with the heat coloring. */
  .cal-day {
    aspect-ratio: 1 / 1;
    padding: 3px;
    box-sizing: border-box;
    border-radius: 6px;
    cursor: pointer;
    user-select: none;
    background: transparent;
  }
  .cal-day.empty { visibility: hidden; cursor: default; padding: 0; }
  .cal-day-fill {
    width: 100%; height: 100%;
    display: flex; align-items: center; justify-content: center;
    border-radius: 3px;
    font-size: 9px;
    background: #f4f4f4; color: #bbb;
  }
  .cal-day-fill.has-data { color: #333; }
  .cal-day:hover .cal-day-fill.has-data { outline: 2px solid #999; }
  .cal-day-fill.in-range { background: #bcd6ff !important; color: #111; }
  .cal-day-fill.range-end { background: #2563eb !important; color: #fff; }
  /* heat levels (background intensity for data density) */
  .cal-day-fill.heat-1 { background: #d9ecff; }
  .cal-day-fill.heat-2 { background: #a9d1ff; }
  .cal-day-fill.heat-3 { background: #6fb1ff; }
  .cal-day-fill.heat-4 { background: #3f8cff; color: #fff; }
  /* Species-match marker: a solid amber frame filling the outer cell's
     padding gap, clearly separated from (and contrasting with) the blue
     heat-color fill inside it — orange/blue are complementary colors,
     so this reads far more distinctly than a thin same-tone outline. */
  .cal-day.species-match { background: #f59e0b; }
  .cal-day.species-match .cal-day-fill { font-weight: 700; }
</style>
</head>
<body>

<div class="page-header">
  <h2>Timberline Birds <span class="subtitle">by John</span></h2>
  <div class="version">v{{ version }}</div>
</div>

<div class="top-row">
  <div class="daterange">
    <div class="daterange-header">
      <button id="calPrev">&laquo;</button>
      <strong id="calRangeLabel"></strong>
      <button id="calNext">&raquo;</button>
    </div>
    <div class="daterange-status" id="calStatus">All dates</div>
    <div class="cal-multi" id="calMulti"></div>
  </div>

  <div id="speciesThumb" class="species-thumb"></div>
</div>

<div class="layout">
  <div class="left">
    <div class="left-scroll">
      <table id="speciesTable">
        <thead>
          <tr><th>#</th><th>Species</th><th>Back (A)</th><th>Front (B)</th><th>Median</th><th>Max conf</th></tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="flagged-section">
      <h4>Likely misclassifications (excluded above)</h4>
      <table id="flaggedTable">
        <thead>
          <tr><th>Species</th><th>Back (A)</th><th>Front (B)</th><th>Median</th><th>Max conf</th><th>Likely cause</th></tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="export-row">
      <button id="exportTextBtn">Save as text (aligned)</button>
      <button id="exportCsvBtn">Save as CSV</button>
    </div>

    <div class="controls-stacked controls-bottom">
      <label>Min confidence: <span id="confVal">0.85</span>
        <input type="range" id="confSlider" min="0.6" max="1.0" step="0.01" value="0.85">
      </label>
    </div>
  </div>

  <div class="right">
    <div id="detail">
      <p>Click a species on the left to see its hourly pattern and hear clips.</p>
    </div>
    <div class="controls-stacked controls-bottom">
      <label>Clip gain (dB): <span id="gainVal">35</span>
        <input type="range" id="gainSlider" min="10" max="60" step="1" value="35">
      </label>
      <label>Clip length (s): <span id="lengthVal">3</span>
        <input type="range" id="lengthSlider" min="1" max="10" step="0.5" value="3">
      </label>
      <label>Lead-in (s): <span id="leadInVal">0</span>
        <input type="range" id="leadInSlider" min="0" max="3" step="0.5" value="0">
      </label>
    </div>
  </div>
</div>

<script>
const confSlider = document.getElementById('confSlider');
const gainSlider = document.getElementById('gainSlider');
const lengthSlider = document.getElementById('lengthSlider');
const leadInSlider = document.getElementById('leadInSlider');
const confVal = document.getElementById('confVal');
const gainVal = document.getElementById('gainVal');
const lengthVal = document.getElementById('lengthVal');
const leadInVal = document.getElementById('leadInVal');
let currentSpecies = null;
let chart = null;
let dailyChart = null;
let lastSpeciesData = null;   // most recent {species, flagged} response, for export

// ---- date range state ----
let dateCounts = {};      // "YYYY-MM-DD" -> count
let dataMinDate = null;
let dataMaxDate = null;
let rangeStart = null;    // "YYYY-MM-DD" or null
let rangeEnd = null;      // "YYYY-MM-DD" or null
let calYear, calMonth;    // currently displayed month (0-indexed month)

// Days the currently-selected species was heard on, across the WHOLE
// dataset (not limited to the current date-range filter), so the
// calendar marks matching days no matter which months you page to.
let speciesMatchDays = new Set();

confSlider.addEventListener('input', () => {
  confVal.textContent = confSlider.value;
  loadSpecies();
  if (currentSpecies) loadDetail(currentSpecies);
});
gainSlider.addEventListener('input', () => {
  gainVal.textContent = gainSlider.value;
});
lengthSlider.addEventListener('input', () => {
  lengthVal.textContent = lengthSlider.value;
});
leadInSlider.addEventListener('input', () => {
  leadInVal.textContent = leadInSlider.value;
});

function dateParams() {
  const p = new URLSearchParams();
  if (rangeStart) p.set('start_date', rangeStart);
  if (rangeEnd) p.set('end_date', rangeEnd);
  return p;
}

function loadSpecies() {
  const p = dateParams();
  p.set('min_conf', confSlider.value);
  fetch(`/api/species?${p.toString()}`)
    .then(r => r.json())
    .then(data => {
      lastSpeciesData = data;

      const tbody = document.querySelector('#speciesTable tbody');
      tbody.innerHTML = '';
      data.species.forEach((row, i) => {
        const tr = document.createElement('tr');
        tr.className = 'species-row';
        if (row.species === currentSpecies) tr.classList.add('selected');
        tr.innerHTML = `<td>${i + 1}</td><td>${row.species}</td>` +
          `<td>${row.n_back}</td><td>${row.n_front}</td>` +
          `<td>${row.median_conf.toFixed(3)}</td>` +
          `<td>${row.max_conf.toFixed(3)}</td>`;
        tr.addEventListener('click', () => loadDetail(row.species));
        tbody.appendChild(tr);
      });

      const flaggedTbody = document.querySelector('#flaggedTable tbody');
      flaggedTbody.innerHTML = '';
      data.flagged.forEach(row => {
        const tr = document.createElement('tr');
        tr.className = 'species-row';
        if (row.species === currentSpecies) tr.classList.add('selected');
        tr.innerHTML = `<td>${row.species}</td>` +
          `<td>${row.n_back}</td><td>${row.n_front}</td>` +
          `<td>${row.median_conf.toFixed(3)}</td>` +
          `<td>${row.max_conf.toFixed(3)}</td>` +
          `<td class="note">${row.note}</td>`;
        tr.addEventListener('click', () => loadDetail(row.species));
        flaggedTbody.appendChild(tr);
      });
    });
}

// ---- export species table to a local file ----
function currentFilterDescription() {
  const conf = confSlider.value;
  const daysWithData = countSelectedDays();
  let range;
  if (!rangeStart) {
    range = `${dataMinDate} to ${dataMaxDate}  (${daysWithData} days with data)`;
  } else if (!rangeEnd) {
    range = `${rangeStart} onward  (${daysWithData} days with data)`;
  } else {
    range = `${rangeStart} to ${rangeEnd}  (${daysWithData} days with data)`;
  }
  return `Min confidence: ${conf}   Date range: ${range}`;
}

function padCell(text, width) {
  return String(text).padEnd(width);
}

function buildAlignedTable(headers, rows) {
  if (rows.length === 0) return '(none)';
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map(r => String(r[i]).length))
  );
  const headerLine = headers.map((h, i) => padCell(h, widths[i])).join('  ');
  const sepLine = widths.map(w => '-'.repeat(w)).join('  ');
  const bodyLines = rows.map(r => r.map((c, i) => padCell(c, widths[i])).join('  '));
  return [headerLine, sepLine, ...bodyLines].join('\\n');
}

function csvEscape(v) {
  const s = String(v);
  if (/[",\\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function buildCsvTable(headers, rows) {
  const lines = [headers.map(csvEscape).join(',')];
  rows.forEach(r => lines.push(r.map(csvEscape).join(',')));
  return lines.join('\\r\\n');
}

function topDayText(row) {
  return row.top_day ? `${row.top_day} (${row.top_day_count})` : 'n/a';
}

function speciesRowsForExport(list) {
  const daysWithData = countSelectedDays();
  return list.map((row, i) => [
    i + 1, row.species, row.n_back, row.n_front,
    row.median_conf.toFixed(3), row.max_conf.toFixed(3),
    row.days_detected, (row.days_detected / daysWithData).toFixed(3),
    topDayText(row),
  ]);
}

function flaggedRowsForExport(list) {
  const daysWithData = countSelectedDays();
  return list.map(row => [
    row.species, row.n_back, row.n_front,
    row.median_conf.toFixed(3), row.max_conf.toFixed(3),
    row.days_detected, (row.days_detected / daysWithData).toFixed(3),
    topDayText(row), row.note,
  ]);
}

function downloadFile(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function exportAsText() {
  if (!lastSpeciesData) return;
  const stamp = new Date().toISOString().slice(0, 16).replace('T', ' ');
  const parts = [
    `Timberline Birds \u2014 species summary \u2014 generated ${stamp}`,
    currentFilterDescription(),
    '',
    buildAlignedTable(
      ['#', 'Species', 'Back(A)', 'Front(B)', 'Median', 'MaxConf', 'DaysDet', 'Fraction', 'TopDay'],
      speciesRowsForExport(lastSpeciesData.species)
    ),
  ];
  if (lastSpeciesData.flagged.length > 0) {
    parts.push('', 'Likely misclassifications (excluded above):', '');
    parts.push(buildAlignedTable(
      ['Species', 'Back(A)', 'Front(B)', 'Median', 'MaxConf', 'DaysDet', 'Fraction', 'TopDay', 'Likely cause'],
      flaggedRowsForExport(lastSpeciesData.flagged)
    ));
  }
  downloadFile('bird_species_summary.txt', parts.join('\\n') + '\\n', 'text/plain');
}

function exportAsCsv() {
  if (!lastSpeciesData) return;
  const parts = [
    buildCsvTable(
      ['#', 'Species', 'Back(A)', 'Front(B)', 'Median', 'MaxConf', 'DaysDetected', 'Fraction', 'TopDay'],
      speciesRowsForExport(lastSpeciesData.species)
    ),
  ];
  if (lastSpeciesData.flagged.length > 0) {
    parts.push('');
    parts.push('Likely misclassifications (excluded above)');
    parts.push(buildCsvTable(
      ['Species', 'Back(A)', 'Front(B)', 'Median', 'MaxConf', 'DaysDetected', 'Fraction', 'TopDay', 'Likely cause'],
      flaggedRowsForExport(lastSpeciesData.flagged)
    ));
  }
  downloadFile('bird_species_summary.csv', parts.join('\\r\\n'), 'text/csv');
}

document.getElementById('exportTextBtn').addEventListener('click', exportAsText);
document.getElementById('exportCsvBtn').addEventListener('click', exportAsCsv);

function loadSpeciesThumb(species) {
  const thumbDiv = document.getElementById('speciesThumb');
  // Show the name immediately; add the image (if any) once fetched.
  thumbDiv.innerHTML = `<div class="thumb-caption">${species}</div>`;

  fetch(`/api/species_image?species=${encodeURIComponent(species)}`)
    .then(r => r.json())
    .then(data => {
      if (data.thumbnail) {
        thumbDiv.innerHTML = `<a href="${data.page_url || '#'}" target="_blank" rel="noopener">
          <img src="${data.thumbnail}" alt="${species}">
        </a>
        <div class="thumb-caption">${species}</div>`;
      }
    })
    .catch(() => { /* fail silently — thumbnail is a nice-to-have */ });
}

function loadDetail(species) {
  currentSpecies = species;

  document.querySelectorAll('#speciesTable tr.species-row').forEach(tr => {
    tr.classList.toggle('selected', tr.children[1].textContent === species);
  });
  document.querySelectorAll('#flaggedTable tr.species-row').forEach(tr => {
    tr.classList.toggle('selected', tr.children[0].textContent === species);
  });

  loadSpeciesThumb(species);

  const detail = document.getElementById('detail');
  detail.innerHTML = `<h3>${species}</h3>
    <canvas id="hourlyChart" height="90"></canvas>
    <div id="hourlyTotal" style="font-size:0.85em; color:#444; margin-top:4px;"></div>
    <h4>Detections per day</h4>
    <canvas id="dailyChart" height="90"></canvas>
    <div id="dailyTotal" style="font-size:0.85em; color:#444; margin-top:4px;"></div>
    <h4>Audio examples</h4>
    <div id="clipList">Loading...</div>`;

  const hp = dateParams();
  hp.set('species', species);
  hp.set('min_conf', confSlider.value);
  fetch(`/api/hourly?${hp.toString()}`)
    .then(r => r.json())
    .then(data => {
      const ctx = document.getElementById('hourlyChart');
      if (chart) chart.destroy();
      const xTitle = (data.earliest_time && data.latest_time)
        ? `Hour of day  (${data.earliest_time}\u2013${data.latest_time} observed)`
        : 'Hour of day';
      chart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: [...Array(24).keys()].map(h => String(h).padStart(2, '0')),
          datasets: [
            { label: 'Back yard (A)', data: data.back, backgroundColor: 'rgba(54,162,235,0.7)' },
            { label: 'Front yard (B)', data: data.front, backgroundColor: 'rgba(255,159,64,0.7)' },
          ]
        },
        options: {
          responsive: true,
          scales: { x: { title: { display: true, text: xTitle } },
                    y: { title: { display: true, text: 'Detections' }, beginAtZero: true,
                         ticks: { precision: 0 } } }
        }
      });

      const sumBack = data.back.reduce((a, b) => a + b, 0);
      const sumFront = data.front.reduce((a, b) => a + b, 0);
      document.getElementById('hourlyTotal').textContent =
        `Total: ${sumBack + sumFront}  (Back A: ${sumBack}, Front B: ${sumFront})`;
    });

  loadDailyChart(species);
  loadSpeciesCalendarMarks(species);
  loadClips(species);
}

function loadClips(species) {
  const cp = dateParams();
  cp.set('species', species);
  cp.set('min_conf', confSlider.value);
  cp.set('limit', 6);
  fetch(`/api/top_clips?${cp.toString()}`)
    .then(r => r.json())
    .then(data => {
      const div = document.getElementById('clipList');
      div.innerHTML = '';
      if (data.length === 0) { div.textContent = 'No clips.'; return; }
      data.forEach(c => {
        const group = document.createElement('div');
        group.className = 'clip-group';

        const row = document.createElement('div');
        row.className = 'clip';

        const span = document.createElement('span');
        span.textContent = `${c.station} \u2014 ${c.detected_at} \u2014 conf ${c.confidence.toFixed(3)}`;
        row.appendChild(span);

        const audio = document.createElement('audio');
        audio.controls = true;
        audio.preload = 'none';
        const clipUrl = `/api/clip/${c.id}?gain=${gainSlider.value}&length=${lengthSlider.value}&lead_in=${leadInSlider.value}`;
        audio.src = clipUrl;
        row.appendChild(audio);

        const saveWavLink = document.createElement('a');
        saveWavLink.className = 'wav-save-link';
        saveWavLink.textContent = 'Save WAV';
        saveWavLink.href = clipUrl;
        const safeSpeciesWav = species.replace(/[^A-Za-z0-9]+/g, '_');
        const safeTimeWav = c.detected_at.replace(/[: ]/g, '-');
        saveWavLink.download = `${safeSpeciesWav}_${c.station}_${safeTimeWav}.wav`;
        row.appendChild(saveWavLink);

        const scaleSel = document.createElement('select');
        scaleSel.className = 'spec-scale';
        scaleSel.innerHTML = '<option value="log" selected>log freq</option><option value="linear">linear freq</option>';
        row.appendChild(scaleSel);

        const specBtn = document.createElement('button');
        specBtn.className = 'spec-btn';
        specBtn.textContent = 'Spectrogram';
        row.appendChild(specBtn);

        group.appendChild(row);

        const specPanel = document.createElement('div');
        specPanel.className = 'spec-panel';
        const img = document.createElement('img');
        img.className = 'spec-img';
        const saveLink = document.createElement('a');
        saveLink.className = 'spec-save-link';
        saveLink.textContent = 'Save image';
        specPanel.appendChild(img);
        specPanel.appendChild(document.createElement('br'));
        specPanel.appendChild(saveLink);
        group.appendChild(specPanel);

        div.appendChild(group);

        specBtn.addEventListener('click', () => {
          const scale = scaleSel.value;
          const url = `/api/spectrogram/${c.id}?scale=${scale}&gain=${gainSlider.value}&length=${lengthSlider.value}&lead_in=${leadInSlider.value}`;
          specBtn.disabled = true;
          specBtn.textContent = 'Generating\u2026';
          img.onload = () => { specBtn.disabled = false; specBtn.textContent = 'Spectrogram'; };
          img.onerror = () => { specBtn.disabled = false; specBtn.textContent = 'Spectrogram (failed)'; };
          img.src = url;
          const safeSpecies = species.replace(/[^A-Za-z0-9]+/g, '_');
          const safeTime = c.detected_at.replace(/[: ]/g, '-');
          saveLink.href = url;
          saveLink.download = `${safeSpecies}_${c.station}_${safeTime}_${scale}.png`;
          specPanel.classList.add('visible');
        });
      });
    });
}

gainSlider.addEventListener('change', () => { if (currentSpecies) loadClips(currentSpecies); });
lengthSlider.addEventListener('change', () => { if (currentSpecies) loadClips(currentSpecies); });
leadInSlider.addEventListener('change', () => { if (currentSpecies) loadClips(currentSpecies); });

// ---- daily (per-day / per-week / per-month) chart ----
function parseDateUTC(dstr) {
  const [y, m, d] = dstr.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
function fmtDateUTC(d) {
  return d.toISOString().slice(0, 10);
}
function addDaysUTC(d, days) {
  return new Date(d.getTime() + days * 86400000);
}

function loadDailyChart(species) {
  const dp = dateParams();
  dp.set('species', species);
  dp.set('min_conf', confSlider.value);
  fetch(`/api/daily?${dp.toString()}`)
    .then(r => r.json())
    .then(data => {
      const counts = data.counts;
      const startDate = parseDateUTC(data.start_date);
      const endDate = parseDateUTC(data.end_date);
      const spanDays = Math.round((endDate - startDate) / 86400000) + 1;

      // pick a bucket size so the chart stays readable
      let bucketType;
      if (spanDays <= 60) bucketType = 'day';
      else if (spanDays <= 400) bucketType = 'week';
      else bucketType = 'month';

      const labels = [];
      const backVals = [];
      const frontVals = [];

      if (bucketType === 'day') {
        for (let d = startDate; d <= endDate; d = addDaysUTC(d, 1)) {
          const dstr = fmtDateUTC(d);
          const c = counts[dstr] || { A: 0, B: 0 };
          labels.push(dstr.slice(5));   // MM-DD
          backVals.push(c.A);
          frontVals.push(c.B);
        }
      } else if (bucketType === 'week') {
        for (let d = startDate; d <= endDate; d = addDaysUTC(d, 7)) {
          const bucketEnd = addDaysUTC(d, 6);
          let a = 0, b = 0;
          for (let dd = d; dd <= bucketEnd && dd <= endDate; dd = addDaysUTC(dd, 1)) {
            const c = counts[fmtDateUTC(dd)];
            if (c) { a += c.A; b += c.B; }
          }
          labels.push(fmtDateUTC(d).slice(5));  // week-start MM-DD
          backVals.push(a);
          frontVals.push(b);
        }
      } else {
        // monthly buckets, calendar-aligned
        let y = startDate.getUTCFullYear(), m = startDate.getUTCMonth();
        while (y < endDate.getUTCFullYear() || (y === endDate.getUTCFullYear() && m <= endDate.getUTCMonth())) {
          const monthStart = new Date(Date.UTC(y, m, 1));
          const monthEnd = new Date(Date.UTC(y, m + 1, 0));
          let a = 0, b = 0;
          for (let dd = (monthStart > startDate ? monthStart : startDate);
               dd <= monthEnd && dd <= endDate; dd = addDaysUTC(dd, 1)) {
            const c = counts[fmtDateUTC(dd)];
            if (c) { a += c.A; b += c.B; }
          }
          labels.push(`${y}-${String(m + 1).padStart(2, '0')}`);
          backVals.push(a);
          frontVals.push(b);
          m += 1;
          if (m > 11) { m = 0; y += 1; }
        }
      }

      const ctx = document.getElementById('dailyChart');
      if (dailyChart) dailyChart.destroy();
      dailyChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [
            { label: 'Back yard (A)', data: backVals, backgroundColor: 'rgba(54,162,235,0.7)' },
            { label: 'Front yard (B)', data: frontVals, backgroundColor: 'rgba(255,159,64,0.7)' },
          ]
        },
        options: {
          responsive: true,
          scales: {
            x: { stacked: true, title: { display: true, text: bucketType === 'day' ? 'Date' : (bucketType === 'week' ? 'Week of' : 'Month') } },
            y: { stacked: true, title: { display: true, text: 'Detections' }, beginAtZero: true,
                 ticks: { precision: 0 } }
          }
        }
      });

      const sumBack = backVals.reduce((a, b) => a + b, 0);
      const sumFront = frontVals.reduce((a, b) => a + b, 0);
      const daysWithSpecies = Object.keys(counts).filter(d => counts[d].A > 0 || counts[d].B > 0).length;
      const daysChecked = countSelectedDays();
      document.getElementById('dailyTotal').textContent =
        `${sumBack + sumFront} (Back A: ${sumBack}, Front B: ${sumFront}) by ${bucketType}` +
        `  —  found on ${daysWithSpecies} of ${daysChecked} days`;
    });
}

// ---- species-day markers on the calendar ----
function loadSpeciesCalendarMarks(species) {
  // Deliberately NOT using dateParams() here — we want every day the
  // species was ever heard, across the whole dataset, so the marker
  // still shows up correctly if you page the calendar to a month
  // outside the currently selected date range.
  const p = new URLSearchParams();
  p.set('species', species);
  p.set('min_conf', confSlider.value);
  fetch(`/api/daily?${p.toString()}`)
    .then(r => r.json())
    .then(data => {
      speciesMatchDays = new Set(
        Object.keys(data.counts).filter(d => data.counts[d].A > 0 || data.counts[d].B > 0)
      );
      renderCalendar();
    });
}

function clearSpeciesCalendarMarks() {
  speciesMatchDays = new Set();
  renderCalendar();
}
// ---- calendar ----
const MONTH_NAMES = ['January','February','March','April','May','June',
                      'July','August','September','October','November','December'];

let heatThresholds = [0, 0, 0];  // [25th, 50th, 75th percentile of daily counts]

function computeHeatThresholds() {
  const vals = Object.values(dateCounts).filter(n => n > 0).sort((a, b) => a - b);
  if (vals.length === 0) { heatThresholds = [0, 0, 0]; return; }
  const pct = p => vals[Math.min(vals.length - 1, Math.floor(p * vals.length))];
  heatThresholds = [pct(0.25), pct(0.5), pct(0.75)];
}

function heatClass(n) {
  if (!n) return '';
  if (n >= heatThresholds[2]) return 'heat-4';
  if (n >= heatThresholds[1]) return 'heat-3';
  if (n >= heatThresholds[0]) return 'heat-2';
  return 'heat-1';
}

function monthDiff(y1, m1, y2, m2) {
  return (y2 - y1) * 12 + (m2 - m1);
}

function renderCalendar() {
  // Determine how many months to show: up to 4, but never reaching
  // earlier than the first month present in the data.
  let minY = calYear, minM = calMonth;
  if (dataMinDate) {
    const d = new Date(dataMinDate + 'T00:00:00');
    minY = d.getFullYear();
    minM = d.getMonth();
  }
  const available = monthDiff(minY, minM, calYear, calMonth) + 1;
  const monthsToShow = Math.max(1, Math.min(4, available));

  const months = [];
  for (let i = monthsToShow - 1; i >= 0; i--) {
    let mm = calMonth - i, yy = calYear;
    while (mm < 0) { mm += 12; yy -= 1; }
    months.push({ year: yy, month: mm });
  }

  const first = months[0], last = months[months.length - 1];
  document.getElementById('calRangeLabel').textContent = (months.length === 1)
    ? `${MONTH_NAMES[last.month]} ${last.year}`
    : `${MONTH_NAMES[first.month].slice(0, 3)} ${first.year} \u2013 ${MONTH_NAMES[last.month].slice(0, 3)} ${last.year}`;

  const container = document.getElementById('calMulti');
  container.innerHTML = '';

  months.forEach(({ year, month }) => {
    const block = document.createElement('div');
    block.className = 'cal-month-block';

    const label = document.createElement('div');
    label.className = 'cal-month-name';
    label.textContent = `${MONTH_NAMES[month]} ${year}`;
    block.appendChild(label);

    const grid = document.createElement('div');
    grid.className = 'cal-grid';
    ['S', 'M', 'T', 'W', 'T', 'F', 'S'].forEach(d => {
      const el = document.createElement('div');
      el.className = 'cal-dow';
      el.textContent = d;
      grid.appendChild(el);
    });

    const firstOfMonth = new Date(year, month, 1);
    const startWeekday = firstOfMonth.getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    for (let i = 0; i < startWeekday; i++) {
      const el = document.createElement('div');
      el.className = 'cal-day empty';
      grid.appendChild(el);
    }

    for (let day = 1; day <= daysInMonth; day++) {
      const dstr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const n = dateCounts[dstr] || 0;

      const el = document.createElement('div');
      el.className = 'cal-day';
      if (currentSpecies && speciesMatchDays.has(dstr)) el.classList.add('species-match');
      if (n > 0) {
        el.title = `${dstr}: ${n} detections`;
        el.addEventListener('click', () => onDayClick(dstr));
      } else {
        el.title = dstr;
      }

      const fill = document.createElement('div');
      fill.className = 'cal-day-fill';
      if (n > 0) fill.classList.add('has-data', heatClass(n));
      if (rangeStart && dstr === rangeStart) fill.classList.add('range-end');
      if (rangeEnd && dstr === rangeEnd) fill.classList.add('range-end');
      if (rangeStart && rangeEnd && dstr > rangeStart && dstr < rangeEnd) fill.classList.add('in-range');
      fill.textContent = day;

      el.appendChild(fill);
      grid.appendChild(el);
    }

    block.appendChild(grid);
    container.appendChild(block);
  });
}

function onDayClick(dstr) {
  if (!rangeStart || (rangeStart && rangeEnd)) {
    // start a fresh selection
    rangeStart = dstr;
    rangeEnd = null;
  } else if (dstr < rangeStart) {
    rangeStart = dstr;
  } else {
    rangeEnd = dstr;
  }
  updateStatus();
  renderCalendar();
  refreshAll();
}

function countSelectedDays() {
  const keys = Object.keys(dateCounts);
  if (!rangeStart) return keys.length;
  if (!rangeEnd) return keys.filter(d => d >= rangeStart).length;
  return keys.filter(d => d >= rangeStart && d <= rangeEnd).length;
}

function updateStatus() {
  const status = document.getElementById('calStatus');
  const totalDays = Object.keys(dateCounts).length;
  const dayCountText = `${countSelectedDays()}/${totalDays} days w/ data`;
  if (!rangeStart) {
    status.innerHTML = `All dates — ${dayCountText}`;
  } else if (!rangeEnd) {
    status.innerHTML = `From ${rangeStart} (click another day to set the end) — ${dayCountText} — <a id="calClear">clear</a>`;
  } else {
    status.innerHTML = `${rangeStart} to ${rangeEnd} — ${dayCountText} — <a id="calClear">clear</a>`;
  }
  const clearLink = document.getElementById('calClear');
  if (clearLink) clearLink.addEventListener('click', () => {
    rangeStart = null; rangeEnd = null;
    updateStatus(); renderCalendar(); refreshAll();
  });
}

function refreshAll() {
  loadSpecies();
  if (currentSpecies) loadDetail(currentSpecies);
}

document.getElementById('calPrev').addEventListener('click', () => {
  calMonth -= 1;
  if (calMonth < 0) { calMonth = 11; calYear -= 1; }
  renderCalendar();
});
document.getElementById('calNext').addEventListener('click', () => {
  calMonth += 1;
  if (calMonth > 11) { calMonth = 0; calYear += 1; }
  renderCalendar();
});

function loadDates() {
  fetch('/api/dates')
    .then(r => r.json())
    .then(data => {
      dateCounts = data.counts;
      dataMinDate = data.min_date;
      dataMaxDate = data.max_date;
      computeHeatThresholds();
      if (dataMaxDate) {
        const d = new Date(dataMaxDate + 'T00:00:00');
        calYear = d.getFullYear();
        calMonth = d.getMonth();
      } else {
        const now = new Date();
        calYear = now.getFullYear();
        calMonth = now.getMonth();
      }
      updateStatus();
      renderCalendar();
    });
}

loadDates();
loadSpecies();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML, version=VERSION)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    DB_PATH = args.db
    print(f"bird_dashboard.py v{VERSION}  (db: {DB_PATH})")
    app.run(host=args.host, port=args.port, debug=False)
