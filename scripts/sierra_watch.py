"""Watch the two Sierra Ferrell grabs; on each completion: rescan Navidrome + push a
phone notification via ha-remind. Durable (run as a systemd --user transient unit so it
survives session resets). Exits when both are done or after the deadline."""
import os, sys, json, time, hashlib, secrets, subprocess, urllib.request, urllib.parse

QBT = "http://localhost:8210/qbt/torrents/info"
TARGETS = {"Trail Of Flowers": "Sierra Ferrell — Trail of Flowers",
           "Long Time Coming": "Sierra Ferrell — Long Time Coming",
           "Metamodern": "Sturgill Simpson — Metamodern Sounds",
           "Sailor": "Sturgill Simpson — A Sailor's Guide to Earth"}
done = set()
deadline = time.time() + 12 * 3600   # give Long Time Coming up to 12h to find peers

def navidrome_rescan():
    try:
        import yaml
        cfg = yaml.safe_load(open("/home/shane/argus/config/navidrome.yaml"))
    except Exception:
        return "no navidrome.yaml"
    def find(d, keys):
        if isinstance(d, dict):
            for k, v in d.items():
                if k.lower() in keys and isinstance(v, str): yield (k.lower(), v)
                yield from find(v, keys)
        elif isinstance(d, list):
            for v in d: yield from find(v, keys)
    vals = dict(find(cfg, {"base_url", "url", "host", "user", "username", "password", "pass"}))
    base = (vals.get("base_url") or vals.get("url") or vals.get("host") or "http://192.168.4.206:4533").rstrip("/")
    user = vals.get("user") or vals.get("username"); pw = vals.get("password") or vals.get("pass")
    if not (user and pw): return "no creds"
    salt = secrets.token_hex(6); tok = hashlib.md5((pw + salt).encode()).hexdigest()
    q = urllib.parse.urlencode({"u": user, "t": tok, "s": salt, "v": "1.16.1", "c": "argus", "f": "json"})
    try:
        urllib.request.urlopen(f"{base}/rest/startScan?{q}", timeout=20).read()
        return "rescan ok"
    except Exception as e:
        return f"rescan err {e}"

def notify(msg):
    try: subprocess.run(["/home/shane/.local/bin/ha-remind", msg], timeout=20)
    except Exception as e: print("notify err", e, flush=True)

while len(done) < len(TARGETS) and time.time() < deadline:
    try:
        info = json.load(urllib.request.urlopen(QBT, timeout=15))
    except Exception:
        time.sleep(60); continue
    for needle, label in TARGETS.items():
        if needle in done: continue
        t = next((x for x in info if needle.lower() in x["name"].lower()), None)
        if t and t["progress"] >= 1.0:
            done.add(needle)
            r = navidrome_rescan()
            notify(f"🎸 Sierra Ferrell — {label} finished downloading. ({r})")
            print(f"[{label}] done -> {r}", flush=True)
    time.sleep(90)
print("WATCHER DONE — finished:", sorted(done), flush=True)
