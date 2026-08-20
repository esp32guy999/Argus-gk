"""Parity + behaviour test for the model manifest (config/models.yaml) and loader.

Standalone (repo convention — no pytest): `python3 tests/test_model_config.py`.
Asserts the manifest reproduces the values that used to be hardcoded across
ui/server.py + app.js, that unlisted models get safe defaults, and that the
cockpit briefing lists cleared lanes + locks gated ones.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argus import model_config as mc
from argus import loop

ok = 0
def check(label, got, want):
    global ok
    assert got == want, f"FAIL {label}: got {got!r}, want {want!r}"
    ok += 1
    print(f"  ✓ {label}")

print("display:")
check("ornith uncensored display", mc.display("ornith-35b-uncensored"), "Ornith 35B uncensored")
check("80B display", mc.display("qwen3-next-80b"), "Argus (local 80B)")
check("claude-code display", mc.display("claude-code"), "Claude Code")
check("cpu gemma display", mc.display("gemma4-cpu"), "Gemma 4 E2B (CPU)")
check("coder display", mc.display("qwen3-coder-30b"), "Coder 30B")

print("vision:")
check("claude-code vision", mc.is_vision("claude-code"), True)
check("80B not vision", mc.is_vision("qwen3-next-80b"), False)

print("warm_on_select:")
check("80B warms", mc.warm_on_select("qwen3-next-80b"), True)
check("ornith warms", mc.warm_on_select("ornith-35b-uncensored"), True)
check("coder no warm", mc.warm_on_select("qwen3-coder-30b"), False)

print("thinking:")
check("qwen3.6 off", mc.thinking("qwen3.6-35b-a3b"), False)
check("qwen3.8 off", mc.thinking("qwen3.8-27b"), False)
check("ornith off", mc.thinking("ornith-35b-uncensored"), False)
check("80B default(None)", mc.thinking("qwen3-next-80b"), None)

print("context_window:")
check("80B ctx", mc.context_window("qwen3-next-80b"), 8192)
check("ornith ctx", mc.context_window("ornith-35b-uncensored"), 131072)
check("qwen3.8 ctx", mc.context_window("qwen3.8-27b"), 32768)
check("unknown ctx default", mc.context_window("brand-new-7b"), 8192)

print("external:")
check("cpu gemma external", mc.external("gemma4-cpu"),
      {"base_url": "http://localhost:11435/v1", "model_id": "gemma4e2b"})
check("80B not external", mc.external("qwen3-next-80b"), None)
check("cpu gemma in externals()", "gemma4-cpu" in mc.externals(), True)
# Grok is SuperGrok OAuth (grok_code), NOT an external OpenAI-compat backend.
check("grok not external", mc.external("grok"), None)
check("grok not in externals()", "grok" not in mc.externals(), True)
check("grok display", mc.display("grok"), "Grok")
check("grok vision on (prompt-json)", mc.is_vision("grok"), True)
check("cpu gemma api_key default", mc.external_api_key("gemma4-cpu"), "none")

print("accent:")
check("80B violet", mc.accent("qwen3-next-80b"), "violet")
check("gemma26 pink", mc.accent("gemma4-26b"), "pink")
check("qwen3.8 green", mc.accent("qwen3.8-27b"), "green")
check("qwen3.8 display", mc.display("qwen3.8-27b"), "Qwen 3.8 27B")
check("qwen3.8 warms", mc.warm_on_select("qwen3.8-27b"), True)
check("qwen3.8 group Chat", mc.group("qwen3.8-27b"), "Chat")
check("coder group", mc.group("qwen3-coder-30b"), "Coder")
check("coder-next unmapped", mc.accent("qwen3-coder-next"), None)

print("unlisted model → safe defaults:")
check("no display", mc.display("brand-new-7b"), None)
check("no vision", mc.is_vision("brand-new-7b"), False)
check("no warm", mc.warm_on_select("brand-new-7b"), False)
check("no grants", mc.grants("brand-new-7b"), [])

print("gate parity (no manifest grants → unchanged seed):")
gates = loop.effective_gates()
check("shell gated", "shell" in gates, True)
check("coder in shell allow", any("qwen3-coder-30b" in m for m in gates["shell"]), True)
check("fs gated", "fs" in gates, True)

print("cockpit briefing:")
class _T:
    def __init__(self, p): self.provider = p
class _R:
    def __init__(self, provs): self._t = [_T(p) for p in provs]
    def all(self): return self._t
brief = loop._cockpit_briefing(_R(["web", "weather", "native", "shell", "fs", "arr_acquire"]),
                               "brand-new-7b")
check("briefing has header", "Your cockpit" in brief, True)
check("briefing lists Web", "🔍 Web" in brief, True)
check("briefing lists Acquire", "📥 Acquire" in brief, True)
check("unlisted model → shell locked", "Locked" in brief and "🛠 Shell" in brief, True)
# A cleared coder should NOT have shell locked.
brief_coder = loop._cockpit_briefing(_R(["web", "shell"]), "qwen3-coder-30b")
check("coder → shell available not locked",
      "🛠 Shell" in brief_coder and "Locked" not in brief_coder, True)

print(f"\nALL {ok} CHECKS PASSED ✅")
