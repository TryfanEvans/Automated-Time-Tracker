"""
classifier.py — window title parsing, hard rules, and LLM classification.
"""

import re
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from anthropic import Anthropic

from config import (
    ANTHROPIC_API_KEY, MODEL, AUTO_CATEGORIES, TRACKER_WINDOW_HINTS, log
)

# ── Anthropic client ───────────────────────────────────────────────────────────
_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ── Classification cache ───────────────────────────────────────────────────────
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

# ── Browser app names ──────────────────────────────────────────────────────────
_BROWSER_APPS = {"brave", "chrome", "firefox", "chromium", "safari", "opera", "edge"}

# ── Hard rules ─────────────────────────────────────────────────────────────────
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
    "docs.anthropic.com": "Work/Study",
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

# ── Window parsing ─────────────────────────────────────────────────────────────
def parse_window(app: str, title: str, url: str | None = None):
    """Split a window title into (content, site).

    If a real URL is provided (from aw-watcher-web), extract the domain directly.
    Otherwise fall back to parsing the title string.
    """
    # Strip leading unread counts: "(2) Title..." -> "Title..."
    clean = re.sub(r'^\(\d+\)\s*', '', title)

    # Prefer real URL domain over title parsing
    if url:
        try:
            host = urlparse(url).hostname or ""
            site = host.removeprefix("www.")
            # Strip trailing " - SiteName - BrowserName" and " - BrowserName"
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

# ── LLM helpers ────────────────────────────────────────────────────────────────
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
    raw = _llm(
        f"Is this about programming, math, CS, EVM/blockchain, or academic study? "
        f"Reply YES, NO, or UNSURE.\nTitle: {content}",
        fallback="UNSURE"
    ).upper()
    if "YES" in raw: return "YES"
    if "NO"  in raw: return "NO"
    return "UNSURE"


def _is_admin(content: str) -> str:
    raw = _llm(
        f"Is this an administrative email or calendar event (signups, scheduling, uni admin, forms)? "
        f"Reply YES, NO, or UNSURE.\nTitle: {content}",
        fallback="UNSURE"
    ).upper()
    if "YES" in raw: return "YES"
    if "NO"  in raw: return "NO"
    return "UNSURE"


def _is_news(content: str) -> str:
    raw = _llm(
        f"Is this a news article about current events, politics, or world affairs (not CS/tech)? "
        f"Reply YES, NO, or UNSURE.\nTitle: {content}",
        fallback="UNSURE"
    ).upper()
    if "YES" in raw: return "YES"
    if "NO"  in raw: return "NO"
    return "UNSURE"


def _general_classify(app: str, site: str | None, content: str) -> str:
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


# ── Main classify entry point ──────────────────────────────────────────────────
def classify(app: str, title: str, url: str | None = None, last_category: str = "Other") -> str:
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
        return "Work/Study" if last_category == "Work/Study" else domain_default

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

    if result is None:
        result = _general_classify(app, site, content)
        log.info("[general] %s -> %s", content[:50], result)

    _classify_cache[key] = result
    _save_cache(_classify_cache)
    return result