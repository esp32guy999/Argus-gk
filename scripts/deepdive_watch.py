"""Watch the 'Deep Dive 90s Rock' grab; on completion:
  1. beets-import the folder into /music (Navidrome's library) via the beets container on
     glassgarden — tags via MusicBrainz where confident, falls back to as-is (keeps the
     PMEDIA ID3 tags) so nothing is skipped; copy:yes leaves the files seeding.
  2. rescan Navidrome (Subsonic startScan) so it appears immediately.
  3. push a phone notification via ha-remind.
Durable: run as a systemd --user transient unit so it survives session resets.
Exits when done or after the deadline (low-seed torrent -> generous 24h)."""
import os, sys, json, time, base64, hashlib, secrets, subprocess, urllib.request, urllib.parse

QBT = "http://localhost:8210/qbt/torrents/info"
NEEDLE = "deep dive 90"                 # match the torrent by name
# qBit container path prefix -> beets container path prefix
#   qBit  /data            == host /mnt/user/downloads/complete
#   beets /downloads       == host /mnt/user/downloads
QBT_PREFIX, BEETS_PREFIX = "/data/", "/downloads/complete/"
OVERRIDE = "import:\n  quiet_fallback: asis\n"   # keep existing tags on a non-match, never skip
deadline = time.time() + 24 * 3600


def sh(args, timeout=1800):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def beets_import(beets_path):
    # write the quiet_fallback override where the beets container can read it (/config)
    b64 = base64.b64encode(OVERRIDE.encode()).decode()
    sh(["ssh", "-o", "ConnectTimeout=10", "unraid",
        f"echo {b64} | base64 -d > /mnt/user/appdata/beets/override.yaml"], timeout=60)
    # import the exact folder; ferry the path via base64 to dodge spaces/brackets/emoji
    pb64 = base64.b64encode(beets_path.encode()).decode()
    remote = (f'P=$(echo {pb64} | base64 -d); '
              f'docker exec beets beet -c /config/override.yaml import -q "$P"')
    rc, out = sh(["ssh", "-o", "ConnectTimeout=10", "unraid", remote], timeout=2400)
    return rc, out


def navidrome_rescan():
    try:
        import yaml
        cfg = yaml.safe_load(open("/home/shane/argus/config/navidrome.yaml"))
    except Exception:
        return "no navidrome.yaml"

    def find(d, keys):
        if isinstance(d, dict):
            for k, v in d.items():
                if k.lower() in keys and isinstance(v, str):
                    yield (k.lower(), v)
                yield from find(v, keys)
        elif isinstance(d, list):
            for v in d:
                yield from find(v, keys)

    vals = dict(find(cfg, {"base_url", "url", "host", "user", "username", "password", "pass"}))
    base = (vals.get("base_url") or vals.get("url") or vals.get("host") or "http://192.168.4.206:4533").rstrip("/")
    user = vals.get("user") or vals.get("username"); pw = vals.get("password") or vals.get("pass")
    if not (user and pw):
        return "no creds"
    salt = secrets.token_hex(6); tok = hashlib.md5((pw + salt).encode()).hexdigest()
    q = urllib.parse.urlencode({"u": user, "t": tok, "s": salt, "v": "1.16.1", "c": "argus", "f": "json"})
    try:
        urllib.request.urlopen(f"{base}/rest/startScan?{q}", timeout=20).read()
        return "rescan ok"
    except Exception as e:
        return f"rescan err {e}"


def notify(msg):
    try:
        subprocess.run(["/home/shane/.local/bin/ha-remind", msg], timeout=20)
    except Exception as e:
        print("notify err", e, flush=True)


print("[deepdive] watching for:", NEEDLE, flush=True)
while time.time() < deadline:
    try:
        info = json.load(urllib.request.urlopen(QBT, timeout=15))
    except Exception:
        time.sleep(60); continue
    t = next((x for x in info if NEEDLE in x["name"].lower()), None)
    if not t:
        time.sleep(90); continue
    pct = round(t["progress"] * 100)
    print(f"[deepdive] {t['name'][:50]} {pct}% {t.get('state')}", flush=True)
    if t["progress"] >= 1.0:
        content = t.get("content_path") or (t.get("save_path", "") + "/" + t["name"])
        beets_path = content.replace(QBT_PREFIX, BEETS_PREFIX, 1)
        print("[deepdive] complete -> beets import:", beets_path, flush=True)
        rc, out = beets_import(beets_path)
        tail = "\n".join(out.strip().splitlines()[-8:])
        print(f"[deepdive] beets rc={rc}\n{tail}", flush=True)
        r = navidrome_rescan()
        # count albums beets reported (rough: lines it printed as imported)
        n = out.count("/music/") or out.lower().count("album")
        notify(f"🎵 Deep Dive 90s Rock finished — beets imported into Navidrome "
               f"(rc={rc}). {r}")
        print("WATCHER DONE", flush=True)
        sys.exit(0)
    time.sleep(90)
print("WATCHER DEADLINE — Deep Dive never completed", flush=True)
notify("⚠️ Deep Dive 90s Rock didn't finish in 24h (low seeds). Check qBit.")
