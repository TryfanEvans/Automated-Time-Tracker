# Activity Tracker

Automatic time tracker for Linux. Polls [ActivityWatch](https://activitywatch.net) for the active window, classifies activity using Claude Haiku, and serves a dashboard at `http://localhost:5700`.

## Features

- **Automatic classification** — window titles are classified into categories using Claude Haiku. Results are cached so each unique title is only classified once.
- **Rule-based pre-filtering** — known apps and domains (VSCode, terminals, Netflix, Instagram, etc.) are classified instantly without an API call.
- **Targeted yes/no prompts** — YouTube, Reddit, Gmail, and news sites use short single-sentence prompts rather than the full classifier, reducing token usage.
- **Browser URL integration** — if the [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web) extension is installed, real URLs are used for domain extraction instead of parsing window titles.
- **AFK detection** — polls the ActivityWatch AFK watcher and flushes the current session after 5 minutes of inactivity.
- **Suspend/resume handling** — listens for D-Bus `PrepareForSleep` signals to flush sessions before sleep and resume tracking on wake.
- **Manual activity logging** — start/stop timer for offline activities (exercise, meals, reading, etc.) with free-text category entry.
- **Gap filling** — click any untracked gap in the timeline to log a manual entry retroactively.
- **Timeline editing** — click any manually logged block to edit its category or note. Drag the top or bottom edge to resize. Delete from the popup or the feed.
- **Daily timeline** — vertical timeline with colour-coded blocks, gap zones, hour labels, and a live now-marker.
- **Category bars** — today's time broken down by category with hours.
- **Work/Study tracking** — today and this week stats show Work/Study hours only, compared against a configurable weekly goal.
- **Activity feed** — scrollable log of all events with delete.
- **CSV export** — export 7 or 30 days of data.
- **Crash safety** — open sessions are checkpointed every 5 minutes so at most 5 minutes of data is lost on unexpected exit.

## Categories

Auto-classified: `Work/Study`, `Reading`, `Reddit`, `YouTube`, `Facebook`, `Instagram`, `Twitter`, `TikTok`, `Browsing`, `Messaging`, `Admin`, `Entertainment`, `News`, `Discord`, `Other`

Manual entry accepts any free text.

## Requirements

- Python 3.10+
- [ActivityWatch](https://activitywatch.net) running on `localhost:5600`
- Anthropic API key (optional — falls back to `Other` for all auto events without one)
- [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web) browser extension (optional — improves URL-based classification)
- `python3-dbus` and `python3-gi` system packages (optional — required for suspend/resume handling)

## Setup

```bash
git clone https://github.com/TryfanEvans/Automated-Time-Tracker.git
cd Automated-Time-Tracker

# Create venv with access to system dbus/gi packages
python3 -m venv venv --system-site-packages
source venv/bin/activate

pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."
# To persist: add the above line to ~/.bashrc
```

Make sure ActivityWatch is running before starting the tracker.

## Run

```bash
source venv/bin/activate
export ANTHROPIC_API_KEY="sk-ant-..."
python3 main.py
```

Open `http://localhost:5700`.

## Data

- Database: `~/.activity_tracker.db` (SQLite)
- Classification cache: `~/.activity_tracker_cache.json`
- Log: `~/activity_tracker.log`

To reset all data:
```bash
rm ~/.activity_tracker.db ~/.activity_tracker_cache.json
```

## Configuration

Edit `config.py` to change:
- `WEEK_GOAL_H` — weekly Work/Study hour goal (default: 20)
- `PORT` — dashboard port (default: 5700)
- `POLL_INTERVAL` — ActivityWatch poll interval in seconds (default: 5)
- `AFK_THRESHOLD_S` in `polling.py` — seconds of inactivity before session is flushed (default: 300)