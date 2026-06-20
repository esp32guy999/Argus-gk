"""Open the Claude Desktop Next shell in a Chromium --app window.

The server runs under claude-desktop-next.service (port 8100 on nyx). This
script is just the window-opener and can run from nyx or anvil. Pass --url
to point at a different host (default: this machine's localhost, or nyx if
run from anywhere else via the installer).
"""

import argparse
import shutil
import subprocess
import sys

DEFAULT_URL = "http://127.0.0.1:8100"
USER_DATA_DIR = f"{__import__('os').path.expanduser('~')}/.config/claude-desktop-next-chrome"


def open_window(url: str) -> int:
    args_tail = [f"--app={url}", f"--user-data-dir={USER_DATA_DIR}"]
    if shutil.which("flatpak"):
        try:
            subprocess.Popen(["flatpak", "run", "org.chromium.Chromium", *args_tail])
            return 0
        except Exception:
            pass
    for exe in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"):
        if shutil.which(exe):
            subprocess.Popen([exe, *args_tail])
            return 0
    print(f"[warn] no Chromium binary found; open {url} manually", file=sys.stderr)
    return 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL, help="App URL (default: %(default)s)")
    args = p.parse_args()
    sys.exit(open_window(args.url))
