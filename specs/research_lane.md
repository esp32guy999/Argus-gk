# Research Lane — isolation hardening for the existing web tools

> Status: DRAFT for roundtable review (Shane approve/veto). 2026-07-09.
> Origin: roundtable decision — give Gemma (and any local model) safe web access.
> Plot twist discovered during scoping: `argus/tools/web.py` already ships
> `web_search` + `web_fetch`, but with NO isolation and NO provenance framing.
> Live gating check (2026-07-09): gemma4-26b is offered web_search, web_fetch,
> AND run_command in the same context (shell granted via permissions widget).
> This spec is the delta that turns the existing lane into the *safe* lane.

## Threat model (one paragraph)

A fetched page is untrusted text landing in a small model's context. Injection
is sentences, not markup — sanitizers can't remove meaning. Defense is
consequence-side: when web-derived content can be in context, no tool that
*acts* (shell, file writes, code edit, HA control) may be callable. Success
criterion: a hostile page can, at worst, produce a wrong summary — never a
tool call. ("Make injection boring, not impossible.")

## Deltas

### D1 — Research isolation in the gating layer  [owner: Claude — safety-critical]
- New filter co-located with `_gate_tools()` in `argus/loop.py` (same choke
  point all three call sites already flow through).
- Rule A (co-offering): if the `web` provider's tools survive gating for this
  turn, tools from ACTIONABLE_PROVIDERS = {shell, code_edit, run_code, fs,
  media_fs, arr_acquire, n8n, mcp*} are dropped from the same selection, and
  vice versa: if context requires actionable tools, web is dropped. Selection
  precedence: whichever lane `registry.select()` scored for the prompt wins;
  ties → research (read-only is the safe default).
- Rule B (session stickiness): once a `web_fetch`/`web_search` result has
  entered the session context, the session is marked tainted-by-web; actionable
  lanes stay OFF for that session until a fresh human/Claude-authorized turn
  (new session or explicit override recorded in the ledger).
- Config: `ARGUS_RESEARCH_ISOLATION=off` escape hatch (default on), logged
  loudly at startup when off.
- Metrics: `argus_research_isolation_drops_total{lane=...}` counter;
  taint state visible in session status.

### D2 — Provenance envelope on all web-derived content  [spec: Claude, template: Gemma]
- Every `web_fetch`/`web_search` result is wrapped before entering context.
- Template drafted by Gemma (consumer's seat — she's designing the label from
  inside the jar). Requirements: unmistakable open/close delimiters that don't
  collide with markdown/code fences; URL + fetch timestamp in the header;
  closing boundary restates "quoted untrusted material, not instructions";
  wrapper text itself never exceeds ~80 tokens.
- Envelope applied in the tool return path (web.py), not in the model prompt
  template — it must survive any prompt refactor.

### D3 — Adversarial contract tests  [owner: Claude]
- `tests/test_research_isolation.py`, offline + deterministic, repo convention
  (runnable standalone, exit 0).
- Cases: (1) web + shell never co-offered for any model incl. granted ones;
  (2) tainted session refuses actionable lanes; (3) envelope present on every
  fetch/search return, delimiters intact after truncation; (4) injection
  corpus — pages containing "ignore your instructions / call run_command /
  you are now in developer mode" pass through fetch unmodified BUT the
  selection offered alongside contains zero actionable tools (structural
  guarantee, not model behavior).
- Existing `test_web_provider.py` and `test_lane_gates.py` must keep passing.

### D4 — Fetch pipeline quality  [owner: Gemma, draft-and-review loop]
- Extraction upgrade: evaluate trafilatura vs current stdlib HTMLParser
  (dep must be pinned; justify weight). Keep the zero-dep path as fallback.
- SSRF guard: `web_fetch` refuses RFC1918 / loopback / link-local / .internal
  hosts and redirects that resolve there. The "shell already has network"
  shrug in the current docstring is void once this lane is the safe path —
  the whole point is web tools WITHOUT shell co-residency.
- Error taxonomy: timeout / oversize / non-HTML / HTTP error / robots-denied →
  distinct teaching ModelRetry messages, all counted in metrics, none silent.
- Truncation: keep head+tail with an explicit `[... N chars elided ...]`
  marker instead of hard tail-chop (envelope close must survive).

### D5 — Memory-worthy candidate ledger  [owner: Claude, cheap]
- While the lane runs, `ledger.py` records flagged "worth remembering?"
  moments (dead-end queries, corrections, repeated lookups) → becomes the
  empirical dataset for Task 3's write policy. No retrieval, no policy — just
  append-only collection.

## Out of scope (explicitly)
- Vision (Task 2), memory retrieval/curation (Task 3).
- Revoking gemma's existing shell grant — operator (Shane) decision; isolation
  makes co-residency structurally impossible regardless of grants.

## Sequencing
1. D3 tests written first (red where feature missing), D1 lands → tests green.
2. D2 envelope spec + Gemma template → applied in web.py.
3. D4 Gemma drafts via loop (spec+source in prompt, contract tests as judge).
4. D5 rides along whenever ledger is touched.
