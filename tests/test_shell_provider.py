"""Contract test for the shell/CLI provider (argus/tools/shell.py).

Defines the interface AND the safety guarantees the shell lane MUST satisfy.
Fully offline — uses real subprocess with harmless commands. Runnable standalone:
    python tests/test_shell_provider.py     (exit 0 = pass)
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write(manifest: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, manifest.encode()); os.close(fd)
    return path


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import shell

    paths: list[str] = []
    try:
        # --- argv mode (shell:false), default safety posture -------------------
        p = _write("enabled: true\nshell: false\ntimeout: 5\ncwd: '~'\n"); paths.append(p)
        tools = shell.tools(p)

        # 1. manifest -> a single Tool in the uniform contract
        assert len(tools) == 1, f"expected 1 tool, got {len(tools)}"
        t = tools[0]
        assert t.name == "run_command" and t.provider == "shell", (t.name, t.provider)
        assert "shell" in t.tags, t.tags
        assert t.schema is None, "native-schema path (built from the signature)"
        print("PASS: manifest -> Tool contract")

        # 2. builds a pydantic tool via the native (signature) path
        t.as_pydantic_tool()
        print("PASS: as_pydantic_tool() via signature")

        # 3. dispatch success -> exit_code 0 + stdout captured
        out = t.func(command="echo hello-argus")
        assert isinstance(out, dict) and out["exit_code"] == 0, out
        assert "hello-argus" in out["stdout"], out
        print("PASS: dispatch returns exit_code + stdout")

        # 4. non-zero exit is RETURNED, not raised (model reads stderr and adapts)
        out = t.func(command="ls /no/such/path/argus")
        assert out["exit_code"] != 0, out
        assert out["stderr"], "stderr should carry the failure detail"
        print("PASS: non-zero exit returned, not raised")

        # 5. denylist refuses a host-wipe outright (defense in depth)
        for danger in ("rm -rf /", "rm -rf ~", "mkfs.ext4 /dev/sda", "shutdown now"):
            try:
                t.func(command=danger); print(f"FAIL: denylist let {danger!r} through"); return 1
            except ModelRetry as e:
                assert "denylist" in str(e), str(e)
        print("PASS: catastrophic patterns refused (denylist)")

        # 5b. but an ordinary recursive delete of a project path is NOT blocked by the
        #     denylist (it reaches the shell — which then safely no-ops on a fake path)
        out = t.func(command="rm -rf /tmp/argus-nope-xyz/build")
        assert isinstance(out, dict), out  # got to execution, not refused
        print("PASS: ordinary rm -rf <path> not over-blocked")

        # 6. timeout -> teaching ModelRetry
        try:
            t.func(command="sleep 3")  # timeout is 5... use a tighter manifest below
        except ModelRetry:
            pass
        pt = _write("enabled: true\nshell: false\ntimeout: 1\n"); paths.append(pt)
        tt = shell.tools(pt)[0]
        try:
            tt.func(command="sleep 3"); print("FAIL: expected timeout ModelRetry"); return 1
        except ModelRetry as e:
            assert "timeout" in str(e).lower(), str(e)
        print("PASS: over-timeout -> teaching ModelRetry")

        # --- allowlist mode ----------------------------------------------------
        pa = _write("enabled: true\nshell: false\nallowlist: [echo]\n"); paths.append(pa)
        ta = shell.tools(pa)[0]
        out = ta.func(command="echo permitted")
        assert out["exit_code"] == 0, out
        try:
            ta.func(command="cat /etc/hostname")
            print("FAIL: allowlist let a non-listed binary through"); return 1
        except ModelRetry as e:
            assert "allowlist" in str(e), str(e)
        print("PASS: allowlist permits listed / refuses unlisted binaries")

        # --- shell:true mode (pipes work; best-effort allowlist) ---------------
        ps = _write("enabled: true\nshell: true\ntimeout: 5\n"); paths.append(ps)
        ts = shell.tools(ps)[0]
        out = ts.func(command="echo hi | tr a-z A-Z")
        assert out["exit_code"] == 0 and "HI" in out["stdout"], out
        print("PASS: shell:true pipes through correctly")

        # --- output truncation -------------------------------------------------
        pt2 = _write("enabled: true\nshell: true\nmax_output_chars: 50\n"); paths.append(pt2)
        tt2 = shell.tools(pt2)[0]
        out = tt2.func(command="for i in $(seq 1 500); do echo line$i; done")
        assert out["truncated"] is True and len(out["stdout"]) <= 120, (out["truncated"], len(out["stdout"]))
        print("PASS: oversized output truncated + flagged")

        # --- disabled lane -> no tools ----------------------------------------
        pd = _write("enabled: false\n"); paths.append(pd)
        assert shell.tools(pd) == [], "disabled lane must emit no tools"
        print("PASS: enabled:false -> lane emits nothing")

        print("\nALL SHELL CONTRACT TESTS PASSED")
        return 0
    finally:
        for p in paths:
            try: os.unlink(p)
            except OSError: pass


if __name__ == "__main__":
    sys.exit(main())
