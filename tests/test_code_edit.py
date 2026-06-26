"""Contract test for the code_edit lane (argus/tools/code_edit.py).

Offline; operates inside a throwaway temp root. Validates the happy path
(read/list/write/edit/revert) AND the rails that are the whole point: path
allowlist (no traversal/abs escape), exact-unique edit (0 and >1 matches refused),
backup-before-write, revert restores, refusing to touch the backup dir, and
max_bytes. Run: python tests/test_code_edit.py
"""
from __future__ import annotations
import os, sys, tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from argus.tools import code_edit                            # noqa: E402
from pydantic_ai.exceptions import ModelRetry                # noqa: E402

FAILS = []
def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond: FAILS.append(msg)
def refused(fn, *a, **k):
    try: fn(*a, **k); return False
    except ModelRetry: return True


def main():
    open("/tmp/ce_off.yaml", "w").write("enabled: false\n")
    check(code_edit.tools("/tmp/ce_off.yaml") == [], "disabled config yields 0 tools")

    root = tempfile.mkdtemp(prefix="codeedit_test_")
    try:
        mani = "/tmp/ce_on.yaml"
        yaml.safe_dump({"enabled": True, "roots": [root], "backup_dirname": ".bak",
                        "max_bytes": 1000, "audit_log": os.path.join(root, ".audit.log")},
                       open(mani, "w"))
        T = {t.name: t.func for t in code_edit.tools(mani)}
        check(set(T) == {"read_code", "list_code", "write_code", "edit_code", "revert_code"},
              "lane exposes the 5 tools")

        f = os.path.join(root, "mod.py")

        # create
        r = T["write_code"](f, "a = 1\nb = 2\n")
        check(r["mode"] == "created" and os.path.isfile(f), "write_code creates a file")
        check(T["read_code"](f)["content"] == "a = 1\nb = 2\n", "read_code returns content")
        check(T["read_code"](f)["lines"] == 3, "read_code line count")

        # edit (exact, unique) — backup made, content changed
        e = T["edit_code"](f, "b = 2", "b = 22")
        check(e["replaced"] == 1 and e["backup"] and os.path.isfile(e["backup"]),
              "edit_code replaces once and backs up")
        check("b = 22" in T["read_code"](f)["content"], "edit applied")

        # edit rails: missing / ambiguous
        check(refused(T["edit_code"], f, "NOPE", "x"), "edit refused when old_string absent")
        T["write_code"](f, "x\nx\n")
        check(refused(T["edit_code"], f, "x", "y"), "edit refused when old_string non-unique")

        # revert: overwrite then restore the prior version
        before = T["read_code"](f)["content"]
        T["write_code"](f, "TOTALLY DIFFERENT\n")
        rv = T["revert_code"](f)
        check(T["read_code"](f)["content"] == before, "revert_code restores latest backup")
        check(rv["restored_from"].endswith("mod.py"), "revert reports the backup source")

        # max_bytes
        check(refused(T["write_code"], f, "Z" * 2000), "write refused over max_bytes")

        # allowlist: traversal, absolute outside, ~ escape all refused
        check(refused(T["read_code"], os.path.join(root, "../escape.py")), "traversal refused")
        check(refused(T["write_code"], "/etc/argus_pwn", "x"), "absolute-outside write refused")
        check(refused(T["read_code"], "~/secrets.txt"), "home-escape refused")

        # never operate inside the backup dir
        bak_file = e["backup"]
        check(refused(T["write_code"], bak_file, "x"), "writing inside backup dir refused")

        # list_code
        names = {x["name"] for x in T["list_code"](root)["entries"]}
        check("mod.py" in names, "list_code lists files")

        # --- per-model lane gate (configure-the-harness-per-model edict) -------
        from argus import loop
        from types import SimpleNamespace
        ce = SimpleNamespace(name="edit_code", provider="code_edit")
        web = SimpleNamespace(name="search", provider="web")     # ungated lane
        check("code_edit" in loop.LANE_MODEL_GATES, "code_edit is a gated lane")
        coder = loop._gate_tools([ce, web], "qwen3-coder-30b")
        check({t.provider for t in coder} == {"code_edit", "web"}, "coder model keeps code_edit")
        weak = loop._gate_tools([ce, web], "qwen3-next-80b")
        check({t.provider for t in weak} == {"web"}, "non-coder model is denied code_edit, keeps web")
        sub = loop._gate_tools([ce], "llamaswap/qwen3-coder-next-iq4")
        check(len(sub) == 1, "substring match clears an embedded coder id")
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{'PASS' if not FAILS else 'FAIL'} — {len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
