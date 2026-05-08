#!/usr/bin/env python3
"""
Activity Tracker
Polls ActivityWatch, classifies with Claude Haiku, serves dashboard on localhost:5700

Setup:
    pip install flask anthropic requests
    export ANTHROPIC_API_KEY="sk-ant-..."
    python3 main.py

Dashboard: http://localhost:5700
"""

import threading

from config import init_db, flask_app, ANTHROPIC_API_KEY, PORT, DASHBOARD, log
import routes  # registers all Flask routes as a side effect
from polling import poll_loop

if __name__ == "__main__":
    init_db()
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — events will be classified as 'Other'")
    if not DASHBOARD.exists():
        log.error("dashboard.html not found — place it in the same folder as main.py")

    threading.Thread(target=poll_loop, daemon=True).start()
    log.info("Dashboard → http://localhost:%d", PORT)
    flask_app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
