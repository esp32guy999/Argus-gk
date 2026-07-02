"""Persistent importer for qBit's 'music-staging' category. On each torrent's completion:
  1. beets-import the folder into /music (Navidrome's library) via the beets container on
     glassgarden — MusicBrainz tags where confident, falls back to as-is (keeps the source
     ID3 tags) so nothing is skipped; copy:yes leaves the files seeding.
  2. rescan Navidrome (Subsonic startScan).
  3. push a phone notification via ha-remind.
Handles multiple torrents + any future staging grabs. A persisted done-set stops re-import
across restarts. Durable: run as a systemd --user unit. Runs indefinitely."""
import os, sys, json, time, base64, hashlib, secrets, subprocess, urllib.request, urllib.parse

QBT = "http://localhost:8210/qbt/torrents/info"
CATEGORY = "music-staging"
STATE = "/home/shane/argus/scripts/.music_staging_done"
QBT_PREFIX, BEETS_PREFIX = "/data/", "/downloads/complete/"   # qBit /data -> beets /downloads/complete
OVERRIDE = "import:\n  quiet_fallback: asis\n"                # keep existing tags on non-match, never skip


def load_done():
    try:
        return set(open(STATE).read().split())
    except Exception:
        return set()


def mark_done(h):
    with open(STATE, "a") as f:
        f.write(h + "\n")


def sh(args, timeout=2400):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def beets_import(beets_path):
    b64 = base64.b64encode(OVERRIDE.encode()).decode()
    sh(["ssh", "-o", "ConnectTimeout=10", "unraid",
        f"echo {b64} | base64 -d > /mnt/user/appdata/beets/override.yaml"], timeout=60)
    pb64 = base64.b64encode(beets_path.encode()).decode()
    # import, then flag Various-Artists albums as compilations. beets `asis` import keeps
    # the source tags (which have albumartist=Various Artists) but never sets the comp flag,
    # and Navidrome needs BOTH — without comp it splits a VA album into one-song albums per
    # track artist. `beet modify` only rewrites items whose value actually changes, so this
    # is idempotent + self-limiting (after the first pass it only touches newly-imported VA
    # albums). Single-artist albums never carry albumartist "Various Artists", so no false hits.
    remote = (f'P=$(echo {pb64} | base64 -d); '
              f'docker exec beets beet -c /config/override.yaml import -q "$P" 2>&1; '
              f'echo "--- auto-flag VA compilations (comp=1) so Navidrome keeps them as one album ---"; '
              f'docker exec beets beet modify -y comp=1 albumartist:"Various Artists" 2>&1')
    return sh(["ssh", "-o", "ConnectTimeout=10", "unraid", remote], timeout=2400)


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


done = load_done()
print(f"[music-staging] watching category '{CATEGORY}'; {len(done)} already imported", flush=True)
while True:
    try:
        info = json.load(urllib.request.urlopen(QBT, timeout=15))
    except Exception:
        time.sleep(60); continue
    for t in info:
        if t.get("category") != CATEGORY:
            continue
        h = t.get("hash", "")
        if h in done:
            continue
        if t["progress"] < 1.0:
            print(f"[music-staging] {t['name'][:48]} {round(t['progress']*100)}% {t.get('state')}", flush=True)
            continue
        content = t.get("content_path") or (t.get("save_path", "") + "/" + t["name"])
        beets_path = content.replace(QBT_PREFIX, BEETS_PREFIX, 1)
        print(f"[music-staging] COMPLETE {t['name'][:48]} -> beets import {beets_path}", flush=True)
        rc, out = beets_import(beets_path)
        tail = "\n".join(out.strip().splitlines()[-8:])
        print(f"[music-staging] beets rc={rc}\n{tail}", flush=True)
        r = navidrome_rescan()
        mark_done(h); done.add(h)
        short = t["name"].split("Mp3")[0].strip(" -") or t["name"][:40]
        notify(f"🎵 '{short}' imported & tagged into Navidrome (beets rc={rc}). {r}")
        print("[music-staging] done ->", short, flush=True)
    time.sleep(90)
