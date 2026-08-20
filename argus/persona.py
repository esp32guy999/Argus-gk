"""Selectable voices, independent of the serving model.

`soul.md` is the default Argus voice. Extra files in `personas/<id>.md` are
other voices you can lay on any model. Model-specific capability notes stay
in `soul.d/` and still attach by model id — they are not personas.
"""
from __future__ import annotations

import os
import re
import threading

_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
_SOUL = os.path.join(_ROOT, "soul.md")
_DIR = os.path.join(_ROOT, "personas")
DEFAULT = "argus"

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}

_ID_OK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _read(path: str) -> str:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    with _lock:
        hit = _cache.get(path)
        if hit is None or hit[0] != mtime:
            try:
                text = open(path, encoding="utf-8").read().strip()
            except OSError:
                text = ""
            _cache[path] = (mtime, text)
        return _cache[path][1]


def _display_from(pid: str, text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip() or pid
        if s:
            break
    return pid.replace("-", " ").replace("_", " ").title()


def normalize(persona_id: str | None) -> str:
    pid = (persona_id or "").strip() or DEFAULT
    if pid == DEFAULT:
        return DEFAULT
    if not _ID_OK.match(pid):
        return DEFAULT
    if os.path.isfile(os.path.join(_DIR, pid + ".md")):
        return pid
    return DEFAULT


def list_personas() -> list[dict]:
    """Default Argus first, then every personas/*.md (except a duplicate argus file)."""
    out = [{"id": DEFAULT, "display": "Argus"}]
    try:
        names = sorted(os.listdir(_DIR))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".md"):
            continue
        pid = fn[:-3]
        if pid == DEFAULT or not _ID_OK.match(pid):
            continue
        text = _read(os.path.join(_DIR, fn))
        out.append({"id": pid, "display": _display_from(pid, text)})
    return out


def voice(persona_id: str | None) -> str:
    """Markdown for the # Your voice section. Unknown ids fall back to soul.md."""
    pid = normalize(persona_id)
    if pid != DEFAULT:
        text = _read(os.path.join(_DIR, pid + ".md"))
        if text:
            return text
    return _read(_SOUL)


def wrap_user_text(persona_id: str | None, text: str) -> str:
    """Prefix a user message for backends that have no system-prompt seam (Grok, CC).
    Default Argus voice is skipped — those agents already have their own register."""
    pid = normalize(persona_id)
    if pid == DEFAULT:
        return text
    v = voice(pid)
    if not v:
        return text
    return f"[Voice for this turn — follow it.]\n{v}\n\n---\n{text}"
