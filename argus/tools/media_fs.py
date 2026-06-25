"""Media filesystem tool lane — safe copy / move / delete / list over the media
library, as a structured alternative to the wide-open shell lane.

Why this exists separately from shell.py: the shell lane can already run `rm`, but
it's freeform and bounded only by a catastrophic denylist. For routine media
management we want *scalpel* tools with rails the model can't talk its way around:

  1. Opt-in by config (`enabled: false` -> lane absent), like every lane.
  2. **Path allowlist** — every path is realpath'd and must resolve UNDER one of the
     configured `roots`. Defeats traversal (`../`) and symlink escapes. Outside -> refused.
  3. **Soft-delete by default** — `delete_media` MOVES the file into
     `<root>/<trash_dirname>/<timestamp>/<relpath>` (recoverable), not `rm`. A hard
     delete only happens if the owner sets `hard_delete: true`.
  4. **Audit log** — every mutating op is appended to `audit_log` (ts, op, src, dst).
  5. Structured ops (move/copy/delete/mkdir/list/stat) — no model-built shell strings.

Errors raise ModelRetry (teaching the model to adjust); successes return dicts.
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


def tools(manifest_path: str = "config/media_fs.yaml") -> list[Tool]:
    cfg = _load(manifest_path)
    if not cfg.get("enabled", False):
        return []

    roots = [os.path.realpath(os.path.expanduser(r)) for r in (cfg.get("roots") or [])]
    if not roots:
        print("[media_fs] enabled but no roots configured — lane disabled.")
        return []
    trash_dirname = str(cfg.get("trash_dirname", ".argus-trash"))
    hard_delete = bool(cfg.get("hard_delete", False))
    audit_log = os.path.expanduser(str(cfg.get("audit_log", "~/.local/share/argus/media_fs_audit.log")))

    # ── safety core ──────────────────────────────────────────────────────────
    def _resolve_under(path: str) -> tuple[str, str]:
        """Realpath `path` and require it under an allowed root. Returns (abs, root).
        Works for not-yet-existing paths (move/copy dst, mkdir) — realpath resolves
        the existing prefix, and the prefix check still holds."""
        if not path or not str(path).strip():
            raise ModelRetry("media_fs: empty path.")
        rp = os.path.realpath(os.path.expanduser(str(path)))
        for root in roots:
            if rp == root or rp.startswith(root + os.sep):
                return rp, root
        raise ModelRetry(
            f"media_fs: refused — {path!r} resolves outside the allowed media roots "
            f"({', '.join(roots)}). Only paths under those are permitted."
        )

    def _audit(op: str, src: str, dst: str = "") -> None:
        try:
            os.makedirs(os.path.dirname(audit_log), exist_ok=True)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(audit_log, "a") as f:
                f.write(f"{ts}\t{op}\t{src}\t{dst}\n")
        except Exception as e:                      # auditing must never break the op
            print(f"[media_fs] audit write failed: {e}")

    # ── read ops ─────────────────────────────────────────────────────────────
    def list_dir(path: str) -> dict:
        """List a directory under the media roots. Returns entries with name, size
        (bytes), and is_dir. Read-only — use this to discover paths before acting."""
        abs_p, _ = _resolve_under(path)
        if not os.path.isdir(abs_p):
            raise ModelRetry(f"media_fs: {path!r} is not a directory (or doesn't exist).")
        entries = []
        with os.scandir(abs_p) as it:
            for e in it:
                try:
                    entries.append({"name": e.name, "is_dir": e.is_dir(),
                                    "size": e.stat().st_size if e.is_file() else None})
                except OSError:
                    entries.append({"name": e.name, "is_dir": None, "size": None})
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"path": abs_p, "count": len(entries), "entries": entries}

    def stat_path(path: str) -> dict:
        """Stat a file/dir under the media roots: exists, is_dir, size, mtime."""
        abs_p, _ = _resolve_under(path)
        if not os.path.exists(abs_p):
            return {"path": abs_p, "exists": False}
        st = os.stat(abs_p)
        return {"path": abs_p, "exists": True, "is_dir": os.path.isdir(abs_p),
                "size": st.st_size, "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))}

    # ── write ops ────────────────────────────────────────────────────────────
    def make_dir(path: str) -> dict:
        """Create a directory (and parents) under the media roots."""
        abs_p, _ = _resolve_under(path)
        os.makedirs(abs_p, exist_ok=True)
        _audit("mkdir", abs_p)
        return {"created": abs_p}

    def move_media(src: str, dst: str) -> dict:
        """Move/rename a file or folder. BOTH src and dst must be under the media roots."""
        abs_s, _ = _resolve_under(src)
        abs_d, _ = _resolve_under(dst)
        if not os.path.exists(abs_s):
            raise ModelRetry(f"media_fs: source {src!r} does not exist.")
        os.makedirs(os.path.dirname(abs_d), exist_ok=True)
        shutil.move(abs_s, abs_d)
        _audit("move", abs_s, abs_d)
        return {"moved": abs_s, "to": abs_d}

    def copy_media(src: str, dst: str) -> dict:
        """Copy a file or folder. BOTH src and dst must be under the media roots."""
        abs_s, _ = _resolve_under(src)
        abs_d, _ = _resolve_under(dst)
        if not os.path.exists(abs_s):
            raise ModelRetry(f"media_fs: source {src!r} does not exist.")
        os.makedirs(os.path.dirname(abs_d), exist_ok=True)
        if os.path.isdir(abs_s):
            shutil.copytree(abs_s, abs_d, dirs_exist_ok=True)
        else:
            shutil.copy2(abs_s, abs_d)
        _audit("copy", abs_s, abs_d)
        return {"copied": abs_s, "to": abs_d}

    def delete_media(path: str) -> dict:
        """Delete a file or folder under the media roots. By default this is a SOFT
        delete: the item is moved into <root>/<trash>/<timestamp>/ and is recoverable.
        (Hard, permanent deletion only if the owner enabled hard_delete in config.)"""
        abs_p, root = _resolve_under(path)
        if not os.path.exists(abs_p):
            raise ModelRetry(f"media_fs: {path!r} does not exist.")
        # never let the model delete the trash dir itself
        trash_root = os.path.join(root, trash_dirname)
        if abs_p == trash_root or abs_p.startswith(trash_root + os.sep):
            raise ModelRetry("media_fs: refused — that path is inside the trash dir.")
        if hard_delete:
            if os.path.isdir(abs_p):
                shutil.rmtree(abs_p)
            else:
                os.remove(abs_p)
            _audit("hard_delete", abs_p)
            return {"deleted": abs_p, "mode": "hard", "recoverable": False}
        # soft delete -> trash, preserving relpath-from-root
        rel = os.path.relpath(abs_p, root)
        dest = os.path.join(trash_root, time.strftime("%Y%m%d-%H%M%S"), rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(abs_p, dest)
        _audit("soft_delete", abs_p, dest)
        return {"deleted": abs_p, "mode": "soft", "trashed_to": dest, "recoverable": True}

    def restore_from_trash(path: str) -> dict:
        """Recover a soft-deleted item from the trash back to its original location.
        `path` is the trashed path (inside <root>/<trash>/<timestamp>/...)."""
        abs_p, root = _resolve_under(path)
        trash_root = os.path.join(root, trash_dirname)
        if not abs_p.startswith(trash_root + os.sep):
            raise ModelRetry("media_fs: restore source must be inside the trash dir.")
        # layout is <trash_root>/<timestamp>/<original-relpath> -> strip the first two
        rel = os.path.relpath(abs_p, trash_root).split(os.sep, 1)
        if len(rel) < 2:
            raise ModelRetry("media_fs: can't determine the original location for that item.")
        orig = os.path.join(root, rel[1])
        if os.path.exists(orig):
            raise ModelRetry(f"media_fs: {orig} already exists; refusing to overwrite on restore.")
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        shutil.move(abs_p, orig)
        _audit("restore", abs_p, orig)
        return {"restored": orig, "from": abs_p}

    def purge_trash(path: str) -> dict:
        """Permanently delete trash contents — IRREVERSIBLE. `path` must be inside an
        .argus-trash dir; pass the trash root itself to empty it. Use list_dir to find it."""
        abs_p, root = _resolve_under(path)
        trash_root = os.path.join(root, trash_dirname)
        if not (abs_p == trash_root or abs_p.startswith(trash_root + os.sep)):
            raise ModelRetry("media_fs: purge only operates inside the .argus-trash dir.")
        if not os.path.exists(abs_p):
            raise ModelRetry(f"media_fs: {path!r} does not exist.")
        if abs_p == trash_root:                         # empty the trash, keep the dir
            for name in os.listdir(abs_p):
                sub = os.path.join(abs_p, name)
                shutil.rmtree(sub) if os.path.isdir(sub) else os.remove(sub)
            _audit("purge_all", abs_p)
            return {"emptied": abs_p}
        shutil.rmtree(abs_p) if os.path.isdir(abs_p) else os.remove(abs_p)
        _audit("purge", abs_p)
        return {"purged": abs_p}

    def _t(name, func, desc, tags, example):
        return Tool(name=name, description=desc, tags=tags, func=func,
                    provider="media_fs", example=example)

    base_tags = ["media", "file", "filesystem", "library"]
    return [
        _t("list_dir", list_dir, "List a directory in the media library (read-only).",
           base_tags + ["list", "ls", "browse"], {"path": roots[0]}),
        _t("stat_path", stat_path, "Stat a media file/dir (exists, size, mtime).",
           base_tags + ["stat", "info"], {"path": roots[0]}),
        _t("make_dir", make_dir, "Create a directory under the media library.",
           base_tags + ["mkdir", "create"], {"path": os.path.join(roots[0], "New Folder")}),
        _t("move_media", move_media, "Move/rename a media file or folder (both ends inside the library).",
           base_tags + ["move", "rename", "mv", "organize"], {"src": "<path>", "dst": "<path>"}),
        _t("copy_media", copy_media, "Copy a media file or folder (both ends inside the library).",
           base_tags + ["copy", "cp"], {"src": "<path>", "dst": "<path>"}),
        _t("delete_media", delete_media,
           "Delete a media file/folder. SOFT by default (moved to a recoverable trash dir).",
           base_tags + ["delete", "remove", "rm", "trash"], {"path": "<path>"}),
        _t("restore_from_trash", restore_from_trash,
           "Recover a soft-deleted item from the trash back to its original location.",
           base_tags + ["restore", "recover", "undelete", "trash"], {"path": "<trash path>"}),
        _t("purge_trash", purge_trash,
           "Permanently empty the trash (irreversible). Pass the .argus-trash dir to empty it.",
           base_tags + ["purge", "empty", "trash"], {"path": "<root>/.argus-trash"}),
    ]
