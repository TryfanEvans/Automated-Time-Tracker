# Activity Tracker

Polls ActivityWatch every 30s, classifies window titles with Claude Haiku,
serves a dashboard at http://localhost:5700.

## Setup

```bash
pip install flask anthropic requests
```

Put `tracker.py` and `dashboard.html` in the same folder.

Set your API key:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# To make it permanent, add the above line to ~/.bashrc
```

Make sure ActivityWatch is running (it should be on localhost:5600 by default).

## Run

```bash
python3 tracker.py
```

Then open http://localhost:5700 in your browser.

## Notes

- Classification results are cached — the API is only called when the active
  window changes, so costs are minimal (fractions of a cent per day).
- Data is stored in ~/.activity_tracker.db (SQLite).
- Logs are written to ~/activity_tracker.log.
- If ANTHROPIC_API_KEY is not set, all auto events are classified as "Other"
  but everything else still works.
- The dashboard auto-refreshes every 30 seconds.

## To run on startup (optional)

Create a systemd service or add to your ~/.bashrc:
```bash
nohup python3 /path/to/tracker.py &
```