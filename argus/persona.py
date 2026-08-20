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
        if pid == DEFAULT or pid.lower() == "readme" or not _ID_OK.match(pid):
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


KNOBS = ("brevity", "blunt", "dry", "warmth")
RULES = {
    "no_brochure": "- **Be a person, not a brochure.** Plain and direct. No \"I'd be happy to.\"",
    "no_gush": "- **Never gush.** No saccharine, no pep talk, no emoji confetti.",
    "own_uncertainty": "- **Own uncertainty.** If you don't know, say so. Don't fabricate success.",
    "unfiltered": "- **Unfiltered, not unhinged.** Answer the question actually asked without swapping in a safer one. Keep the register technical.",
}
_FORBIDDEN = re.compile(
    r"you cannot run|run_command|no shell|llama-swap|tool grants|gpu seat",
    re.I,
)


class PersonaError(ValueError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (s[:48] or "persona")


def _clamp(v, default: int = 50) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(0, min(100, n))


def _knob_line(knob: str, n: int) -> str:
    n = _clamp(n)
    if knob == "brevity":
        if n >= 75:
            return "- **Brevity is respect.** One or two sentences unless asked for more."
        if n >= 50:
            return "- **Brevity is respect.** Say the thing, then stop."
        if n >= 25:
            return "- Keep answers reasonably short; a short paragraph is fine."
        return "- Take the space you need; complete sentences over telegrams."
    if knob == "blunt":
        if n >= 75:
            return "- **Blunt.** No padding, no softener, no preamble."
        if n >= 50:
            return "- Be direct. Skip the throat-clearing."
        if n >= 25:
            return "- Direct, but a little courtesy is fine."
        return "- Soften the edges; don't steamroll."
    if knob == "dry":
        if n >= 75:
            return "- **Dry and sarcastic** by default. Wit is allowed; cruelty is not."
        if n >= 50:
            return "- A little dry. Sarcasm is seasoning, not the meal."
        if n >= 25:
            return "- Mostly straight; the occasional dry aside is fine."
        return "- Earnest. Don't reach for sarcasm."
    if knob == "warmth":
        if n >= 75:
            return "- Warm and companionable, without becoming a brochure."
        if n >= 50:
            return "- Competent first, personable second."
        if n >= 25:
            return "- Cool and matter-of-fact."
        return "- Cold and technical. No small talk."
    return ""


def render_markdown(spec: dict) -> str:
    """Build a voice file from the form. Template is the product; no LLM."""
    name = str(spec.get("name") or spec.get("display") or "").strip()
    if not name:
        raise PersonaError("name required")
    pid = str(spec.get("id") or slugify(name)).strip()
    if not _ID_OK.match(pid) or pid.lower() in ("argus", "readme"):
        raise PersonaError("invalid id")
    desc = " ".join(str(spec.get("description") or "").split())
    if desc and _FORBIDDEN.search(desc):
        raise PersonaError("description cannot set tools, shell, or GPU")
    knobs = spec.get("knobs") if isinstance(spec.get("knobs"), dict) else {}
    rules = spec.get("rules") if isinstance(spec.get("rules"), list) else list(RULES)
    fm = ["---"]
    for k in KNOBS:
        fm.append(f"{k}: {_clamp(knobs.get(k))}")
    chosen = [r for r in rules if r in RULES]
    fm.append("rules: [" + ", ".join(chosen) + "]")
    fm.append("---")
    lines = fm + ["", f"# {name}", ""]
    lines.append(f"Your name is **{name}**. Asked who you are: you're {name}.")
    lines.append("")
    if desc:
        lines.append(desc)
        lines.append("")
    for k in KNOBS:
        lines.append(_knob_line(k, knobs.get(k)))
    for r in chosen:
        lines.append(RULES[r])
    return "\n".join(lines).rstrip() + "\n"


def save_persona(spec: dict) -> dict:
    """Write personas/<id>.md. Returns {id, display}."""
    name = str(spec.get("name") or spec.get("display") or "").strip()
    pid = str(spec.get("id") or slugify(name)).strip()
    text = render_markdown({**spec, "id": pid, "name": name})
    os.makedirs(_DIR, exist_ok=True)
    path = os.path.join(_DIR, pid + ".md")
    tmp = path + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        _cache.pop(path, None)
    return {"id": pid, "display": name}


def parse_persona(pid: str) -> dict | None:
    """Form-shaped spec from a file, or None."""
    if pid == DEFAULT or pid.lower() == "readme" or not _ID_OK.match(pid or ""):
        return None
    path = os.path.join(_DIR, pid + ".md")
    text = _read(path)
    if not text:
        return None
    knobs = {k: 50 for k in KNOBS}
    rules: list[str] = []
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw = text[3:end]
            body = text[end + 4:].lstrip("-\n")
            for line in raw.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if k in KNOBS:
                    knobs[k] = _clamp(v)
                elif k == "rules":
                    inner = v.strip("[]")
                    rules = [x.strip() for x in inner.split(",") if x.strip() in RULES]
    display = _display_from(pid, body)
    desc = ""
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-") or s.startswith("Your name"):
            continue
        desc = s
        break
    return {
        "id": pid,
        "name": display,
        "display": display,
        "knobs": knobs,
        "rules": rules or list(RULES),
        "description": desc,
        "markdown": text,
    }


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
