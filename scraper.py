#!/usr/bin/env python3
"""
TVPass Schedule Scraper
Fetches channel list and per-channel schedules from tvpass.org,
stores them locally, and compiles a master timegrid.

Usage:
    python3 scraper.py              # full scrape + rebuild timegrid
    python3 scraper.py --dry-run    # show what would be fetched, no writes
    python3 scraper.py --channel espn # scrape single channel only
"""

import json
import re
import sys
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

BASE_URL = "https://tvpass.org"
CHANNELS_URL = f"{BASE_URL}/channels"
SCHEDULE_URL = f"{BASE_URL}/tv_schedules"
DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = DATA_DIR / "scrape_state.json"
MASTER_FILE = DATA_DIR / "master_schedule.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch(url, timeout=15):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_json(url, timeout=15):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Channel list
# ---------------------------------------------------------------------------

def parse_channels(html):
    """Return list of (channel_name, slug) from the channels page HTML."""
    # <a href="https://tvpass.org/channel/<slug>">...<channel name>...</a>
    pattern = re.compile(
        r'<a\s+href="https://tvpass\.org/channel/([^"]+)"[^>]*>\s*'
        r'(?:<[^>]*>)*\s*([^<]+?)\s*(?:</[^>]*>)*\s*</a>',
        re.IGNORECASE,
    )
    channels = []
    seen = set()
    for m in pattern.finditer(html):
        slug = m.group(1).strip()
        name = m.group(2).strip()
        if slug and name and slug not in seen:
            seen.add(slug)
            channels.append((name, slug))
    return channels


# ---------------------------------------------------------------------------
# Schedule fetching
# ---------------------------------------------------------------------------

def fetch_schedule(slug):
    """Fetch schedule JSON for a single channel slug. Returns list of program dicts."""
    url = f"{SCHEDULE_URL}/{slug}.json"
    try:
        data = fetch_json(url)
    except HTTPError as e:
        if e.code == 404:
            print(f"  [WARN] No schedule found for {slug} (404)")
            return []
        raise
    except (URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  [ERROR] {slug}: {e}")
        return []

    normalised = []
    for entry in data:
        normalised.append({
            "channel_slug": slug,
            "start_iso": entry.get("data-listdatetime", ""),
            "duration_min": int(entry.get("data-duration", 0) or 0),
            "show": entry.get("data-showname", ""),
            "episode": entry.get("data-episodetitle", ""),
            "description": entry.get("data-description", ""),
        })
    return normalised


# ---------------------------------------------------------------------------
# Master schedule store
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"channels": {}, "last_scrape": None, "scrape_count": 0}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def compute_end_iso(start_iso, duration_min):
    if not start_iso:
        return ""
    try:
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end_dt = start_dt + timedelta(minutes=duration_min)
        return end_dt.isoformat()
    except Exception:
        return ""


def build_master(schedule_by_channel):
    """
    Merge all channel schedules into a flat sorted list of program slots.
    """
    entries = []
    for slug, programs in schedule_by_channel.items():
        for p in programs:
            end_iso = compute_end_iso(p["start_iso"], p["duration_min"])
            entries.append({
                "key": f"{slug}||{p['start_iso']}",
                "channel_slug": slug,
                "start_iso": p["start_iso"],
                "end_iso": end_iso,
                "duration_min": p["duration_min"],
                "show": p["show"],
                "episode": p["episode"],
                "description": p["description"],
            })
    entries.sort(key=lambda x: x["start_iso"])
    return entries


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(old_entry, new_entry):
    """Return True if any programme fields differ."""
    for field in ("show", "episode", "description", "duration_min"):
        if old_entry.get(field) != new_entry.get(field):
            return True
    return False


# ---------------------------------------------------------------------------
# Main scrape
# ---------------------------------------------------------------------------

def scrape(*, dry_run=False, channel_slug=None):
    print(f"\n{'[DRY RUN] ' if dry_run else ''}TVPass Schedule Scraper — {datetime.now().isoformat()}")
    print("=" * 60)

    state = load_state()

    # --- 1. Fetch channel list ---
    print(f"\n[1] Fetching channel list: {CHANNELS_URL}")
    html = fetch(CHANNELS_URL)
    channels = parse_channels(html)
    print(f"    Found {len(channels)} channels")
    if dry_run:
        for name, slug in channels[:5]:
            print(f"    - {name} ({slug})")
        print(f"    ... ({len(channels) - 5} more omitted)")
        return

    if channel_slug:
        channels = [(n, s) for n, s in channels if s == channel_slug]
        if not channels:
            print(f"[ERROR] Channel '{channel_slug}' not found in channel list")
            return

    # --- 2. Fetch each channel schedule ---
    schedule_by_channel = {}
    changes_log = []   # (action, slug, start_iso, show, ...)
    total_programs = 0

    print(f"\n[2] Fetching schedules for {len(channels)} channel(s)...")
    for i, (name, slug) in enumerate(channels, 1):
        print(f"  [{i}/{len(channels)}] {name} ...", end="", flush=True)

        programs = fetch_schedule(slug)
        schedule_by_channel[slug] = programs
        total_programs += len(programs)

        old_programs = state["channels"].get(slug, {}).get("programs", [])
        old_map = {f"{slug}||{p['start_iso']}": p for p in old_programs}
        new_keys = {p["start_iso"] for p in programs}

        # new / amended
        for p in programs:
            key = f"{slug}||{p['start_iso']}"
            if key not in old_map:
                changes_log.append(("APPEND", slug, p["start_iso"], p["show"]))
            else:
                old_p = old_map[key]
                if detect_changes(old_p, p):
                    changes_log.append(("AMEND", slug, p["start_iso"], p["show"]))

        # removed
        for p in old_programs:
            key = f"{slug}||{p['start_iso']}"
            if p["start_iso"] not in new_keys:
                changes_log.append(("REMOVE", slug, p["start_iso"], p["show"]))

        # update state channel metadata
        meta = dict(state["channels"].get(slug, {}))
        meta["name"] = name
        meta["programs"] = programs
        meta["last_fetch"] = datetime.now(timezone.utc).isoformat()
        state["channels"][slug] = meta

        print(f" {len(programs)} programs")
        time.sleep(0.3)

    # --- 3. Save state ---
    state["last_scrape"] = datetime.now(timezone.utc).isoformat()
    state["scrape_count"] = state.get("scrape_count", 0) + 1
    save_state(state)
    print(f"\n[3] State saved ({total_programs} total programs)")

    # --- 4. Build and save master schedule ---
    master = build_master(schedule_by_channel)
    MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MASTER_FILE, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "channels": list(schedule_by_channel.keys()),
            "programs": master,
            "changes": changes_log,
        }, f, indent=2)
    print(f"[4] Master schedule: {MASTER_FILE}")

    # --- 5. Summary ---
    appends = sum(1 for c in changes_log if c[0] == "APPEND")
    amends  = sum(1 for c in changes_log if c[0] == "AMEND")
    removes = sum(1 for c in changes_log if c[0] == "REMOVE")
    print(f"\n[5] Change summary: +{appends}  ~{amends}  -{removes}")
    print(f"\nDone. Next run in 2 days (cron: 0 2 */2 * *)\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    channel_slug = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            channel_slug = arg

    try:
        scrape(dry_run=dry_run, channel_slug=channel_slug)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[FATAL] {e}")
        sys.exit(1)
