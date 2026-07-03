#!/usr/bin/env python3
"""Memory gardener — a Loop-1-style maintenance pass over the wiki-memory directory.

Inspired by AutoMem (arXiv:2607.01224): the cheap "scaffold review" pass — no GPU, an
LLM Critic reading the store — is where most memory-hygiene gain lives. This reviews the
memory/ dir and PROPOSES (dry-run by default):

  • index drift      — files missing from MEMORY.md, and index lines pointing to no file
  • duplicates       — files covering the same fact -> merge proposals (LLM Critic, unions info)
  • stale flags      — facts likely outdated (past-dated plans, "verify live", "DEPLOYED
                       not merged", superseded) -> flag for human review, never auto-deleted
  • unwritten links  — [[links]] with no target file (forward-refs per the memory rules —
                       informational: write them, or they're noise)

Safety: DRY-RUN by default (prints a report + writes proposals.json). `--apply` first copies
the whole dir to a timestamped backup, then applies ONLY: the safe index sync, plus any
merge/stale proposals you've marked "approved": true in proposals.json. Merges union content
(lose nothing); stale items get a frontmatter marker, never a delete.

Usage:
  memory_garden.py                      # dry-run report over the default argus memory dir
  memory_garden.py --dir <path>         # a different memory dir
  memory_garden.py --apply              # backup + index sync + approved merges/stale
  ARGUS_GARDEN_MODEL=qwen3.6-35b-a3b memory_garden.py   # pick the Critic model
"""
import os, re, sys, json, glob, shutil, argparse, datetime
import httpx

DEFAULT_DIR = os.path.expanduser("~/.claude/projects/-home-shane-argus/memory")
MODEL_URL = os.environ.get("ARGUS_MODEL_URL", "http://localhost:9090/v1")
# Non-reasoning + reliable JSON by default; point it at a frontier shim for higher-quality
# judgement (the AutoMem "originate from frontier, amortize into the store" pattern).
MODEL = os.environ.get("ARGUS_GARDEN_MODEL", "gemma4-26b")
TODAY = datetime.date.today().isoformat()


# ── load / parse ─────────────────────────────────────────────────────────────
def parse_frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if mm and mm.group(2).strip():
            meta[mm.group(1)] = mm.group(2).strip()
    return meta, m.group(2)


def load(memdir):
    mems = []
    for p in sorted(glob.glob(os.path.join(memdir, "*.md"))):
        base = os.path.basename(p)
        if base == "MEMORY.md":
            continue
        text = open(p).read()
        meta, body = parse_frontmatter(text)
        mems.append({
            "path": p, "file": base,
            "name": meta.get("name", base[:-3]),
            "description": meta.get("description", ""),
            "type": meta.get("type", ""),
            "body": body.strip(),
            "links": sorted(set(re.findall(r"\[\[([^\]|]+)", body))),
            "text": text,
        })
    return mems


# ── structural passes (deterministic, no LLM) ────────────────────────────────
def index_state(mems, memdir):
    idx_path = os.path.join(memdir, "MEMORY.md")
    idx = open(idx_path).read() if os.path.exists(idx_path) else ""
    referenced = set(re.findall(r"\(([^)]+\.md)\)", idx))
    files = {m["file"] for m in mems}
    missing = [m["file"] for m in mems if m["file"] not in referenced]
    dead = [r for r in referenced if r not in files]
    return {"path": idx_path, "text": idx, "missing_from_index": missing, "dead_index_lines": dead}


def unwritten_links(mems):
    names = {m["name"] for m in mems}
    out = []
    for m in mems:
        for l in m["links"]:
            if l not in names:
                out.append({"file": m["file"], "link": l})
    return out


# ── LLM Critic ───────────────────────────────────────────────────────────────
def _complete(messages, max_tokens=2400, temperature=0):
    with httpx.Client(timeout=240) as c:
        r = c.post(f"{MODEL_URL}/chat/completions",
                   json={"model": MODEL, "messages": messages,
                         "max_tokens": max_tokens, "temperature": temperature})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _extract_json(s):
    m = re.search(r"(\{.*\}|\[.*\])", s or "", re.S)
    if not m:
        return None
    for cand in (m.group(1), m.group(1).replace("'", '"')):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _listing(mems, body_chars=500):
    return "\n".join(
        f"### {m['file']}\nname: {m['name']}\ndescription: {m['description']}\n"
        f"type: {m['type']}\nbody: {m['body'][:body_chars]}"
        for m in mems)


DEDUP_SYS = (
    "You are a memory-hygiene critic for a personal AI's wiki-memory (a directory of small "
    "markdown fact-files). Given the files, find GROUPS of 2+ that cover the SAME fact/topic and "
    "should be MERGED — near-duplicates, or one clearly supersedes another. Do NOT group files "
    "that are merely related but distinct (e.g. two different deferred ideas). For each group, "
    "propose ONE merged file that UNIONS every durable detail from all members (lose nothing) and "
    "keeps all [[links]]. Respond with ONLY JSON:\n"
    '{"merges":[{"files":["a.md","b.md"],"reason":"...","merged_file":"a.md",'
    '"merged_markdown":"---\\nname: <slug>\\ndescription: <one line>\\nmetadata:\\n  type: <t>\\n---\\n\\n<merged body>"}]}\n'
    "If nothing should merge, return {\"merges\":[]}. Be conservative — only truly redundant files.")

STALE_SYS = (
    f"You are a memory-hygiene critic. Today is {TODAY}. Flag memory files that are LIKELY STALE: "
    "past-dated plans that are probably done, statuses like 'verify live' / 'planned' / 'DEPLOYED "
    "but NOT merged' / 'queued' that may have changed, or facts a newer file supersedes. Do NOT "
    "flag durable facts (user preferences, stable project facts, reference pointers). Respond with "
    'ONLY JSON: {"stale":[{"file":"x.md","reason":"...","confidence":"high|medium|low"}]}. '
    "Empty list if all fresh.")


def llm_dedup(mems):
    try:
        raw = _complete([{"role": "system", "content": DEDUP_SYS},
                         {"role": "user", "content": _listing(mems)}], max_tokens=3000)
        d = _extract_json(raw) or {}
        return d.get("merges", []) if isinstance(d, dict) else []
    except Exception as e:
        print(f"  [dedup critic error: {e}]", file=sys.stderr)
        return []


def llm_stale(mems):
    try:
        raw = _complete([{"role": "system", "content": STALE_SYS},
                         {"role": "user", "content": _listing(mems, 700)}], max_tokens=1500)
        d = _extract_json(raw) or {}
        return d.get("stale", []) if isinstance(d, dict) else []
    except Exception as e:
        print(f"  [stale critic error: {e}]", file=sys.stderr)
        return []


# ── report ───────────────────────────────────────────────────────────────────
def report(memdir):
    mems = load(memdir)
    idx = index_state(mems, memdir)
    links = unwritten_links(mems)
    print(f"🌱 Memory garden — {memdir}  ({len(mems)} files, Critic={MODEL})\n")

    print("── index drift ──")
    print(f"  missing from MEMORY.md : {idx['missing_from_index'] or 'none'}")
    print(f"  dead index lines       : {idx['dead_index_lines'] or 'none'}")

    print("\n── duplicate candidates (Critic) ──")
    merges = llm_dedup(mems)
    if merges:
        for g in merges:
            print(f"  MERGE {g.get('files')}  → {g.get('merged_file')}\n    {g.get('reason','')[:160]}")
    else:
        print("  none")

    print("\n── stale candidates (Critic) ──")
    stale = llm_stale(mems)
    if stale:
        for s in stale:
            print(f"  [{s.get('confidence','?')}] {s.get('file')} — {s.get('reason','')[:150]}")
    else:
        print("  none")

    print("\n── unwritten [[links]] (forward-refs — informational) ──")
    if links:
        for l in links:
            print(f"  {l['file']} → [[{l['link']}]]")
    else:
        print("  none")

    proposals = {
        "dir": memdir, "generated": TODAY, "model": MODEL,
        "index_fix": {"add": idx["missing_from_index"], "remove_dead": idx["dead_index_lines"]},
        "merges": [dict(g, approved=False) for g in merges],
        "stale":  [dict(s, approved=False) for s in stale],
        "unwritten_links": links,
    }
    pf = os.path.join(memdir, ".garden-proposals.json")
    json.dump(proposals, open(pf, "w"), indent=2)
    print(f"\n📝 proposals written → {pf}")
    print("   review it, set \"approved\": true on merges/stale you want, then run with --apply")


# ── apply ────────────────────────────────────────────────────────────────────
def _rewrite_index(memdir, add_files, remove_files):
    mems = {m["file"]: m for m in load(memdir)}
    idx_path = os.path.join(memdir, "MEMORY.md")
    lines = open(idx_path).read().splitlines() if os.path.exists(idx_path) else ["# Memory Index", ""]
    # drop dead lines
    lines = [ln for ln in lines if not any(f"({d})" in ln for d in remove_files)]
    # append missing pointers
    for f in add_files:
        m = mems.get(f)
        if not m:
            continue
        title = m["name"].replace("-", " ").title()
        lines.append(f"- [{title}]({f}) — {m['description'][:80]}")
    open(idx_path, "w").write("\n".join(lines) + "\n")


def apply(memdir):
    pf = os.path.join(memdir, ".garden-proposals.json")
    if not os.path.exists(pf):
        sys.exit("no .garden-proposals.json — run a dry-run first, review, then --apply")
    prop = json.load(open(pf))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{memdir.rstrip('/')}-backup-{stamp}"
    shutil.copytree(memdir, backup)
    print(f"🛟 backup → {backup}")

    # 1) index sync (safe, deterministic)
    fx = prop.get("index_fix", {})
    if fx.get("add") or fx.get("remove_dead"):
        _rewrite_index(memdir, fx.get("add", []), fx.get("remove_dead", []))
        print(f"✓ index sync: +{len(fx.get('add',[]))} pointers, -{len(fx.get('remove_dead',[]))} dead")

    # 2) approved merges (union content -> merged_file, remove the others, relink)
    for g in prop.get("merges", []):
        if not g.get("approved"):
            continue
        files = g["files"]; keep = g.get("merged_file", files[0])
        md = g.get("merged_markdown")
        if not md:
            print(f"  ⚠ merge {files} skipped (no merged_markdown)"); continue
        open(os.path.join(memdir, keep), "w").write(md if md.endswith("\n") else md + "\n")
        for f in files:
            if f != keep:
                p = os.path.join(memdir, f)
                if os.path.exists(p):
                    os.remove(p)
        print(f"✓ merged {files} → {keep}")
    # index sync again to drop pointers for removed files
    mems_now = {m["file"] for m in load(memdir)}
    idx = index_state(load(memdir), memdir)
    if idx["dead_index_lines"]:
        _rewrite_index(memdir, [], idx["dead_index_lines"])

    # 3) approved stale -> add a frontmatter marker (never delete)
    for s in prop.get("stale", []):
        if not s.get("approved"):
            continue
        p = os.path.join(memdir, s["file"])
        if not os.path.exists(p):
            continue
        txt = open(p).read()
        fm = txt.split("---")[1] if txt.count("---") >= 2 else ""
        if "stale:" in fm:
            continue                       # already flagged
        reason = s.get("reason", "")[:80].replace("\n", " ").replace("#", "")
        marker = f"stale: true  # gardener {TODAY}: {reason}\n"
        txt = ("---\n" + marker + txt[4:]) if txt.startswith("---\n") else (marker + txt)
        open(p, "w").write(txt)
        print(f"✓ flagged stale: {s['file']}")
    print("\ndone. review changes; the backup is your rollback.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not os.path.isdir(a.dir):
        sys.exit(f"no such memory dir: {a.dir}")
    (apply if a.apply else report)(a.dir)
