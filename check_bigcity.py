"""Big City Volleyball open-play watcher -> ntfy.

Renders the schedule iframe in headless Chromium (it is JS-loaded), turns the
visible text into "sessions", diffs against state_bigcity.json, and pushes ntfy notifications for:
  - NEW sessions (a new batch), and
  - sessions that were REMOVED from the page and later put BACK online.
One push per kind per run. Waitlist/full changes are ignored (BCV keeps full games visible).

Heuristic parsing lives in extract_sessions() - tune it once you see a real
run's debug output (set DEBUG=1, or see the uploaded artifact in Actions).
"""
import json, os, re, sys, urllib.request
from pathlib import Path

URL = os.environ.get("BCV_URL", "https://app.bigcityvolleyball.com/iframe/big-city-volleyball/tab/open-play")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE = Path("state_bigcity.json")
DEBUG = os.environ.get("DEBUG") == "1"
# Lines that describe availability / attendance (they change over time), so they never go in the key.
STATUS_LINE = re.compile(r"(waitlist|wait list|\bfull\b|sold out|filled|\d+\s+going|\d+\s+spots?|spots?\s+(left|open|available)|^(join|sign up|book|register)$)", re.I)
# A line that looks like the start of a session (contains a time like 7:00 PM / 7pm).
TIME_RE = re.compile(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b", re.I)
# Optional: only alert for these venue/keyword substrings (comma-separated env). Empty = all.
WATCH = [w.strip().lower() for w in os.environ.get("BCV_FILTER", "").split(",") if w.strip()]

MONTHS = {m: i for i, m in enumerate(["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1)}
TIME_RANGE = re.compile(
    r"^(\w{3})\w*\s+(\w{3})\w*\s+(\d{1,2})\s+(\d{1,2}(?::\d{2})?)\s?([AP]M)\s*-\s*(\d{1,2}(?::\d{2})?)\s?([AP]M)", re.I)
# Edit freely: exact venue name -> short form shown in the push.
VENUE_ABBR = {
    "High School for Environmental Studies": "HS for Env.",
    "Information Technology High School": "IT HS",
}
LEVEL_ABBR = {"advanced": "Adv.", "intermediate": "Int.", "beginner": "Beg.", "novice": "Nov."}


def _mins(t: str, ap: str) -> int:
    h, _, m = t.partition(":")
    h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
    return h * 60 + int(m or 0)


def _fmt_time(t: str, ap: str) -> str:
    h, _, m = t.partition(":")
    return f"{int(h)}:{m or '00'}{ap.upper()}"


def _fmt_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hrs = minutes / 60
    return f"{int(hrs)} hr" if hrs == 1 else (f"{int(hrs)} hrs" if hrs == int(hrs) else f"{hrs:g} hrs")


def _short_level(name: str) -> str:
    base = re.sub(r"\s+-\s+.*$", "", name)            # drop " - Left Court" etc.
    base = re.sub(r"\bopen play\b", "", base, flags=re.I).strip()
    fmt = "Women's" if re.search(r"women", base, re.I) else ("Co-Ed" if re.search(r"co[\s-]?ed", base, re.I) else "")
    lvl = next((v for k, v in LEVEL_ABBR.items() if k in base.lower()), "")
    return f"{fmt} {lvl}".strip() or base


def _sort_key(key: str):
    m = TIME_RANGE.match(key.split(" | ")[0])
    if not m:
        return (99, 99, 0, key)
    _, mon, day, t1, ap1, *_ = m.groups()
    mo = MONTHS[mon.lower()]
    return ((mo + 6) % 12, int(day), _mins(t1, ap1), key)  # Nov/Dec before Jan: rolls over the new year


def describe(key: str) -> str:
    """'Venue | Co-Ed Int. | Mon 11/16 | 8:00PM | 2 hrs' (falls back to the raw key)."""
    try:
        parts = [p.strip() for p in key.split(" | ")]
        m = TIME_RANGE.match(parts[0])
        rest = [p for p in parts[1:] if not p.startswith("$")]
        name = rest[0]
        venue = rest[1] if len(rest) > 1 else ""
        venue = VENUE_ABBR.get(venue, venue.replace("High School", "HS"))
        wd, mon, day, t1, ap1, t2, ap2 = m.groups()
        dur = (_mins(t2, ap2) - _mins(t1, ap1)) % (24 * 60)
        bits = [venue, _short_level(name), f"{wd.title()} {MONTHS[mon.lower()]:02d}/{int(day):02d}",
                _fmt_time(t1, ap1), _fmt_duration(dur)]
        return " | ".join(b for b in bits if b)
    except Exception:
        return key


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
    """Return {session_key: True}. A session = a time line plus the next few lines."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    sessions, date = {}, ""
    date_re = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s", re.I)
    for i, l in enumerate(lines):
        if date_re.match(l) and not TIME_RE.search(l):
            date = l
        if TIME_RE.search(l):
            block = lines[i:i + 5]
            key = " | ".join([x for x in block[:5] if not STATUS_LINE.search(x)][:3])
            if WATCH and not any(w in key.lower() for w in WATCH):
                continue
            sessions[key] = True
    return sessions


def notify(title: str, body: str):
    # JSON publish API (safe for UTF-8/emoji in the title). No ntfy tags: the title carries its own emoji.
    payload = json.dumps({"topic": TOPIC, "title": title, "message": body,
                          "click": "https://www.bigcityvolleyball.com/adult-open-play"}).encode()
    req = urllib.request.Request("https://ntfy.sh/", data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)


# A session must be absent for this many consecutive runs before it counts as "removed"
# (guards against a one-off partial page load). Set to 1 for the fastest detection.
MISS_RUNS = int(os.environ.get("BCV_MISS_RUNS", "2"))
# If a run sees fewer than this share of last run's sessions, assume a bad/partial render.
PARTIAL_RATIO = 0.5


def send_list(title: str, keys: list):
    keys = sorted(keys, key=_sort_key)
    lines = [describe(k) for k in keys[:8]]
    if len(keys) > 8:
        lines.append(f"+{len(keys) - 8} more")
    notify(title, "\n".join(lines))


def main():
    if not TOPIC:
        sys.exit("NTFY_TOPIC is empty - add it as a repository variable (Settings > Secrets and variables > Actions > Variables)")
    sessions = extract_sessions(render_text())
    present = set(sessions)
    print(f"Found {len(present)} sessions")
    if not present:
        print("WARNING: no sessions parsed - check debug output", file=sys.stderr)
        return  # don't clobber state on a bad render
    raw = json.loads(STATE.read_text()) if STATE.exists() else None
    if not (isinstance(raw, dict) and "seen" in raw):  # first run (or old-format state): seed only, no alert
        save(present, present, {}, set())
        return
    seen = set(raw["seen"])
    prev_current = set(raw.get("current", present))   # older state without "current": treat as unchanged
    missing = dict(raw.get("missing", {}))             # key -> consecutive runs absent
    removed = set(raw.get("removed", []))              # confirmed removed, waiting to come back

    new = [k for k in present if k not in seen]
    back = [k for k in present if k in removed]

    partial = len(present) < PARTIAL_RATIO * len(prev_current)
    if partial:
        print(f"WARNING: only {len(present)} of {len(prev_current)} sessions seen - skipping removal tracking this run", file=sys.stderr)
    for k in present:
        missing.pop(k, None)
        removed.discard(k)
    if not partial:
        for k in seen - present:
            if k in removed:
                continue
            missing[k] = missing.get(k, 0) + 1
            if missing[k] >= MISS_RUNS:
                removed.add(k)
                missing.pop(k, None)
                print(f"REMOVED (no alert): {k}")

    # Save state first-ish? No: notify first so a failed ntfy call fails the run and retries next time.
    if new:
        print(f"{len(new)} new sessions: {new}")
        send_list("\U0001F195 BCV New Open Play", new)           # 🆕
    if back:
        print(f"{len(back)} sessions back online: {back}")
        send_list("\U0001F504 BCV Back Online", back)            # 🔄
    save(seen | present, present if not partial else prev_current | present, missing, removed)


def save(seen, current, missing, removed):
    STATE.write_text(json.dumps({"seen": sorted(seen), "current": sorted(current),
                                 "missing": missing, "removed": sorted(removed)}, indent=1))


if __name__ == "__main__":
    main()
