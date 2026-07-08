# Defect Patterns — recurring failure classes to audit every tool against

These aren't one-off bugs; they're *classes* of failure that keep recurring across
tools. When we find one, we (1) fix the instance, (2) record the pattern here, (3) add
the untested tools to that pattern's **audit checklist**, and (4) sweep them.

The unifying theme: **local models can't tell success from failure** — they trust
whatever a tool returns. So a tool that lies (or a harness that lets a lie through)
turns into a confident, hour-long user-facing failure. The defense is making tools
*honest* and the harness *verify*, not making the model smarter.

---

## Pattern 1 — Hollow success
A state-changing call reports success / "searching" / "done" while changing **nothing**.

- **Symptom:** tool returns an optimistic status; ground truth is unchanged. The model
  reports success and never re-checks. User waits forever for a result that can't come.
- **Root cause:** an "already exists" / no-op branch that returns an encouraging status
  without doing the work — or a pass-through of an upstream optimistic response
  (HTTP 200, `searching:true`) with no verification of *effect*.
- **Fix shape:** after a write, verify the effect. Either do the work the branch
  skipped, or return an honest "did nothing / already present — nothing to do." Never
  emit success/searching without a real, confirmed effect.

**Instances:**
- ✅ `arr_acquire` already-in-library (the OMAM/gemma case): fired a search without
  monitoring → nothing downloaded, returned `searching:true`. Fixed: monitor the
  missing items *then* search; honest counts + "nothing missing" early-out.
- ✅ MCP lane writes (`HassTurnOn` &c.): no-op/failure returned as hollow success.
  Fixed: `mcp_lane` verifies writes (parse HA `success/failed/code`, escalate to
  `ModelRetry`, enrich success with what changed).
- ✅ HA REST `turn_on` on an empty target → `200 []`. Surfaced via the MCP verifier.

**Audit checklist — sweep these for hollow success:**
- [ ] `navidrome` create_playlist / dj — confirm the playlist/queue actually changed?
- [ ] `media_fs` copy / move / delete — confirm the file actually moved/removed?
- [ ] `audiobook` grab (`/api/grab`) — confirm the torrent was really added, or trust 200?
- [ ] `lidarr_get_album` — returns `monitored:true` optimistically; confirm monitor+search took.
- [ ] radarr/sonarr/readarr **fresh-add** path — confirm the item was added *and* monitored.
- [ ] HA writes beyond turn on/off (`HassLightSet`, `HassSetVolume`, `HassMedia*`,
      `HassList*Item`) — confirm the `success/failed` parse fires for each shape.
- [ ] `shell` — returns real stdout/exit, but does the caller check the exit code?

---

## Pattern 2 — Asserted-not-checked  *(seed — mitigate next)*
The model states facts (library contents, entity state, file existence) from its prior
instead of reading a tool. E.g. gemma listing albums as "in your library" (they weren't).
- **Mitigation direction:** for stateful questions, prefer a read tool over recall; the
  soul already says "get facts from lookup_memory, never from memory" — extend that
  instinct to library/HA/filesystem state. Possibly a harness nudge on "is/are/do you
  have"-shaped questions with no preceding read call.

## Pattern 3 — Wrong-domain / silent no-match  *(seed)*
An action targets the wrong entity type/domain and matches nothing (porch light vs
`switch.porch_lights`). Fails silently or as hollow success.
- **Mitigation direction:** honest no-match errors (done for the MCP lane); entity-type
  hints in tool descriptions; read-back that reports "0 matched."

---

*Process:* new defect → fix it → add an instance line above → add any untested tools to
the checklist → sweep them in a follow-up. Keep this list short and current; retire a
checklist item only once that tool is actually verified (with a test), not assumed.
