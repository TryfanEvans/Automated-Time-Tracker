"""
polling.py — ActivityWatch polling, window state management, poll loop,
             graceful shutdown, and D-Bus suspend/resume handling.
"""

import time
import signal
import threading
import logging
from datetime import datetime, timezone

import requests

from config import (
    AW_SERVER, POLL_INTERVAL, TRACKER_WINDOW_HINTS,
    get_db, log
)
from classifier import classify, _BROWSER_APPS

# ── Shared window state ────────────────────────────────────────────────────────
_last_key      = None
_last_category = "Other"

_window_state = {
    "row_id":   None,
    "app":      "",
    "title":    "",
    "category": "Other",
    "start_ts": None,
}
_window_lock = threading.Lock()

# ── Shared activity state ──────────────────────────────────────────────────────
_active_activity = {
    "running":    False,
    "category":   None,
    "ts":         None,
    "stopped_by": None,
}
_activity_lock = threading.Lock()

# ── Window helpers ─────────────────────────────────────────────────────────────
def is_tracker_window(app: str, title: str) -> bool:
    combined = (app + " " + title).lower()
    return any(h in combined for h in TRACKER_WINDOW_HINTS)


def insert_window_row(app: str, title: str, category: str, start_ts: datetime) -> int:
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


def auto_stop_activity(app: str, title: str):
    """Log and clear the active manual activity. Called from poll loop."""
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

# ── AFK detection ─────────────────────────────────────────────────────────────
AFK_THRESHOLD_S = 300  # 5 minutes

def is_afk() -> bool:
    """Return True if ActivityWatch reports the user has been AFK for AFK_THRESHOLD_S."""
    try:
        r = requests.get(f"{AW_SERVER}/api/0/buckets", timeout=3)
        if not r.ok:
            return False
        for bid in r.json():
            if "aw-watcher-afk" in bid:
                r2 = requests.get(
                    f"{AW_SERVER}/api/0/buckets/{bid}/events",
                    params={"limit": 1},
                    timeout=3,
                )
                if r2.ok:
                    events = r2.json()
                    if events:
                        e = events[0]
                        if e["data"].get("status") == "afk":
                            afk_since = datetime.fromisoformat(
                                e["timestamp"].replace("Z", "+00:00")
                            )
                            afk_s = (datetime.now(timezone.utc) - afk_since).total_seconds()
                            return afk_s >= AFK_THRESHOLD_S
    except Exception as ex:
        log.debug(f"AFK poll error: {ex}")
    return False


# ── ActivityWatch polling ──────────────────────────────────────────────────────
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
                        d     = events[0]["data"]
                        app   = d.get("app", "")
                        title = d.get("title", "")
                        url   = _get_web_url() if app.lower() in _BROWSER_APPS else None
                        return app, title, url
    except Exception as e:
        log.debug(f"AW poll: {e}")
    return None

# ── Poll loop ──────────────────────────────────────────────────────────────────
def poll_loop():
    global _last_key, _last_category
    log.info("Starting event-driven window tracker (check every %ds)", POLL_INTERVAL)

    CHECKPOINT_INTERVAL = 300
    last_checkpoint = datetime.now(timezone.utc)

    _was_afk = False

    while True:
        try:
            now    = datetime.now(timezone.utc)

            # AFK check — flush and pause tracking if idle
            afk = is_afk()
            if afk and not _was_afk:
                log.info("[afk] user went AFK — flushing current window")
                flush_current_window()
                with _window_lock:
                    _window_state["row_id"]   = None
                    _window_state["start_ts"] = None
                _was_afk = True
            elif not afk and _was_afk:
                log.info("[afk] user returned — resuming tracking")
                _was_afk = False
                _last_key = None  # force new row on resume

            if afk:
                time.sleep(POLL_INTERVAL)
                continue

            result = get_current_window()

            if result:
                app, title, url = result

                if not app and not title:
                    time.sleep(POLL_INTERVAL)
                    continue
                if app.lower() == "unknown" or title.lower() == "unknown":
                    time.sleep(POLL_INTERVAL)
                    continue

                key = f"{app}|{title}"

                if key != _last_key:
                    flush_current_window()

                    with _activity_lock:
                        act_running = _active_activity["running"]
                    if act_running and not is_tracker_window(app, title):
                        auto_stop_activity(app, title)

                    _last_category = classify(app, title, url, _last_category)
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
                    if (now - last_checkpoint).total_seconds() >= CHECKPOINT_INTERVAL:
                        flush_current_window()
                        last_checkpoint = now

        except Exception as e:
            log.error(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)

# ── Graceful shutdown ──────────────────────────────────────────────────────────
def _shutdown_handler(signum, frame):
    log.info("Shutdown signal received — flushing current window")
    flush_current_window()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT,  _shutdown_handler)

# ── D-Bus sleep listener ───────────────────────────────────────────────────────
def _start_sleep_listener():
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