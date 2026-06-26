"""Code-edit tool lane — let the model READ and safely MUTATE source files, as a
structured alternative to freeform shell. This is the lane the coder bake-off showed
was missing: a model could *propose* a diff but had no tool to *apply* one, so it
confabulated. Now it can apply real, reversible edits — within rails it can't argue past.

Rails (mirrors media_fs + the loop-safety principle that edits must be reversible):
  1. Opt-in by config (`enabled: false` -> lane absent). SHIPPED DISABLED — a code
     editor is powerful, so the owner must name the `roots` and flip it on.
  2. **Path allowlist** — every path is realpath'd and must resolve UNDER a configured
     `root`. Defeats traversal/symlink escape. Outside -> refused.
  3. **Backup-before-write** — every mutation first copies the current file to
     `<root>/<backup_dirname>/<timestamp>/<relpath>`, so `revert_code` can undo it.
     Reversible by construction (no edit is load-bearing-without-a-net).
  4. **Audit log** — every mutating op is appended (ts, op, path, backup).
  5. Exact-string `edit_code` (not fuzzy diff application) — the reliable primitive;
     the bake-off proved model-emitted diffs have bad hunk headers.

Errors raise ModelRetry (teach the model to adjust); successes return dicts.
"""
from __future__ import annotations

import os
import shutil
import time

import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool


def _load(manifest_path: str) -> dict:
    with open(manifest_path) as f:
        return yaml.safe_load(f) or {}


def tools(manifest_path: str = "config/code_edit.yaml") -> list[Tool]:
    cfg = _load(manifest_path)
    if not cfg.get("enabled", False):
        return []

    roots = [os.path.realpath(os.path.expanduser(r)) for r in (cfg.get("roots") or [])]
    if not roots:
        print("[code_edit] enabled but no roots configured — lane disabled.")
        return []
    backup_dirname = str(cfg.get("backup_dirname", ".argus-codebak"))
    max_bytes = int(cfg.get("max_bytes", 2_000_000))     # refuse to write huge blobs
    audit_log = os.path.expanduser(str(cfg.get("audit_log", "~/.local/share/argus/code_edit_audit.log")))

    # ── safety core ──────────────────────────────────────────────────────────
    def _resolve_under(path: str) -> tuple[str, str]:
        if not path or not str(path).strip():
            raise ModelRetry("code_edit: empty path.")
        rp = os.path.realpath(os.path.expanduser(str(path)))
        for root in roots:
            if rp == root or rp.startswith(root + os.sep):
                return rp, root
        raise ModelRetry(
            f"code_edit: refused — {path!r} resolves outside the allowed roots "
            f"({', '.join(roots)})."
        )

    def _not_in_backup(abs_p: str, root: str) -> None:
        bak = os.path.join(root, backup_dirname)
        if abs_p == bak or abs_p.startswith(bak + os.sep):
            raise ModelRetry("code_edit: refused — that path is inside the backup dir.")

    def _audit(op: str, path: str, extra: str = "") -> None:
        try:
            os.makedirs(os.path.dirname(audit_log), exist_ok=True)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(audit_log, "a") as f:
                f.write(f"{ts}\t{op}\t{path}\t{extra}\n")
        except Exception as e:
            print(f"[code_edit] audit write failed: {e}")

    def _backup(abs_p: str, root: str) -> str | None:
        """Copy the current file into the timestamped backup tree. None if the file
        doesn't exist yet (a fresh create — revert means delete)."""
        if not os.path.exists(abs_p):
            return None
        rel = os.path.relpath(abs_p, root)
        dest = os.path.join(root, backup_dirname, time.strftime("%Y%m%d-%H%M%S-%f"), rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(abs_p, dest)
        return dest

    def _latest_backup(abs_p: str, root: str) -> str | None:
        rel = os.path.relpath(abs_p, root)
        bak_root = os.path.join(root, backup_dirname)
        if not os.path.isdir(bak_root):
            return None
        cands = []
        for stamp in os.listdir(bak_root):
            c = os.path.join(bak_root, stamp, rel)
            if os.path.isfile(c):
                cands.append(c)
        return max(cands, key=os.path.getmtime) if cands else None

    # ── read ops ─────────────────────────────────────────────────────────────
    def read_code(path: str) -> dict:
        """Read a source file under the allowed roots. Returns its text + line count.
        Read this before editing so your old_string in edit_code matches exactly."""
        abs_p, _ = _resolve_under(path)
        if not os.path.isfile(abs_p):
            raise ModelRetry(f"code_edit: {path!r} is not a file (or doesn't exist).")
        with open(abs_p, encoding="utf-8", errors="replace") as f:
            text = f.read()
        return {"path": abs_p, "lines": text.count("\n") + 1, "content": text}

    def list_code(path: str) -> dict:
        """List a directory under the allowed roots (read-only) to discover files."""
        abs_p, _ = _resolve_under(path)
        if not os.path.isdir(abs_p):
            raise ModelRetry(f"code_edit: {path!r} is not a directory.")
        entries = []
        with os.scandir(abs_p) as it:
            for e in it:
                entries.append({"name": e.name, "is_dir": e.is_dir()})
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"path": abs_p, "count": len(entries), "entries": entries}

    # ── write ops (all reversible via backup) ─────────────────────────────────
    def write_code(path: str, content: str) -> dict:
        """Create or OVERWRITE a file with `content`. Backs up any existing version
        first (revert_code undoes it). Use edit_code for surgical changes to big files."""
        abs_p, root = _resolve_under(path)
        _not_in_backup(abs_p, root)
        if content is None:
            raise ModelRetry("code_edit: content is required.")
        if len(content.encode("utf-8")) > max_bytes:
            raise ModelRetry(f"code_edit: content exceeds max_bytes ({max_bytes}).")
        existed = os.path.exists(abs_p)
        bak = _backup(abs_p, root)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "w", encoding="utf-8") as f:
            f.write(content)
        _audit("write" if existed else "create", abs_p, bak or "")
        return {"path": abs_p, "mode": "overwrote" if existed else "created",
                "bytes": len(content.encode("utf-8")), "backup": bak, "reversible": existed}

    def edit_code(path: str, old_string: str, new_string: str) -> dict:
        """Replace an EXACT, unique occurrence of old_string with new_string in a file
        (like a precise patch). Fails if old_string is missing or appears more than once
        — include enough surrounding context to make it unique. Backs up first."""
        abs_p, root = _resolve_under(path)
        _not_in_backup(abs_p, root)
        if not os.path.isfile(abs_p):
            raise ModelRetry(f"code_edit: {path!r} does not exist — use write_code to create it.")
        if not old_string:
            raise ModelRetry("code_edit: old_string is required and must be non-empty.")
        with open(abs_p, encoding="utf-8") as f:
            text = f.read()
        n = text.count(old_string)
        if n == 0:
            raise ModelRetry("code_edit: old_string not found. Read the file and copy it exactly.")
        if n > 1:
            raise ModelRetry(f"code_edit: old_string matches {n} times — add context to make it unique.")
        bak = _backup(abs_p, root)
        with open(abs_p, "w", encoding="utf-8") as f:
            f.write(text.replace(old_string, new_string, 1))
        _audit("edit", abs_p, bak or "")
        return {"path": abs_p, "replaced": 1, "backup": bak, "reversible": True}

    def revert_code(path: str) -> dict:
        """Undo the most recent write_code/edit_code on a file by restoring its latest
        backup. (If the file was freshly created with no prior version, there's nothing
        to revert to — delete it with the shell/media lane instead.)"""
        abs_p, root = _resolve_under(path)
        _not_in_backup(abs_p, root)
        bak = _latest_backup(abs_p, root)
        if not bak:
            raise ModelRetry(f"code_edit: no backup found for {path!r} to revert to.")
        shutil.copy2(bak, abs_p)
        _audit("revert", abs_p, bak)
        return {"path": abs_p, "restored_from": bak}

    def _t(name, func, desc, tags, example):
        return Tool(name=name, description=desc, tags=tags, func=func,
                    provider="code_edit", example=example)

    base = ["code", "file", "edit", "source"]
    r0 = roots[0]
    return [
        _t("read_code", read_code, "Read a source file (text + line count). Read before editing.",
           base + ["read", "view"], {"path": os.path.join(r0, "file.py")}),
        _t("list_code", list_code, "List a directory under the code roots (read-only).",
           base + ["list", "ls"], {"path": r0}),
        _t("write_code", write_code, "Create or overwrite a file (backs up the old version first).",
           base + ["write", "create"], {"path": os.path.join(r0, "file.py"), "content": "..."}),
        _t("edit_code", edit_code,
           "Replace an exact, unique string in a file (surgical patch; backs up first).",
           base + ["patch", "replace"],
           {"path": os.path.join(r0, "file.py"), "old_string": "...", "new_string": "..."}),
        _t("revert_code", revert_code, "Undo the last edit/write to a file from its latest backup.",
           base + ["revert", "undo", "restore"], {"path": os.path.join(r0, "file.py")}),
    ]
