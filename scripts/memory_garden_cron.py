#!/usr/bin/env python3
"""Scheduled REPORT-ONLY memory-garden run + phone push. Never --apply (scheduled safety):
a timer only ever surfaces proposals; you approve + apply by hand."""
import subprocess, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, ".garden-last.log")
PROP = os.path.expanduser("~/.claude/projects/-home-shane-argus/memory/.garden-proposals.json")
HA = "/home/shane/.local/bin/ha-remind"

out = subprocess.run(["python3", os.path.join(HERE, "memory_garden.py")],
                     capture_output=True, text=True, timeout=900)
open(LOG, "w").write(out.stdout + "\n--- stderr ---\n" + out.stderr)

try:
    d = json.load(open(PROP))
except Exception:
    subprocess.run([HA, "🌱 Memory garden run failed — see scripts/.garden-last.log"], timeout=30)
    raise SystemExit(1)

idx = d.get("index_fix", {})
drift = len(idx.get("add", [])) + len(idx.get("remove_dead", []))
merges, stale, links = d.get("merges", []), d.get("stale", []), d.get("unwritten_links", [])

counts = []
if drift:  counts.append(f"{drift} index fix(es)")
if merges: counts.append(f"{len(merges)} merge candidate(s)")
if stale:  counts.append(f"{len(stale)} stale flag(s)")
if links:  counts.append(f"{len(links)} unwritten link(s)")

lines = ["🌱 Memory garden (weekly)", ", ".join(counts) if counts else "all clean ✅"]
for s in stale[:3]:
    lines.append(f"• stale[{str(s.get('confidence','?'))[:1]}] {s.get('file','')}")
for m in merges[:2]:
    lines.append(f"• merge? {'+'.join(m.get('files', []))}")
if merges or stale or drift:
    lines.append("Tell me to review/apply.")

msg = "\n".join(lines)
subprocess.run([HA, msg], timeout=30)
print(msg)
