#!/usr/bin/env python3
"""Father-of-Constructs watcher — durable auto-grab of the 4 missing audiobooks.

Shane has Book 5 (Ultimate Crafter); this grabs Books 1-4 of Aaron Renfroe's
"Father of Constructs" LitRPG series the moment AudiobookBay's search recovers.

ABB search is the only source for these audiobooks (Prowlarr only has the ebooks),
and right now ABB serves its homepage to every query (throttled). So we retry the
lane's search on a schedule, across a couple of reachable mirrors, and grab() each
book as soon as a real match appears. Phone push per grab + a final "all 4" push.

Run durably:  systemd-run --user --unit=constructs-watch \
                 --working-directory=/home/shane/argus \
                 /home/shane/argus/.venv/bin/python scripts/constructs_watch.py
Stop:         systemctl --user stop constructs-watch
"""
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from argus.tools import audiobook as a  # noqa: E402

STATE = Path.home() / ".constructs_watch.json"
LOG = Path.home() / "constructs_watch.log"
MIRRORS = ["https://audiobookbay.lu", "https://audiobookbay.li"]
INTERVAL = 1200          # 20 min between rounds
MAX_HOURS = 72           # give up after 3 days (ABB throttles clear well within this)
GAP = 8                  # polite gap between per-book ABB requests

# Each book: a query + a predicate that confirms a search result is really this book.
BOOKS = [
    {"n": 1, "q": "Janitor Killed World Boss Renfroe",
     "ok": lambda t: "janitor" in t and any(w in t for w in ("renfroe", "construct", "world boss"))},
    {"n": 2, "q": "Father of Constructs Master of Steel Renfroe",
     "ok": lambda t: "steel" in t and any(w in t for w in ("renfroe", "construct", "master"))},
    {"n": 3, "q": "Father of Constructs Eldritch Artisan Renfroe",
     "ok": lambda t: "eldritch" in t and any(w in t for w in ("renfroe", "construct", "artisan"))},
    {"n": 4, "q": "Father of Constructs Artificer Quest Renfroe",
     "ok": lambda t: "artificer" in t and any(w in t for w in ("renfroe", "construct", "quest"))},
]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def push(msg: str) -> None:
    """Out-of-band phone confirmation (reliable even if Forge isn't rendering)."""
    try:
        subprocess.run([str(Path.home() / ".local/bin/ha-remind"), msg], timeout=20)
    except Exception as e:
        log(f"push failed: {e}")


def load_done() -> set:
    try:
        return set(json.loads(STATE.read_text()))
    except Exception:
        return set()


def save_done(done: set) -> None:
    STATE.write_text(json.dumps(sorted(done)))


def search_any_mirror(query: str):
    """Try the lane's search across mirrors; return real posts or []."""
    base = a._cfg()
    for m in MIRRORS:
        cfg = copy.deepcopy(base)
        cfg["abb_base"] = m
        a._cfg_cache = cfg
        try:
            res = a.search(query)
        except Exception:
            continue
        if res:
            return res, m
        time.sleep(GAP)
    return [], None


def main() -> None:
    done = load_done()
    log(f"watcher armed — need books {[b['n'] for b in BOOKS if b['n'] not in done]} "
        f"(already done: {sorted(done)})")
    push("📚 Constructs watcher armed — will grab Books 1-4 when ABB search recovers")
    deadline = time.time() + MAX_HOURS * 3600

    while time.time() < deadline:
        for b in BOOKS:
            if b["n"] in done:
                continue
            posts, mirror = search_any_mirror(b["q"])
            match = next((p for p in posts if b["ok"](p["title"].lower())), None)
            if not match:
                continue
            try:
                a.grab(match["url"], match["title"])
                done.add(b["n"])
                save_done(done)
                log(f"GRABBED Book {b['n']}: {match['title']}  (via {mirror})")
                push(f"📖 Got Father of Constructs Book {b['n']}: {match['title']}")
            except Exception as e:
                log(f"grab Book {b['n']} failed: {e}")
            time.sleep(GAP)

        if all(b["n"] in done for b in BOOKS):
            log("ALL 4 grabbed — done.")
            push("✅ All 4 Father of Constructs audiobooks grabbed — series complete")
            return
        time.sleep(INTERVAL)

    missing = [b["n"] for b in BOOKS if b["n"] not in done]
    log(f"deadline reached; still missing {missing}")
    push(f"⏰ Constructs watcher gave up after {MAX_HOURS}h; still missing books {missing}")


if __name__ == "__main__":
    main()
