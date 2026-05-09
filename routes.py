"""
routes.py — all Flask route handlers.
"""

import csv
import io
from datetime import datetime, timezone, timedelta

from flask import jsonify, request, send_from_directory, Response

from config import (
    flask_app, DASHBOARD, AUTO_CATEGORIES, MANUAL_CATEGORIES, WEEK_GOAL_H,
    get_db, since, today_start, week_start, log
)
from polling import (
    _active_activity, _activity_lock,
    _window_state, _window_lock,
    auto_stop_activity, is_tracker_window
)


@flask_app.route("/")
def index():
    return send_from_directory(str(DASHBOARD.parent), DASHBOARD.name)


@flask_app.route("/api/events")
def api_events():
    days = int(request.args.get("days", 1))
    con  = get_db()
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
            "SELECT ROUND(SUM(duration_s)/3600.0,2) FROM events "
            "WHERE ts>=? AND category='Work/Study'", (after,)
        ).fetchone()[0] or 0

    ws = week_start()
    td = today_start()
    result = {
        "today": totals_by_cat(td),
        "week":  totals_by_cat(ws),
        "totals": {
            "today_h":     productive_h(td),
            "week_h":      productive_h(ws),
            "week_goal":   WEEK_GOAL_H,
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
        "INSERT INTO events (ts,app,title,category,source,duration_s,note) "
        "VALUES (?,?,?,?,'manual',?,?)",
        (ts, "manual", note or cat, cat, dur, note),
    )
    con.commit()
    con.close()
    log.info("[manual] %s %.0fm at %s — %s", cat, dur / 60, ts[:16], note)
    return jsonify({"ok": True})


@flask_app.route("/api/update/<int:eid>", methods=["POST"])
def api_update(eid):
    data = request.json or {}
    fields, values = [], []
    if "category" in data:
        fields.append("category=?"); fields.append("title=?")
        values += [data["category"], data["category"]]
    if "note" in data:
        fields.append("note=?"); values.append(data["note"])
    if "ts" in data:
        fields.append("ts=?"); values.append(data["ts"])
    if "duration_s" in data:
        fields.append("duration_s=?"); values.append(float(data["duration_s"]))
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    values.append(eid)
    con = get_db()
    con.execute(f"UPDATE events SET {', '.join(fields)} WHERE id=?", values)
    con.commit()
    con.close()
    log.info("[update] row %d — %s", eid, data)
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
    con  = get_db()
    rows = con.execute(
        "SELECT ts,source,app,title,category,ROUND(duration_s/60.0,1) as duration_min,note "
        "FROM events WHERE ts>=? ORDER BY ts",
        (since(days),),
    ).fetchall()
    con.close()
    out = io.StringIO()
    w   = csv.writer(out)
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
    """Return merged activity blocks for today, sorted ascending."""
    after = today_start()
    con   = get_db()
    rows  = con.execute(
        "SELECT id, ts, category, source, duration_s, note "
        "FROM events WHERE ts >= ? ORDER BY ts ASC",
        (after,),
    ).fetchall()
    con.close()

    if not rows:
        return jsonify([])

    GAP_S = 120

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
            # Only merge auto blocks — manual blocks are always kept distinct
            if last["category"] == cat and gap <= GAP_S and src == "auto" and last["source"] == "auto":
                last["end"]        = end.isoformat()
                last["duration_s"] += row["duration_s"]
                continue

        blocks.append({
            "id":         row["id"] if src == "manual" else None,
            "category":   cat,
            "source":     src,
            "note":       row["note"] if src == "manual" else "",
            "start":      ts.isoformat(),
            "end":        end.isoformat(),
            "duration_s": float(row["duration_s"]),
        })

    return jsonify(blocks)


@flask_app.route("/api/categories")
def api_categories():
    return jsonify({"auto": AUTO_CATEGORIES, "manual": MANUAL_CATEGORIES})