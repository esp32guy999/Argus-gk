"""Contract test for the media_fs lane (argus/tools/media_fs.py).

Offline; operates entirely inside a throwaway temp root. Validates the happy path
(list/move/copy/soft-delete/restore/purge) AND the safety rails that are the whole
point of the lane: path-allowlist (no traversal/abs/~ escape), refusing to delete
the trash dir, and purge only inside trash. Run: python tests/test_media_fs.py
"""
from __future__ import annotations
import os, shutil, sys, tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from argus.tools import media_fs                                   # noqa: E402
from pydantic_ai.exceptions import ModelRetry                      # noqa: E402

FAILS = []
def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond: FAILS.append(msg)
def refused(fn, *a):
    try: fn(*a); return False
    except ModelRetry: return True


def main():
    # disabled config -> no tools
    open("/tmp/mf_off.yaml", "w").write("enabled: false\n")
    check(media_fs.tools("/tmp/mf_off.yaml") == [], "disabled config yields 0 tools")

    root = tempfile.mkdtemp(prefix="mediafs_test_")
    try:
        os.makedirs(os.path.join(root, "Artist/Album"))
        open(os.path.join(root, "Artist/Album/01.flac"), "w").write("x" * 100)
        open(os.path.join(root, "loose.mp3"), "w").write("y" * 50)
        mani = "/tmp/mf_on.yaml"
        yaml.safe_dump({"enabled": True, "roots": [root], "trash_dirname": ".argus-trash",
                        "hard_delete": False, "audit_log": os.path.join(root, ".audit.log")},
                       open(mani, "w"))
        T = {t.name: t.func for t in media_fs.tools(mani)}
        check(set(T) == {"list_dir", "stat_path", "make_dir", "move_media", "copy_media",
                         "delete_media", "restore_from_trash", "purge_trash"}, "all 8 tools present")

        # happy path
        check(T["list_dir"](root)["count"] == 2, "list_dir sees 2 entries")
        T["move_media"](os.path.join(root, "loose.mp3"), os.path.join(root, "Artist/Album/02.mp3"))
        check(os.path.exists(os.path.join(root, "Artist/Album/02.mp3")), "move_media renames")
        T["copy_media"](os.path.join(root, "Artist/Album/02.mp3"), os.path.join(root, "Artist/Album/03.mp3"))
        check(os.path.exists(os.path.join(root, "Artist/Album/03.mp3")), "copy_media copies")

        # soft delete -> recoverable
        d = T["delete_media"](os.path.join(root, "Artist/Album/01.flac"))
        check(d["mode"] == "soft" and os.path.exists(d["trashed_to"])
              and not os.path.exists(os.path.join(root, "Artist/Album/01.flac")),
              "delete_media soft-deletes to trash")
        T["restore_from_trash"](d["trashed_to"])
        check(os.path.exists(os.path.join(root, "Artist/Album/01.flac")), "restore_from_trash recovers")

        # purge
        d2 = T["delete_media"](os.path.join(root, "Artist/Album/03.mp3"))
        T["purge_trash"](os.path.join(root, ".argus-trash"))
        check(os.listdir(os.path.join(root, ".argus-trash")) == [], "purge_trash empties trash")

        # SECURITY — path allowlist
        check(refused(T["stat_path"], "/etc/passwd"), "refuses absolute path outside roots")
        check(refused(T["stat_path"], os.path.join(root, "../../etc/hosts")), "refuses ../ traversal")
        check(refused(T["stat_path"], "~/.ssh/id_ed25519"), "refuses ~ outside roots")
        check(refused(T["delete_media"], os.path.join(root, ".argus-trash")), "refuses deleting the trash dir")
        check(refused(T["purge_trash"], os.path.join(root, "Artist")), "refuses purge outside trash")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if FAILS:
        print(f"\n{len(FAILS)} FAILED"); sys.exit(1)
    print("\nALL PASSED"); sys.exit(0)


if __name__ == "__main__":
    main()
