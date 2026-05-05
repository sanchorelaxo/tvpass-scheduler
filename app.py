#!/usr/bin/env python3
"""
TVPass Schedule HTTP Service
Serves the master TV schedule as an HTML timegrid.
Run on Raspberry Pi:  python3 app.py  (default port 8765)

You can change the port with:  python3 app.py --port 8080
"""

import json
import argparse
import re
import time
import urllib.request
import zoneinfo
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template, request

DATA_DIR = Path(__file__).parent / "data"
MASTER_FILE = DATA_DIR / "master_schedule.json"
STATE_FILE = DATA_DIR / "scrape_state.json"
PORT = 8765

# New York timezone (EST/EDT handled automatically via IANA database)
_NY_TZ = zoneinfo.ZoneInfo("America/New_York")


def ny_now():
    """Return current datetime in America/New_York (EST or EDT as appropriate)."""
    return datetime.now(timezone.utc).astimezone(_NY_TZ)


def ny_strftime(fmt):
    """Shortcut for ny_now().strftime(fmt)."""
    return ny_now().strftime(fmt)

# Persistent session for streaming (maintains cookies across requests)
_stream_session = None
_stream_session_last_refresh = 0


def get_stream_session():
    """Return a urllib session that maintains cookies (needed for /token endpoint)."""
    global _stream_session, _stream_session_last_refresh
    now = time.time()
    # Refresh session every 10 minutes to keep it alive
    if _stream_session is None or (now - _stream_session_last_refresh) > 600:
        _stream_session = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        _stream_session_last_refresh = now
    return _stream_session


app = Flask(__name__, template_folder=Path(__file__).parent / "templates")


# -----------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_master():
    if MASTER_FILE.exists():
        with open(MASTER_FILE) as f:
            return json.load(f)
    return None


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def local_now():
    """Return current datetime in UTC."""
    return datetime.now(timezone.utc)


def parse_iso(iso_str):
    """Parse an ISO datetime string to a UTC datetime object."""
    if not iso_str:
        return None
    try:
        # handle Z suffix
        s = iso_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _ny_offset(dt_utc):
    """
    Return the New York UTC offset in hours for a given UTC datetime.
    Handles EST (UTC-5) vs EDT (UTC-4) based on US DST rules.
    """
    # US DST: second Sunday March 2am local → first Sunday November 2am local
    ny_dt = dt_utc.astimezone(_NY_TZ)
    # The offset in hours is: UTC - local, so we get it from the tzinfo
    total_secs = ny_dt.utcoffset().total_seconds()
    return total_secs / 3600.0


def to_ny(dt_utc):
    """
    Convert a UTC datetime to New York local time (naive, in NY timezone).
    The Pi system clock is in ET/EST. dt_utc has a UTC tzinfo; subtract offset
    to get the correct ET clock time.
    UTC = ET + |offset|  →  ET = UTC - |offset|  →  ET = UTC + offset (offset is negative)
    """
    offset_hours = _ny_offset(dt_utc)
    # offset_hours is negative (e.g. -4 for EDT). UTC + offset = ET.
    ny_naive = dt_utc.replace(tzinfo=None) + timedelta(hours=offset_hours)
    return ny_naive


def slot_key(dt):
    """Return (date_str, hour) tuple for grouping."""
    if not dt:
        return ("Unknown", 0)
    # Convert to UTC (already UTC from the source)
    date_str = dt.strftime("%Y-%m-%d")
    hour = dt.hour
    return (date_str, hour)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Render the master timegrid HTML."""
    master = load_master()
    state = load_state()

    if not master:
        return render_template("no_data.html"), 503

    programs = master.get("programs", [])
    channel_slugs = master.get("channels", [])

    # Build channel name lookup from state
    channel_meta = state.get("channels", {})
    channel_names = {
        slug: channel_meta.get(slug, {}).get("name", slug.replace("-", " ").title())
        for slug in channel_slugs
    }

    # Group programs by (date, hour) in New York time
    # (source data is UTC; we convert to America/New_York for display)
    by_slot = {}   # (date, hour) → channel_slug → program

    for p in programs:
        dt = parse_iso(p["start_iso"])
        if not dt:
            continue
        # Convert UTC → New York using proper offset subtraction
        dt_ny = to_ny(dt)
        date_str = dt_ny.strftime("%Y-%m-%d")
        hour = dt_ny.hour
        key = (date_str, hour)
        if key not in by_slot:
            by_slot[key] = {}
        ch = p["channel_slug"]
        if ch not in by_slot[key]:
            by_slot[key][ch] = []
        by_slot[key][ch].append(p)

    # Build sorted list of time slots
    sorted_slots = sorted(by_slot.keys())  # (date, hour)

    # Collect all unique dates that appear (in NY time)
    # Today's date (NY) always comes first; subsequent dates follow in order
    today_ny = ny_now().strftime("%Y-%m-%d")
    unique_dates = sorted(set(s for s, _ in sorted_slots))
    all_dates = [today_ny] + [d for d in unique_dates if d != today_ny]

    # Time labels (e.g. "00:00", "02:00", ... every 2 hours)
    hours = list(range(0, 24, 2))
    time_labels = [f"{h:02d}:00" for h in hours]

    # Build the grid: string key "date||hour||ch" → list of programs
    grid = {}
    for slot_key_val, channel_map in by_slot.items():
        date, hour = slot_key_val
        for ch, progs in channel_map.items():
            grid[f"{date}||{hour:02d}||{ch}"] = progs

    context = {
        "generated_at": master.get("generated_at", ""),
        "channel_slugs": channel_slugs,
        "channel_names": channel_names,
        "all_dates": all_dates,
        "today_ny": today_ny,          # current NY date for "today" highlight
        "time_labels": time_labels,
        "hours": hours,
        "grid": grid,
        "scrape_count": state.get("scrape_count", 0),
        "last_scrape": state.get("last_scrape", ""),
        "program_count": len(programs),
        "now_utc": ny_strftime("%Y-%m-%d %H:%M %Z"),  # was UTC, now NY time
    }
    return render_template("timegrid.html", **context)


@app.route("/api/schedule")
def api_schedule():
    """Return the raw master schedule as JSON."""
    master = load_master()
    if not master:
        return jsonify({"error": "No schedule data available. Run scraper.py first."}), 503
    return jsonify(master)


@app.route("/api/channels")
def api_channels():
    """Return list of channels with metadata."""
    state = load_state()
    channels = []
    for slug, meta in state.get("channels", {}).items():
        channels.append({
            "slug": slug,
            "name": meta.get("name", slug),
            "program_count": len(meta.get("programs", [])),
            "last_fetch": meta.get("last_fetch", ""),
        })
    channels.sort(key=lambda x: x["name"])
    return jsonify(channels)


@app.route("/api/channels/<slug>")
def api_channel(slug):
    """Return schedule for a specific channel."""
    state = load_state()
    meta = state.get("channels", {}).get(slug)
    if not meta:
        return jsonify({"error": f"Channel '{slug}' not found"}), 404
    return jsonify({
        "slug": slug,
        "name": meta.get("name", slug),
        "programs": meta.get("programs", []),
    })


@app.route("/health")
def health():
    """Health check endpoint."""
    master = load_master()
    return {"status": "ok" if master else "no_data", "generated_at": master.get("generated_at") if master else None}


@app.route("/favicon.ico")
def favicon():
    """Serve favicon."""
    from flask import send_from_directory
    return send_from_directory(Path(__file__).parent / "templates", "favicon.ico", mimetype="image/x-icon")


# -----------------------------------------------------------------------------
# Streaming
# -----------------------------------------------------------------------------

@app.route("/api/stream/<slug>")
def api_stream(slug):
    """
    Proxy for the m3u8 stream URL of a channel.
    Fetches the channel page (with session cookies), then /token to get m3u8 URL.
    """
    session = get_stream_session()
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://tvpass.org/channel/" + slug,
        "Origin": "https://tvpass.org",
        "Accept": "application/json",
    }
    try:
        # Fetch channel page to extract stream_name (session establishes cookies)
        channel_url = f"https://tvpass.org/channel/{slug}"
        req = urllib.request.Request(channel_url, headers=headers)
        with session.open(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Extract stream_name from <div id="stream_name" name="WABCDT1">
        m = re.search(r'id\s*=\s*["\']stream_name["\'][^>]*name\s*=\s*["\']([^"\']+)["\']', html)
        if not m:
            m = re.search(r'name\s*=\s*["\']([^"\']+)["\'][^>]*id\s*=\s*["\']stream_name["\']', html)
        if not m:
            return jsonify({"error": f"No stream_name found for channel '{slug}'"}), 404

        stream_name = m.group(1)

        # Fetch token — cookies from the channel page visit are sent automatically
        token_url = f"https://tvpass.org/token/{stream_name}"
        token_req = urllib.request.Request(token_url, headers=headers)
        with session.open(token_req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))

        if token_data.get("status") == "failed":
            return jsonify({"error": "Stream unavailable — server at capacity"}), 503
        m3u8_url = token_data.get("url")
        if not m3u8_url:
            return jsonify({"error": "No m3u8 URL in token response"}), 502

        return jsonify({"m3u8": m3u8_url, "stream_name": stream_name})

    except urllib.error.HTTPError as e:
        return jsonify({"error": f"HTTP {e.code}"}), e.code
    except urllib.error.URLError as e:
        return jsonify({"error": f"URL error: {e.reason}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TVPass Schedule HTTP Service")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    args = parser.parse_args()

    print(f"TVPass Schedule Service starting on {args.host}:{args.port}")
    print(f"  Data dir : {DATA_DIR}")
    print(f"  Master   : {MASTER_FILE}")
    print(f"  Open     : http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, debug=False)
