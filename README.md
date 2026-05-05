# tvpass-scheduler

Flask web app that scrapes TV schedules from [tvpass.org](https://tvpass.org) and displays them as a browsable timegrid, viewable in a browser on your network. Designed to run on a Raspberry Pi.

**Stack:** Python 3 + Flask + plain JavaScript (no build step)  
**Data:** 177 channels, refreshed every 2 days via cron  
**Network:** serves on `http://<pi-ip>:8765`

---

## Quick Start

```
# Install dependencies
pip install -r requirements.txt

# Run the scraper once to fetch schedules
python3 scraper.py

# Start the web server
python3 app.py
```

Then open `http://<this-pi>:8765` in your browser.

---

## Project Structure

```
tvpass-scheduler/
├── app.py              # Flask server + API routes
├── scraper.py          # Schedule fetcher + state manager
├── requirements.txt    # Python dependencies
├── cron.sh             # Cron job template (see below)
├── data/
│   ├── scrape_state.json     # Per-channel program state (source of truth)
│   └── master_schedule.json  # Flat merged schedule, served to the browser
└── templates/
    └── timegrid.html  # Single-page UI (Jinja2 template)
```

---

## API Routes

| Route | Method | Description |
|---|---|---|
| `/` | GET | Timegrid page |
| `/api/channels` | GET | `[{slug, name}, ...]` for all channels |
| `/api/schedule` | GET | Full schedule: `{programs, channels, all_dates, ...}` |
| `/api/stream/<slug>` | GET | Stream URL for a channel slug |
| `/favicon.ico` | GET | Blue TV-icon favicon |

---

## Channel Filter

The channel dropdown has three modes:

| Selection | Grid | Video Overlay |
|---|---|---|
| "— No channel selected —" (default) | Empty | Hidden |
| "— All channels —" | All 177 channels | Hidden |
| `<channel name>` | That channel only | Shows stream |

---

## Cron Job — Auto-Scrape Every 2 Days

The scraper should run on a schedule to keep schedules current. On the Pi, edit your crontab:

```bash
crontab -e
```

Add this line:

```
0 2 */2 * * /usr/bin/python3 /home/sanchobot/tvpass-scheduler/scraper.py >> /home/sanchobot/tvpass-scheduler/scrape.log 2>&1
```

This runs the scraper every 2 days at 02:00 UTC (which is ~22:00 EDT / 23:00 EST).

**Note:** The cron entry in `cron.sh` is commented out by default. Uncomment it in your crontab to activate.

The scraper:
- Fetches all 177 channel schedules from tvpass.org
- Diffs against previous state (APPEND / AMEND / REMOVE)
- Saves to `scrape_state.json` + `master_schedule.json`
- Produces a change summary (`+N ~M -R`) in `scrape.log`

To manually trigger a scrape:

```bash
python3 scraper.py
```

---

## Streaming

Clicking a channel loads its stream via `/api/stream/<slug>`. This is a proxy to the underlying TVPass stream URL — it is not a transcoding server. Streams work only within the network; external access depends on your ISP's carrier-grade NAT.

---

## Pi-Specific Notes

- **Timezone:** Pi runs in UTC. TV schedules are converted to Eastern Time (America/New_York) in the browser via `Intl.DateTimeFormat`.
- **Camera conflict:** PipeWire may hold `/dev/video4` and cause `rpicam-vid` timeouts. Stop it with `systemctl --user stop pipewire pipewire-pulse wireplumber` if needed.
- **Firewall:** Ensure port 8765 is open on the Pi's firewall if you have one.

---

## Git

```bash
git remote add origin https://github.com/sanchorelaxo/tvpass-scheduler.git
git push -u origin master
```
