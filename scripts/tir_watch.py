"""Watch the 'This Inevitable Ruin' re-download; on completion: trigger an ABS library
scan so it appears, then buzz the phone via ha-remind. Durable systemd --user unit."""
import json, time, subprocess, urllib.request

QBT = "http://localhost:8210/qbt/torrents/info"
NEEDLE = "inevitable"
ABS_LIB = "47b1ad1f-13c1-4365-88a7-fbb16c73ebb7"   # Shane's Books
ENVFILE = "/home/shane/.config/secrets/credentials.env"
deadline = time.time() + 12 * 3600


def _env(key):
    try:
        for line in open(ENVFILE):
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def abs_scan():
    base = _env("ABS_URL").rstrip("/"); tok = _env("ABS_TOKEN")
    if not (base and tok):
        return "no ABS creds"
    req = urllib.request.Request(f"{base}/api/libraries/{ABS_LIB}/scan", method="POST",
                                 headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"scan {r.status}"
    except Exception as e:
        return f"scan err {e}"


def notify(msg):
    try:
        subprocess.run(["/home/shane/.local/bin/ha-remind", msg], timeout=20)
    except Exception as e:
        print("notify err", e, flush=True)


print("[tir] watching for:", NEEDLE, flush=True)
while time.time() < deadline:
    try:
        info = json.load(urllib.request.urlopen(QBT, timeout=15))
    except Exception:
        time.sleep(45); continue
    t = next((x for x in info if NEEDLE in x["name"].lower()), None)
    if not t:
        time.sleep(60); continue
    print(f"[tir] {round(t['progress']*100)}% {t.get('state')}", flush=True)
    if t["progress"] >= 1.0:
        time.sleep(20)                 # let the watch-folder move settle
        r = abs_scan()
        notify(f"📗 'This Inevitable Ruin' re-downloaded & scanned into Audiobookshelf ({r}). The corrupted copy was removed.")
        print("[tir] DONE ->", r, flush=True)
        break
    time.sleep(90)
else:
    notify("⚠️ 'This Inevitable Ruin' re-download didn't finish in 12h — check qBittorrent.")
    print("[tir] deadline", flush=True)
