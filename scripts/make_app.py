#!/usr/bin/env python3
"""/make — promote a code snippet into an installed desktop app on anvil.

The human-gated EXPORT step for run_code: proven code leaves the sandbox and becomes a
real, launchable app. Three moves:
  1. ICON — generate one with Lumen (Krea 2 text-to-image, the local image stack) from
     a prompt describing the app; falls back to a clean programmatic icon (PIL) if Lumen
     / the GPU is unavailable, so /make always succeeds.
  2. EXECUTABLE — write the code to ~/.local/share/argus-apps/<slug> with a shebang, +x.
  3. LAUNCHER — a .desktop on ~/Desktop (Icon + Exec), marked executable + gio-trusted so
     GNOME/Zorin shows it as a clickable icon without the "untrusted" prompt.

Usage:
  make_app.py --name "Prime Printer" --desc "prints prime numbers" --lang python \
      --code-file /tmp/primes.py
  echo '<code>' | make_app.py --name "Prime Printer" --desc "prints primes"   # code on stdin
  ... --no-ai-icon   # skip Lumen, use the fast programmatic icon (no GPU/LLM eviction)
"""
from __future__ import annotations
import argparse
import colorsys
import hashlib
import io
import json
import os
import re
import shlex
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
DESKTOP = os.path.join(HOME, "Desktop")
APPS_DIR = os.path.join(HOME, ".local", "share", "argus-apps")
LUMEN = os.environ.get("LUMEN_URL", "http://127.0.0.1:8094")
_SHEBANG = {"python": "#!/usr/bin/env python3", "bash": "#!/usr/bin/env bash"}
_EXT = {"python": ".py", "bash": ".sh"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def gen_icon_lumen(desc: str, out_png: str, timeout: float = 180) -> bool:
    """Generate an icon via Lumen (Krea 2). Returns True on success. Async: submit ->
    poll /api/result -> fetch the PNG -> downscale to a 256px icon."""
    prompt = (f"a minimal flat app icon representing {desc}: a single bold centered "
              "symbol, simple geometric shapes, solid background, crisp, no text, no words")
    body = json.dumps({"prompt": prompt, "width": 512, "height": 512, "steps": 8}).encode()
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            LUMEN + "/api/generate", data=body, headers={"Content-Type": "application/json"}),
            timeout=45)
        pid = json.load(r).get("prompt_id")
    except Exception as e:
        print(f"[make] Lumen generate failed ({e}) — using fallback icon"); return False
    if not pid:
        return False
    deadline, url = time.time() + timeout, None
    while time.time() < deadline:
        try:
            res = json.load(urllib.request.urlopen(f"{LUMEN}/api/result?pid={pid}", timeout=10))
        except Exception:
            time.sleep(3); continue
        if res.get("state") == "done":
            url = res.get("url"); break
        if res.get("state") == "error":
            print("[make] Lumen reported an error — using fallback icon"); return False
        time.sleep(3)
    if not url:
        print("[make] Lumen timed out — using fallback icon"); return False
    try:
        png = urllib.request.urlopen(LUMEN + url, timeout=20).read()
        from PIL import Image
        Image.open(io.BytesIO(png)).convert("RGBA").resize((256, 256), Image.LANCZOS).save(out_png)
        return True
    except Exception as e:
        print(f"[make] fetching/resizing the Lumen image failed ({e}) — fallback"); return False


def gen_icon_pil(name: str, out_png: str) -> bool:
    """Fallback icon: a colored rounded square (hue derived from the name) + initials."""
    from PIL import Image, ImageDraw, ImageFont
    hue = int(hashlib.md5(name.encode()).hexdigest(), 16) % 360
    r, g, b = (int(x * 255) for x in colorsys.hsv_to_rgb(hue / 360, 0.55, 0.85))
    size = 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([14, 14, size - 14, size - 14], radius=52, fill=(r, g, b, 255))
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name)[:2]).upper() or "A"
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 108)
    except Exception:
        font = ImageFont.load_default()
    bb = d.textbbox((0, 0), initials, font=font)
    d.text(((size - (bb[2] - bb[0])) / 2 - bb[0], (size - (bb[3] - bb[1])) / 2 - bb[1]),
           initials, font=font, fill="white")
    im.save(out_png)
    return True


def make_app(name: str, code: str, desc: str = "", language: str = "python",
             ai_icon: bool = True, terminal: bool = True) -> dict:
    if not name.strip():
        raise ValueError("make_app: name is required")
    if not code.strip():
        raise ValueError("make_app: code is empty")
    slug = _slug(name)
    os.makedirs(APPS_DIR, exist_ok=True)
    os.makedirs(DESKTOP, exist_ok=True)

    # 1. executable
    exe = os.path.join(APPS_DIR, slug + _EXT.get(language, ".sh"))
    src = code if code.lstrip().startswith("#!") else _SHEBANG.get(language, _SHEBANG["bash"]) + "\n" + code
    with open(exe, "w") as f:
        f.write(src if src.endswith("\n") else src + "\n")
    os.chmod(exe, 0o755)

    # 2. icon (Lumen if asked + reachable, else programmatic)
    icon = os.path.join(APPS_DIR, slug + ".png")
    used_ai = ai_icon and gen_icon_lumen(desc or name, icon)
    if not used_ai:
        gen_icon_pil(name, icon)

    # 3. .desktop launcher on the Desktop, trusted so GNOME shows it as a real icon
    launcher = os.path.join(DESKTOP, slug + ".desktop")
    entry = ("[Desktop Entry]\n"
             "Type=Application\n"
             f"Name={name}\n"
             f"Comment={desc or name}\n"
             f"Exec={shlex.quote(exe)}\n"
             f"Icon={icon}\n"
             f"Terminal={'true' if terminal else 'false'}\n"
             "Categories=Utility;\n")
    with open(launcher, "w") as f:
        f.write(entry)
    os.chmod(launcher, 0o755)
    os.system(f"gio set {shlex.quote(launcher)} metadata::trusted true >/dev/null 2>&1")

    return {"name": name, "slug": slug, "executable": exe, "icon": icon,
            "icon_source": "lumen" if used_ai else "programmatic", "launcher": launcher}


def main() -> int:
    ap = argparse.ArgumentParser(description="Promote code into a desktop app on anvil.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--desc", default="")
    ap.add_argument("--lang", default="python", choices=["python", "bash"])
    ap.add_argument("--code-file", help="read code from a file (else stdin)")
    ap.add_argument("--no-ai-icon", action="store_true", help="skip Lumen, use the fast icon")
    ap.add_argument("--no-terminal", action="store_true", help="don't launch in a terminal window")
    a = ap.parse_args()
    code = open(a.code_file).read() if a.code_file else sys.stdin.read()
    out = make_app(a.name, code, a.desc, a.lang, ai_icon=not a.no_ai_icon,
                   terminal=not a.no_terminal)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
