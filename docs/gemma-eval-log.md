# Gemma tool-use evaluation — running log

Live record of what we learn giving gemma4-26b real tasks through the Argus loop
(`scripts/gemma_probe.py` captures the full tool trace; ground truth verified
separately). Three sections: **Findings** (problems observed), **Fixes** (what we
changed), **Ideas** (improvements worth doing later). Keep it current — append per round.

The working thesis so far: **gemma's honesty tracks tool-feedback quality.** When tools
tell the truth (or aren't offered), gemma is truthful; it fabricates only when a tool
lies to it (the hollow-success class). So the leverage is honest tools, not a smarter model.

---

## Findings

### F1 — `run_command` (shell) is never surfaced for system queries  *(batch 1)*
"GPU temperature" and "free disk space" — textbook shell tasks — did not offer
`run_command`, so gemma honestly declined. The shell lane is effectively unreachable
for its intended system-status use; semantic select isn't ranking it for hardware/disk
queries. → see Fix1 / Idea1.

### F2 — HA entity-name thrash  *(batch 1)*
"Are the porch lights on?" took **6 `GetLiveContext` calls**: memory stores the
entity_id `switch.porch_lights`, but `GetLiveContext` matches the *friendly* name
"Porch lights". gemma flailed through `porch` / `porch_lights` before landing on
"porch lights". Recovered honestly, but wasteful/fragile. → see Idea2.

### F3 — coherence wobble  *(batch 1, minor)*
The "is Nefarious in my library?" answer said *"No, it's not. Wait, yes it is."* —
landed correct but visibly flip-flopped mid-sentence.

### F4 — `lidarr_get_album` hollow success on a fresh artist  *(round 2, REAL BUG)*
"Download the Fleetwood Mac album Rumours." → the tool returned
`{monitored:True, searching:True, have_tracks:0, total_tracks:0}` and gemma reported
"added + searching, will import". **Ground truth ~2 min later: artist AND album both
`monitored:False`** — so it will NOT download. The `0/0 tracks` is the tell: the album
metadata populates ASYNC after a fresh artist add, and the refresh supersedes the album
id the tool monitored (and/or the artist-monitor doesn't stick) → the monitor is lost.
Same hollow-success class as OMAM (F… the arr already-in-library fix), but in the
granular album path. gemma was honest relative to the tool — **the tool lied.** Needs a
proper fix (re-fetch after metadata settles, verify the monitor stuck, or defer the
"searching" claim until tracks > 0). NOTE: my earlier lidarr_get_album artist-monitor
"hardening" did NOT prevent this — investigate whether it fired.

### F2b — HA entity-name thrash recurred  *(round 2)*
"Turn on the porch lights and confirm" took **5 calls** (turned on, then thrashed
light-vs-switch domain to confirm, turned on again). Same root as F2. The MCP
write-verifier DID fire (`[verified changed: Porch lights]` in the trace) — good.

---

## Fixes

### Fix1 — enriched `run_command` description with system-status terms  *(partial)*
`shell.py` default description now names GPU temperature/nvidia-smi, disk/df, memory/free,
services/systemctl, network. **Result: GPU-temp queries now surface `run_command`** and
gemma used it perfectly (`nvidia-smi --query-gpu=temperature.gpu`, read `ok`/exit_code,
answered "59°C" — verified accurate). **But "free disk space" STILL doesn't surface it**
— the media_fs tools (`stat_path`, `list_dir`) outrank it. → **Lesson: description
enrichment is an unreliable lever for tool selection.** Dedicated tools (Idea1) are the
robust answer; promoting Idea1 to the next fix to build.

### Fix2 — dedicated `get_gpu` / `get_disk` native tools  *(F1 fully resolved)*
`native.py`: `get_gpu` (nvidia-smi → temp/util/VRAM) and `get_disk` (shutil.disk_usage
→ free/used GB). Deterministic, honest (raise on failure), available to ALL models (no
shell grant). **Verified live: both now surface for GPU/disk queries and gemma used them
correctly** — "40°C" and "355.3 GB free" (the disk query that failed twice under Fix1).
This is the robust answer Idea1 predicted; description-enrichment (Fix1) was the weak one.

### Fix3 — `lidarr_get_album` confirms the monitor stuck  *(F4 resolved)*
After monitoring the album, re-fetch and require `monitored==True` AND `trackCount>0`
before claiming `searching:True`. On a fresh artist with unready metadata (0 tracks) it
now returns `searching:False` + an honest "still populating, ask again in ~30s" instead
of hollow success. Unit-tested (test_arr_acquire: 0-track album defers, no AlbumSearch).

---

## Ideas (backlog)

- **Idea1 — dedicated `get_gpu` / `get_disk` status tools.** Safer + more reliable than
  surfacing the raw shell for system queries: deterministic parse of `nvidia-smi` / `df`,
  available to ALL models (no shell grant needed), and they'd rank naturally for
  "GPU temp" / "disk space". The right long-term answer to F1.
- **Idea2 — HA entity resolution.** Either teach lookups to match `entity_id` as well as
  friendly name, or store friendly names alongside ids in memory, so the model doesn't
  thrash. (GetLiveContext is an MCP tool we don't own — so the fix is memory-side or a
  small resolver helper.)
- **Idea3 — surface the shell lane deliberately, not broadly.** If we enrich
  `run_command` for system queries (Fix1), watch that it doesn't get offered for
  everything — it's the high-blast lane. Measure offered-rate on the eval suite.

---

## Round history
- **Batch 1 (2026-07-08):** 7 tasks, **7/7 honest** — correct when the right tool was
  offered (weather, port, movie, playlist, porch), honest decline when not (GPU, disk).
  Surfaced F1, F2, F3. Confirmed the navidrome verified-count fix works live (4/4).
- **Round 2 (2026-07-08, escalation):** 3 tasks. GPU-safe-range conditional ✅ (52°C,
  "well under 80"). Porch on+confirm ✅ (verified on; 5 calls — F2b thrash; MCP
  write-verifier fired). "Download Rumours" — gemma picked the right tool + reported
  honestly, but the TOOL hollow-successed → **F4** (a real bug). Net: gemma 3/3 honest;
  the one failure was a lying tool, not the model. Applied Fix1 (partial). All probe
  writes cleaned up (playlist deleted, porch off, Fleetwood Mac unmonitored).
- **Round 3 (2026-07-08, fixes):** built Fix2 (`get_gpu`/`get_disk`) → GPU + disk both
  work now (gemma: "40°C", "355.3 GB free"). Built Fix3 (F4 confirm) → `lidarr_get_album`
  defers honestly on unready metadata. F1 + F4 both fully resolved; suite green
  (test_arr_acquire incl. F4 regression). Open: F2/F2b (HA entity thrash — Idea2).
