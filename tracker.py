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

import re as _re
import re

# Browser app names as reported by ActivityWatch
_BROWSER_APPS = {"brave", "chrome", "firefox", "chromium", "safari", "opera", "edge"}

def parse_window(app: str, title: str, url: str | None = None):
    """Split a window title into (content, site).

    If a real URL is provided (from aw-watcher-web), extract the domain directly.
    Otherwise fall back to parsing the title string.
    """
    from urllib.parse import urlparse

    # Strip leading unread counts: "(2) Title..." -> "Title..."
    clean = re.sub(r'^\(\d+\)\s*', '', title)

    # Prefer real URL domain over title parsing
    if url:
        try:
            host = urlparse(url).hostname or ""
            site = host.removeprefix("www.")
            # Strip trailing " - SiteName - BrowserName" and " - BrowserName" from title
            page_title = clean
            m = re.match(r'^(.*?) - [^-]+ - ' + re.escape(app) + r'$', clean, re.IGNORECASE)
            if m:
                page_title = m.group(1).strip()
            else:
                m = re.match(r'^(.*?) - ' + re.escape(app) + r'$', clean, re.IGNORECASE)
                if m:
                    page_title = m.group(1).strip()
            return page_title, site if site else None
        except Exception:
            pass

    if app.lower() in _BROWSER_APPS:
        # Three-part: "Content - Site - AppName"
        m = re.match(r'^(.*?) - ([^-]+) - ' + re.escape(app) + r'$', clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        # Two-part: "Content - AppName"
        m = re.match(r'^(.*?) - ' + re.escape(app) + r'$', clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), None

    return clean, None


# Hard rules: site or app alone is sufficient -- no LLM needed.
_HARD_RULES_SITE = {
    # Social / entertainment -- always recreational
    "instagram":        "Instagram",
    "facebook":         "Facebook",
    "twitter":          "Twitter",
    "x.com":            "Twitter",
    "tiktok":           "TikTok",
    "netflix":          "Entertainment",
    "netflix.com":      "Entertainment",
    "spotify":          "Entertainment",
    "open.spotify.com": "Entertainment",
    "twitch":           "Entertainment",
    "twitch.tv":        "Entertainment",
    "primevideo.com":   "Entertainment",
    # Study platforms -- always Work/Study
    "updraft.cyfrin.io":  "Work/Study",
    "cyfrin.io":          "Work/Study",
    "udemy.com":          "Work/Study",
    "coursera.org":       "Work/Study",
    "edx.org":            "Work/Study",
    "khanacademy.org":    "Work/Study",
    "brilliant.org":      "Work/Study",
    "leetcode.com":       "Work/Study",
    "arxiv.org":          "Work/Study",
    "github.com":         "Work/Study",
    "stackoverflow.com":  "Work/Study",
    "docs.anthropic.com": "Work/Study",
    "claude.ai":          "Work/Study",
    "app.claude.ai":      "Work/Study",
    "chatgpt.com":        "Work/Study",
    "gemini.google.com":  "Work/Study",
}

_HARD_RULES_APP = {
    "code":           "Work/Study",   # VSCode reports as "Code"
    "gnome-terminal": "Work/Study",
    "terminal":       "Work/Study",
    "konsole":        "Work/Study",
    "kitty":          "Work/Study",
    "alacritty":      "Work/Study",
    "steam_app_0":    "Entertainment",
    "heroic":         "Entertainment",
    "zenity":         "Entertainment",
}


def _llm(prompt: str, fallback: str) -> str:
    """Single LLM call, returns stripped response text or fallback on error."""
    try:
        msg = _client.messages.create(
            model=MODEL,
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"LLM error: {e}")
        return fallback


def _is_work_study(content: str) -> str:
    """Ask LLM if content is work/study related. Returns YES, NO, or UNSURE."""
    raw = _llm(
        f"Is this about programming, math, CS, EVM/blockchain, or academic study? "
        f"Reply YES, NO, or UNSURE.\nTitle: {content}",
        fallback="UNSURE"
    ).upper()
    if "YES" in raw:   return "YES"
    if "NO"  in raw:   return "NO"
    return "UNSURE"


def _is_admin(content: str) -> str:
    """Ask LLM if email/calendar content is administrative. Returns YES, NO, or UNSURE."""
    raw = _llm(
        f"Is this an administrative email or calendar event (signups, scheduling, uni admin, forms)? "
        f"Reply YES, NO, or UNSURE.\nTitle: {content}",
        fallback="UNSURE"
    ).upper()
    if "YES" in raw:   return "YES"
    if "NO"  in raw:   return "NO"
    return "UNSURE"


def _is_news(content: str) -> str:
    """Ask LLM if content is news (not tech/CS). Returns YES, NO, or UNSURE."""
    raw = _llm(
        f"Is this a news article about current events, politics, or world affairs (not CS/tech)? "
        f"Reply YES, NO, or UNSURE.\nTitle: {content}",
        fallback="UNSURE"
    ).upper()
    if "YES" in raw:   return "YES"
    if "NO"  in raw:   return "NO"
    return "UNSURE"


def _general_classify(app: str, site: str | None, content: str) -> str:
    """Full category classification for cases no targeted prompt covers."""
    site_line = f"Site: {site}" if site else "Site: (none)"
    raw = _llm(
        f"Classify this computer activity into one category.\n"
        f"App: {app}\n{site_line}\nContent: {content}\n"
        f"Categories: {', '.join(AUTO_CATEGORIES)}\n"
        f"Rules: Work/Study=code/math/CS/EVM/academic; Reading=book/paper PDF; "
        f"Messaging=Discord DM/WhatsApp; Discord=Discord server; Admin=uni portal/calendar; "
        f"Browsing=unrecognised web; Other=everything else.\n"
        f"Reply with ONLY the category name.",
        fallback="Other"
    )
    for cat in AUTO_CATEGORIES:
        if cat.lower() in raw.lower():
            return cat
    return "Other"


def classify(app: str, title: str, url: str | None = None) -> str:
    """Parse window, apply hard rules, targeted yes/no prompts, fallback to general LLM."""
    if not _client:
        return "Other"

    # Tracker window -- deterministic
    combined = (app + " " + title).lower()
    if any(h in combined for h in TRACKER_WINDOW_HINTS):
        return "Admin"

    key = f"{app}|{title}"
    if key in _classify_cache:
        return _classify_cache[key]

    content, site = parse_window(app, title, url)
    site_lower = site.lower() if site else ""

    # Hard rules -- site
    if site:
        rule = _HARD_RULES_SITE.get(site_lower)
        if rule:
            _classify_cache[key] = rule
            _save_cache(_classify_cache)
            log.info("[rule/site] %s -> %s", site, rule)
            return rule

    # Hard rules -- app
    rule = _HARD_RULES_APP.get(app.lower())
    if rule:
        _classify_cache[key] = rule
        _save_cache(_classify_cache)
        log.info("[rule/app] %s -> %s", app, rule)
        return rule

    def _unsure(domain_default):
        return "Work/Study" if _last_category == "Work/Study" else domain_default

    # Targeted yes/no prompts
    result = None

    if site_lower == "youtube":
        ans = _is_work_study(content)
        result = "Work/Study" if ans == "YES" else ("YouTube" if ans == "NO" else _unsure("YouTube"))
        log.info("[yn/youtube] %s -> %s (%s)", content[:50], result, ans)

    elif site_lower in ("reddit", "reddit.com", "old.reddit.com", "www.reddit.com"):
        ans = _is_work_study(content)
        result = "Work/Study" if ans == "YES" else ("Reddit" if ans == "NO" else _unsure("Reddit"))
        log.info("[yn/reddit] %s -> %s (%s)", content[:50], result, ans)

    elif site_lower in ("gmail", "outlook", "mail"):
        ans = _is_admin(content)
        result = "Admin" if ans == "YES" else ("Browsing" if ans == "NO" else _unsure("Admin"))
        log.info("[yn/mail] %s -> %s (%s)", content[:50], result, ans)

    elif site_lower in ("bbc", "abc", "cnn", "guardian", "reuters", "nytimes",
                        "mit technology review", "hacker news", "ars technica"):
        ans = _is_news(content)
        result = "News" if ans == "YES" else ("Work/Study" if ans == "NO" else _unsure("News"))
        log.info("[yn/news] %s -> %s (%s)", content[:50], result, ans)

    # Unknown site/app -- general classifier
    if result is None:
        result = _general_classify(app, site, content)
        log.info("[general] %s -> %s", content[:50], result)

    _classify_cache[key] = result
    _save_cache(_classify_cache)
    return result
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

def _get_web_url() -> str | None:
    """Return the current browser URL from aw-watcher-web, or None if unavailable."""
    try:
        r = requests.get(f"{AW_SERVER}/api/0/buckets", timeout=3)
        if not r.ok:
            return None
        for bid in r.json():
            if "aw-watcher-web" in bid:
                r2 = requests.get(
                    f"{AW_SERVER}/api/0/buckets/{bid}/events",
                    params={"limit": 1},
                    timeout=3,
                )
                if r2.ok:
                    events = r2.json()
                    if events:
                        return events[0]["data"].get("url")
    except Exception as e:
        log.debug(f"AW web poll: {e}")
    return None


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
                        app   = d.get("app", "")
                        title = d.get("title", "")
                        url   = _get_web_url() if app.lower() in _BROWSER_APPS else None
                        return app, title, url
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
                app, title, url = result

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

                    # Classify new window and insert a fresh row (skip tracker itself)
                    _last_category = classify(app, title, url)
                    _last_key      = key
                    last_checkpoint = now

                    if is_tracker_window(app, title):
                        row_id = None
                        log.info("[skip] tracker window — no row inserted")
                    else:
                        row_id = insert_window_row(app, title, _last_category, now)
                        log.info("[new] %s | %.55s → %s", app, title, _last_category)

                    with _window_lock:
                        _window_state["row_id"]   = row_id
                        _window_state["app"]      = app
                        _window_state["title"]    = title
                        _window_state["category"] = _last_category
                        _window_state["start_ts"] = now

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

def today_start():
    """Midnight of the current local day as UTC ISO string."""
    local_now = datetime.now().astimezone()  # timezone-aware in system local tz
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat()

def week_start():
    local_now = datetime.now().astimezone()  # timezone-aware in system local tz
    local_monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_monday.astimezone(timezone.utc).isoformat()

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
        (today_start() if days <= 1 else since(days),),
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

    def productive_h(after):
        return con.execute(
            "SELECT ROUND(SUM(duration_s)/3600.0,2) FROM events WHERE ts>=? AND category='Work/Study'", (after,)
        ).fetchone()[0] or 0

    ws = week_start()
    td = today_start()
    result = {
        "today": totals_by_cat(td),
        "week":  totals_by_cat(ws),
        "totals": {
            "today_h": productive_h(td),
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

@flask_app.route("/api/manual", methods=["POST"])
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
    after = today_start()
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