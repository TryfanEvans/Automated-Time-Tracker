#!/usr/bin/env python3
"""
Activity Tracker
Polls ActivityWatch, classifies with Claude Haiku, serves dashboard on localhost:5700

Setup:
    pip install flask anthropic requests
    export ANTHROPIC_API_KEY="sk-ant-..."
    python3 tracker.py

Dashboard: http://localhost:5700
"""

import os
import time
import sqlite3
import threading
import csv
import io
import json
import logging
import signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from anthropic import Anthropic
from flask import Flask, jsonify, request, send_from_directory, Response

# ── Config ─────────────────────────────────────────────────────────────────────
AW_SERVER     = "http://localhost:5600"
POLL_INTERVAL = 5           # seconds between window checks
DB_PATH       = Path.home() / ".activity_tracker.db"
PORT          = 5700
MODEL         = "claude-haiku-4-5-20251001"
WEEK_GOAL_H   = 20

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

AUTO_CATEGORIES = [
    "Work/Study",
    "Reading",
    "Reddit",
    "YouTube",
    "Facebook",
    "Instagram",
    "Twitter",
    "TikTok",
    "Browsing",
    "Messaging",
    "Admin",
    "Entertainment",
    "News",
    "Discord",
    "Other",
]

MANUAL_CATEGORIES = [
    "Messaging",
    "Reading",
    "Sleep",
    "Meal",
    "Admin",
    "Other",
]

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path.home() / "activity_tracker.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            app        TEXT    DEFAULT '',
            title      TEXT    DEFAULT '',
            category   TEXT    NOT NULL DEFAULT 'Other',
            source     TEXT    NOT NULL DEFAULT 'auto',
            duration_s REAL    NOT NULL DEFAULT 0,
            note       TEXT    DEFAULT ''
        )
    """)
    con.commit()
    con.close()

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

# ── Classification ─────────────────────────────────────────────────────────────
_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
_cache_path = Path.home() / ".activity_tracker_cache.json"

def _load_cache() -> dict:
    try:
        if _cache_path.exists():
            return json.loads(_cache_path.read_text())
    except Exception:
        pass
    return {}

def _save_cache(cache: dict):
    try:
        _cache_path.write_text(json.dumps(cache))
    except Exception as e:
        log.warning(f"Cache save failed: {e}")

_classify_cache = _load_cache()
log.info("Loaded %d cached classifications", len(_classify_cache))

def classify(app: str, title: str) -> str:
    """Classify window title via Claude Haiku. Caches results to save API calls."""
    if not _client:
        return "Other"

    # Deterministic rules — no API call needed
    combined = (app + " " + title).lower()
    if any(h in combined for h in TRACKER_WINDOW_HINTS):
        return "Admin"

    key = f"{app}|{title}"
    if key in _classify_cache:
        return _classify_cache[key]

    prompt = f"""Classify this computer activity into exactly one category.
App: {app}
Window title: {title}

Categories: {", ".join(AUTO_CATEGORIES)}

Rules (apply in order — first match wins):

1. WORK/STUDY — use this if ANY of the following are true:
   - Content involves code, programming, math, algorithms, data structures, EVM, blockchain, formal verification, or computer science
   - Title mentions Adelaide Uni, AUCPL, university coursework, or study groups
   - App is VSCode, a terminal, an IDE, Jupyter, or similar coding tool
   - Claude/ChatGPT/AI tool AND title suggests the topic is code, math, or study
   - YouTube AND title suggests a coding tutorial, math lecture, or CS topic
   - Reddit AND title suggests a programming or math subreddit/post
   - PDF viewer or ebook app (also consider Reading below)
   - News site AND content is clearly about code or technology

2. READING — use this if:
   - App is a PDF viewer (Evince, Okular, Adobe, browser with .pdf in title) AND content looks like a book, paper, or article
   - Does NOT override Work/Study if the PDF is clearly study material — use Work/Study instead

3. MESSAGING — use this if:
   - Discord AND a username/person's name appears at the start of the title (DM pattern)
   - WhatsApp Web, Messenger, Telegram Web, Signal Desktop
   - Title pattern looks like "Username - Discord" or "Name | Messenger"

4. DISCORD — use this if:
   - App is Discord AND server name does NOT contain CS, study, programming, code, uni, university, or similar academic signals
   - General Discord servers, gaming servers, friend groups

5. ADMIN — use this if:
   - Gmail AND email looks administrative (signups, forms, scheduling, uni admin)
   - Google Calendar, forms, university portals
   - Discord server name contains CS, Study, Programming, Uni, University, or similar
   - Reddit AND title looks like a search or post about a specific admin task

6. Per-site categories — use the site name if content is recreational:
   - REDDIT — reddit.com, not work/study/admin
   - YOUTUBE — youtube.com, not work/study
   - FACEBOOK — facebook.com, not messaging
   - INSTAGRAM — instagram.com
   - TWITTER — twitter.com or x.com
   - TIKTOK — tiktok.com

7. ENTERTAINMENT — Netflix, Spotify, Steam, games, Twitch

8. NEWS — news sites (ABC, BBC, Guardian, Reuters etc.) when content is not code/math

9. BROWSING — any other unrecognised web browsing

10. OTHER — everything else

Reply with ONLY the category name, nothing else."""

    try:
        msg = _client.messages.create(
            model=MODEL,
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        result = "Other"
        for cat in AUTO_CATEGORIES:
            if cat.lower() in raw.lower():
                result = cat
                break
        _classify_cache[key] = result
        _save_cache(_classify_cache)
        return result
    except Exception as e:
        log.error(f"Classification error: {e}")
        return "Other"

# ── ActivityWatch polling ──────────────────────────────────────────────────────
_last_key      = None
_last_category = "Other"

# Shared activity state — set by frontend via /api/activity/start
_active_activity = {
    "running":   False,
    "category":  None,
    "ts":        None,   # ISO start time
    "stopped_by": None,  # None | "manual" | "auto"
}
_activity_lock = threading.Lock()

TRACKER_WINDOW_HINTS = ("activity tracker", "localhost:5700", "127.0.0.1:5700")

def is_tracker_window(app: str, title: str) -> bool:
    combined = (app + " " + title).lower()
    return any(h in combined for h in TRACKER_WINDOW_HINTS)

def auto_stop_activity(app: str, title: str):
    """Log and clear the active activity. Called from poll loop."""
    with _activity_lock:
        if not _active_activity["running"]:
            return
        cat      = _active_activity["category"]
        start_ts = _active_activity["ts"]
        now      = datetime.now(timezone.utc)
        dur_s    = (now - datetime.fromisoformat(start_ts)).total_seconds()
        _active_activity["running"]    = False
        _active_activity["stopped_by"] = "auto"

    if dur_s < 5:
        log.info("[auto-stop] activity too short (<5s), discarding")
        return

    try:
        con = get_db()
        con.execute(
            "INSERT INTO events (ts,app,title,category,source,duration_s,note) "
            "VALUES (?,?,?,?,'manual',?,?)",
            (start_ts, "manual", cat, cat, dur_s, f"auto-stopped by: {app}"),
        )
        con.commit()
        con.close()
        log.info("[auto-stop] %s %.0fs — window switched to %s", cat, dur_s, app)
    except Exception as e:
        log.error(f"Auto-stop write error: {e}")

def get_current_window():
    try:
        r = requests.get(f"{AW_SERVER}/api/0/buckets", timeout=3)
        if not r.ok:
            return None
        for bid in r.json():
            if "aw-watcher-window" in bid:
                r2 = requests.get(
                    f"{AW_SERVER}/api/0/buckets/{bid}/events",
                    params={"limit": 1},
                    timeout=3,
                )
                if r2.ok:
                    events = r2.json()
                    if events:
                        d = events[0]["data"]
                        return d.get("app", ""), d.get("title", "")
    except Exception as e:
        log.debug(f"AW poll: {e}")
    return None

# ── Current window state (shared between poll loop and shutdown handlers) ──────
_window_state = {
    "row_id":    None,      # database id of the current open row
    "app":       "",
    "title":     "",
    "category":  "Other",
    "start_ts":  None,      # datetime
}
_window_lock = threading.Lock()

def insert_window_row(app: str, title: str, category: str, start_ts: datetime) -> int:
    """Insert a new row with duration 0 and return its id."""
    con = get_db()
    cur = con.execute(
        "INSERT INTO events (ts,app,title,category,source,duration_s) VALUES (?,?,?,?,'auto',0)",
        (start_ts.isoformat(), app, title, category),
    )
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id

def update_window_row(row_id: int, start_ts: datetime, end_ts: datetime):
    """Update duration_s on an existing row."""
    dur_s = max(0, (end_ts - start_ts).total_seconds())
    try:
        con = get_db()
        con.execute("UPDATE events SET duration_s=? WHERE id=?", (dur_s, row_id))
        con.commit()
        con.close()
    except Exception as e:
        log.error(f"Update error: {e}")

def flush_current_window(min_duration_s: float = 3.0):
    """Write final duration for the current open row. Discards rows shorter than min_duration_s."""
    with _window_lock:
        if _window_state["row_id"] is None or _window_state["start_ts"] is None:
            return
        row_id   = _window_state["row_id"]
        start_ts = _window_state["start_ts"]
    now   = datetime.now(timezone.utc)
    dur_s = (now - start_ts).total_seconds()
    if dur_s < min_duration_s:
        # Too short — delete the row rather than keep a noisy entry
        try:
            con = get_db()
            con.execute("DELETE FROM events WHERE id=?", (row_id,))
            con.commit()
            con.close()
            log.info("[flush] row %d discarded (%.0fs < %.0fs min)", row_id, dur_s, min_duration_s)
        except Exception as e:
            log.error(f"Discard error: {e}")
        return
    update_window_row(row_id, start_ts, now)
    log.info("[flush] row %d updated (%.0fs)", row_id, dur_s)

def poll_loop():
    global _last_key, _last_category
    log.info("Starting event-driven window tracker (check every %ds)", POLL_INTERVAL)

    CHECKPOINT_INTERVAL = 300  # update row duration every 5 min as crash safety net
    last_checkpoint = datetime.now(timezone.utc)

    while True:
        try:
            now    = datetime.now(timezone.utc)
            result = get_current_window()

            if result:
                app, title = result

                # Skip transient unknown windows (e.g. briefly after resume)
                if not app and not title:
                    time.sleep(POLL_INTERVAL)
                    continue
                if app.lower() == "unknown" or title.lower() == "unknown":
                    time.sleep(POLL_INTERVAL)
                    continue

                key = f"{app}|{title}"

                if key != _last_key:
                    # Window changed — finalise the previous row
                    flush_current_window()

                    # Auto-stop manual activity if switched away from tracker
                    with _activity_lock:
                        act_running = _active_activity["running"]
                    if act_running and not is_tracker_window(app, title):
                        auto_stop_activity(app, title)

                    # Classify new window and insert a fresh row
                    _last_category = classify(app, title)
                    _last_key      = key
                    row_id         = insert_window_row(app, title, _last_category, now)
                    last_checkpoint = now

                    with _window_lock:
                        _window_state["row_id"]   = row_id
                        _window_state["app"]      = app
                        _window_state["title"]    = title
                        _window_state["category"] = _last_category
                        _window_state["start_ts"] = now

                    log.info("[new] %s | %.55s → %s", app, title, _last_category)

                else:
                    # Same window — checkpoint every 5 min so crashes lose minimal data
                    if (now - last_checkpoint).total_seconds() >= CHECKPOINT_INTERVAL:
                        flush_current_window()
                        last_checkpoint = now

        except Exception as e:
            log.error(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)

# ── Graceful shutdown (SIGTERM / Ctrl-C) ───────────────────────────────────────
def _shutdown_handler(signum, frame):
    log.info("Shutdown signal received — flushing current window")
    flush_current_window()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT,  _shutdown_handler)

# ── D-Bus PrepareForSleep listener (suspend/resume) ───────────────────────────
def _start_sleep_listener():
    """Listen for systemd PrepareForSleep signal to flush before suspend."""
    try:
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()

        def on_prepare_for_sleep(sleeping):
            if sleeping:
                log.info("[sleep] PrepareForSleep — flushing current window")
                flush_current_window()
            else:
                # Resumed — reset start time so duration doesn't include sleep
                log.info("[sleep] Resumed — resetting window start time")
                with _window_lock:
                    if _window_state["row_id"] is not None:
                        _window_state["start_ts"] = datetime.now(timezone.utc)

        bus.add_signal_receiver(
            on_prepare_for_sleep,
            signal_name="PrepareForSleep",
            dbus_interface="org.freedesktop.login1.Manager",
            bus_name="org.freedesktop.login1",
            path="/org/freedesktop/login1",
        )

        loop = GLib.MainLoop()
        log.info("D-Bus sleep listener active")
        loop.run()

    except Exception as e:
        log.warning(f"D-Bus sleep listener unavailable ({e}) — suspend handling disabled")

threading.Thread(target=_start_sleep_listener, daemon=True).start()

# ── Flask ──────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
DASHBOARD = Path(__file__).parent / "dashboard.html"

def since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

def week_start():
    n = datetime.now(timezone.utc)
    return (n - timedelta(days=n.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

@flask_app.route("/")
def index():
    return send_from_directory(str(DASHBOARD.parent), DASHBOARD.name)

@flask_app.route("/api/events")
def api_events():
    days = int(request.args.get("days", 1))
    con = get_db()
    rows = con.execute(
        "SELECT id,ts,source,app,title,category,duration_s,note "
        "FROM events WHERE ts>=? ORDER BY ts DESC LIMIT 400",
        (since(days),),
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@flask_app.route("/api/stats")
def api_stats():
    con = get_db()

    def totals_by_cat(after):
        return [
            dict(r) for r in con.execute(
                "SELECT category, ROUND(SUM(duration_s)/3600.0,2) as hours "
                "FROM events WHERE ts>=? GROUP BY category ORDER BY hours DESC",
                (after,),
            ).fetchall()
        ]

    def total_h(after):
        return con.execute(
            "SELECT ROUND(SUM(duration_s)/3600.0,2) FROM events WHERE ts>=?", (after,)
        ).fetchone()[0] or 0

    ws = week_start()
    td = since(1)
    result = {
        "today": totals_by_cat(td),
        "week":  totals_by_cat(ws),
        "totals": {
            "today_h": total_h(td),
            "week_h":  total_h(ws),
            "week_goal": WEEK_GOAL_H,
            "manual_today": con.execute(
                "SELECT COUNT(*) FROM events WHERE ts>=? AND source='manual'", (td,)
            ).fetchone()[0] or 0,
        },
    }
    con.close()
    return jsonify(result)

@flask_app.route("/api/activity/start", methods=["POST"])
def api_activity_start():
    data = request.json or {}
    cat  = data.get("category", "Other")
    ts   = data.get("ts") or datetime.now(timezone.utc).isoformat()
    with _activity_lock:
        _active_activity["running"]    = True
        _active_activity["category"]   = cat
        _active_activity["ts"]         = ts
        _active_activity["stopped_by"] = None
    log.info("[activity] started: %s at %s", cat, ts[:16])
    return jsonify({"ok": True})

@flask_app.route("/api/activity/end", methods=["POST"])
def api_activity_end():
    with _activity_lock:
        if not _active_activity["running"]:
            return jsonify({"ok": False, "reason": "no active activity"})
        cat      = _active_activity["category"]
        start_ts = _active_activity["ts"]
        _active_activity["running"]    = False
        _active_activity["stopped_by"] = "manual"

    now   = datetime.now(timezone.utc)
    dur_s = (now - datetime.fromisoformat(start_ts)).total_seconds()
    if dur_s < 5:
        return jsonify({"ok": False, "reason": "too short"})

    con = get_db()
    con.execute(
        "INSERT INTO events (ts,app,title,category,source,duration_s) "
        "VALUES (?,?,?,?,'manual',?)",
        (start_ts, "manual", cat, cat, dur_s),
    )
    con.commit()
    con.close()
    log.info("[activity] ended: %s %.0fs", cat, dur_s)
    return jsonify({"ok": True, "duration_s": dur_s, "category": cat})

@flask_app.route("/api/activity/status")
def api_activity_status():
    with _activity_lock:
        return jsonify({
            "running":    _active_activity["running"],
            "category":   _active_activity["category"],
            "ts":         _active_activity["ts"],
            "stopped_by": _active_activity["stopped_by"],
        })

@flask_app.route("/api/activity/cancel", methods=["POST"])
def api_activity_cancel():
    with _activity_lock:
        _active_activity["running"]    = False
        _active_activity["category"]   = None
        _active_activity["ts"]         = None
        _active_activity["stopped_by"] = None
    return jsonify({"ok": True})
def api_manual():
    data = request.json or {}
    cat  = data.get("category", "Other")
    dur  = float(data.get("duration_s", 0))
    note = data.get("note", "")
    ts   = data.get("ts") or datetime.now(timezone.utc).isoformat()
    if dur <= 0:
        return jsonify({"error": "duration must be > 0"}), 400
    con = get_db()
    con.execute(
        "INSERT INTO events (ts,app,title,category,source,duration_s,note) VALUES (?,?,?,?,'manual',?,?)",
        (ts, "manual", note or cat, cat, dur, note),
    )
    con.commit()
    con.close()
    log.info("[manual] %s %.0fm at %s — %s", cat, dur / 60, ts[:16], note)
    return jsonify({"ok": True})

@flask_app.route("/api/delete/<int:eid>", methods=["DELETE"])
def api_delete(eid):
    con = get_db()
    con.execute("DELETE FROM events WHERE id=?", (eid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@flask_app.route("/api/export")
def api_export():
    days = int(request.args.get("days", 30))
    con = get_db()
    rows = con.execute(
        "SELECT ts,source,app,title,category,ROUND(duration_s/60.0,1) as duration_min,note "
        "FROM events WHERE ts>=? ORDER BY ts",
        (since(days),),
    ).fetchall()
    con.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["timestamp", "source", "app", "title", "category", "duration_min", "note"])
    for row in rows:
        w.writerow(list(row))
    fname = f"activity_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )

@flask_app.route("/api/timeline")
def api_timeline():
    """Return merged activity blocks for today (last 24h), sorted ascending."""
    after = since(1)
    con = get_db()
    rows = con.execute(
        "SELECT ts, category, source, duration_s "
        "FROM events WHERE ts >= ? ORDER BY ts ASC",
        (after,),
    ).fetchall()
    con.close()

    if not rows:
        return jsonify([])

    GAP_S = 120  # merge blocks of same category within 2 min of each other

    def parse(ts_str):
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

    blocks = []
    for row in rows:
        ts  = parse(row["ts"])
        end = ts + timedelta(seconds=row["duration_s"])
        cat = row["category"]
        src = row["source"]

        if blocks:
            last     = blocks[-1]
            last_end = parse(last["end"])
            gap      = (ts - last_end).total_seconds()
            if last["category"] == cat and gap <= GAP_S:
                last["end"]        = end.isoformat()
                last["duration_s"] += row["duration_s"]
                continue

        blocks.append({
            "category":   cat,
            "source":     src,
            "start":      ts.isoformat(),
            "end":        end.isoformat(),
            "duration_s": float(row["duration_s"]),
        })

    return jsonify(blocks)

@flask_app.route("/api/categories")
def api_categories():
    return jsonify({"auto": AUTO_CATEGORIES, "manual": MANUAL_CATEGORIES})

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — events will be classified as 'Other'")
    if not DASHBOARD.exists():
        log.error("dashboard.html not found — place it in the same folder as tracker.py")

    threading.Thread(target=poll_loop, daemon=True).start()
    log.info("Dashboard → http://localhost:%d", PORT)
    flask_app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)