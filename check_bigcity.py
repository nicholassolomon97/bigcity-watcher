"""Big City Volleyball open-play watcher -> ntfy.

Renders the schedule iframe in headless Chromium (it is JS-loaded), turns the
visible text into "sessions", diffs against state_bigcity.json, and pushes an
ntfy notification when a session is NEW or a FULL session REOPENS.

Heuristic parsing lives in extract_sessions() - tune it once you see a real
run's debug output (set DEBUG=1, or see the uploaded artifact in Actions).
"""
import json, os, re, sys, urllib.request
from pathlib import Path

URL = os.environ.get("BCV_URL", "https://app.bigcityvolleyball.com/iframe/big-city-volleyball/tab/open-play")
TOPIC = os.environ["NTFY_TOPIC"]
STATE = Path("state_bigcity.json")
DEBUG = os.environ.get("DEBUG") == "1"
# Session lines with any of these words (case-insens.) count as FULL.
FULL_WORDS = re.compile(r"\b(full|sold out|waitlist|wait list|no spots|0 spots)\b", re.I)
# A line that looks like the start of a session (contains a time like 7:00 PM / 7pm).
TIME_RE = re.compile(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", re.I)
# Optional: only alert for these venue/keyword substrings (comma-separated env). Empty = all.
WATCH = [w.strip().lower() for w in os.environ.get("BCV_FILTER", "").split(",") if w.strip()]


def render_text() -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 2000})
        pg.goto(URL, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(3000)
        # Click any "load more" / "show more" buttons a few times.
        for _ in range(5):
            btn = pg.locator("button:has-text('more'), button:has-text('More')").first
            if btn.count() == 0:
                break
            try:
                btn.click(timeout=2000); pg.wait_for_timeout(1500)
            except Exception:
                break
        text = pg.inner_text("body")
        if DEBUG:
            Path("debug_page.txt").write_text(text)
            pg.screenshot(path="debug_page.png", full_page=True)
        b.close()
    return text


def extract_sessions(text: str) -> dict:
    """Return {session_key: is_full}. A session = a time line plus the next few lines."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    sessions, date = {}, ""
    date_re = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s", re.I)
    for i, l in enumerate(lines):
        if date_re.match(l) and not TIME_RE.search(l):
            date = l
        if TIME_RE.search(l):
            block = lines[i:i + 5]
            key = " | ".join([date] + block[:3])
            if WATCH and not any(w in key.lower() for w in WATCH):
                continue
            sessions[key] = bool(FULL_WORDS.search(" ".join(block)))
    return sessions


def notify(title: str, body: str, tags: str = "volleyball"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}", data=body.encode(),
        headers={"Title": title.encode("utf-8"), "Tags": tags, "Click": "https://www.bigcityvolleyball.com/adult-open-play"})
    urllib.request.urlopen(req, timeout=20)


def main():
    sessions = extract_sessions(render_text())
    print(f"Found {len(sessions)} sessions")
    if not sessions:
        print("WARNING: no sessions parsed - check debug output", file=sys.stderr)
        return  # don't clobber state on a bad render
    old = json.loads(STATE.read_text()) if STATE.exists() else None
    if old is not None:  # first run just seeds state, no alerts
        for k, full in sessions.items():
            if k not in old and not full:
                notify("🏐 New Big City open play", k.replace(" | ", "\n"))
            elif k in old and old[k] and not full:
                notify("🏐 Spot reopened (Big City)", k.replace(" | ", "\n"))
    STATE.write_text(json.dumps(sessions, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
