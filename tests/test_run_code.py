"""Contract test for the run_code lane (argus/tools/run_code.py).

Runs real bubblewrap sandboxes (offline — no network by design). Proves the four
guarantees: compute works, the host FS is read-only, the network is unreachable, and a
runaway is killed by the timeout. python tests/test_run_code.py  (skips if bwrap absent).
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic_ai.exceptions import ModelRetry
from argus.tools import run_code


def main() -> int:
    if not shutil.which("bwrap"):
        print("SKIP: bwrap not installed — run_code lane would be disabled"); return 0
    fd, cfg = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, b"enabled: true\ntimeout: 4\nlanguages: [python, bash]\n"); os.close(fd)
    try:
        T = {t.name: t.func for t in run_code.tools(cfg)}
        assert "run_code" in T, "run_code tool present"
        print("PASS: run_code registered")

        r = T["run_code"]("print(6*7)")
        assert r["ok"] and r["stdout"].strip() == "42", r
        print("PASS: compute works (42)")

        canary = "/home/shane/RC_TEST_CANARY"
        r = T["run_code"](f"open({canary!r}, 'w').write('x')")
        assert not r["ok"] and "read-only" in r["stderr"].lower(), r
        assert not os.path.exists(canary), "real-FS canary must NOT exist"
        print("PASS: host FS read-only (write refused, canary absent)")

        r = T["run_code"]("import socket; socket.setdefaulttimeout(3); "
                          "socket.create_connection(('1.1.1.1', 53)); print('NET')")
        assert not r["ok"] and "unreachable" in r["stderr"].lower(), r
        print("PASS: no network (unreachable)")

        try:
            T["run_code"]("while True: pass")
            print("FAIL: runaway should time out"); return 1
        except ModelRetry as e:
            assert "timeout" in str(e).lower(), str(e)
        print("PASS: runaway killed by timeout")

        r = T["run_code"]("echo sandboxed", "bash")
        assert r["ok"] and r["stdout"].strip() == "sandboxed", r
        print("PASS: bash works")

        try:
            T["run_code"]("print(1)", "ruby")
            print("FAIL: disabled language should raise"); return 1
        except ModelRetry:
            pass
        print("PASS: disabled language refused")

        print("\nALL RUN_CODE TESTS PASSED"); return 0
    finally:
        os.unlink(cfg)


if __name__ == "__main__":
    sys.exit(main())
