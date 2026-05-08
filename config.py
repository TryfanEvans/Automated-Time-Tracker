"""
config.py — constants, logging, database helpers, and Flask app instance.
Everything else imports from here; this module imports nothing from the project.
"""

import os
import sqlite3
import logging
import csv
import io
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask

# ── Constants ──────────────────────────────────────────────────────────────────
AW_SERVER     = "http://localhost:5600"
POLL_INTERVAL = 5
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

TRACKER_WINDOW_HINTS = ("activity tracker", "localhost:5700", "127.0.0.1:5700")

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

# ── Flask app ──────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
DASHBOARD = Path(__file__).parent / "dashboard.html"

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

# ── Date helpers ───────────────────────────────────────────────────────────────
def since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

def today_start():
    """Midnight of the current local day as UTC ISO string."""
    local_now = datetime.now().astimezone()
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat()

def week_start():
    local_now = datetime.now().astimezone()
    local_monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_monday.astimezone(timezone.utc).isoformat()
