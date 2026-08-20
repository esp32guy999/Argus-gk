# Argus — homelab AI assistant / agent harness

This is the Argus project: an orchestrator driving a Pydantic AI loop over local models,
with tool lanes, SQLite storage, and a forked-Forge UI. See `SPEC.md` and `docs/`.

**New here? Start with `docs/CONTRIBUTING.md`** — the dev loop: how to add a tool lane,
how to run the tests (no pytest — standalone scripts), and the conventions/rules.
**Then catch up:** read the latest `docs/session-*.md` and `git log --oneline -25` —
the running log of what changed and the hard-won gotchas.
**SEE** is the only supervisor (`specs/see.md`). SUPER-SEE (SS) is retired as a
product — do not wire `argus/ss/` back into SEE or chat. `docs/ROADMAP-ss-v1.md`
is historical.

## Scope control (stay on task)

Full policy: `docs/POLICY-scope-control.md`. Backlog: `docs/ISSUES.md` (the one register).
Operative rules:

1. **State a Task Anchor** at the start of any non-trivial task — one-line `objective`, a
   *testable* `success_condition`, and the obvious `out_of_scope`. Re-state it when the user
   gives a new objective. Can't write a testable success condition? Clarify scope first.
2. **Discovery ≠ authorization.** Finding a bug/refactor/improvement is not permission to do
   it. Only work that advances the success condition — or clears an in-path obstacle — is in
   scope.
3. **Obstacle vs Issue.** An *obstacle* blocks the objective → resolve it (in scope). An
   *issue* is found-but-not-required → record it in `docs/ISSUES.md`, don't pursue it.
4. **Decide-and-notify vs Stop-and-ask.** Reversible + Low blast radius + obvious answer → do
   it, then say so. Irreversible, Medium+ blast radius, ambiguous, or multiple paths → stop
   and ask. Uncertain reversibility → treat as irreversible.
5. **Circuit breaker.** After **3 consecutive actions — investigating an issue OR clearing an
   obstacle — that neither reduce uncertainty nor advance the `success_condition`**, STOP,
   record as Deferred, return to the anchor. Count by *progress*, not steps or clock (one
   action can eat 20 minutes); the counter resets only when an action actually narrows the
   problem. An obstacle that blows past its budget is bigger than the task → stop and ask.
6. **Default Deferred** when torn Blocking vs Deferred. Record to `docs/ISSUES.md` (tiered:
   one line for Low blast radius, full record for Medium/High).
7. **Close the loop.** At task completion, surface what you deferred so it can be triaged.

## Session-end checkpoint (non-negotiable)

Work is not done until it's in git — the 2026-07-03 audit found 2 weeks of Forge
features living only in a working tree, with pushes silently broken the whole time.

1. **End every working session with `git status` clean.** Commit real work as logical
   units; gitignore ephemera. New scripts/files count as work — untracked ≠ saved.
2. **Push.** An unpushed branch is one disk failure from gone. If push *fails*, that is
   itself a blocking problem — fix or escalate now, don't leave it for next session.
3. **Never create `.bak` files.** Git stash/branches/commits are the backup.

Backstop: the daily `repo-hygiene` watcher (nyx, 09:00) phone-pushes on dirty trees,
untracked files, unpushed commits, broken push paths, or .bak litter.

## Homelab context

(Inlined here because Claude Code won't expand `@import`s outside this dir. The full,
authoritative index is `/home/shane/CLAUDE.md` + `/home/shane/tools.md` — Read them for
anything not covered below.)

### Hosts (Tailscale MagicDNS hostnames)
- **anvil** — GPU workstation (RTX 5080), LAN `192.168.6.220` (ethernet `eno1`; wifi disabled).
  Runs **Argus** (UI `:8210`), **llama-swap** `:9090` (local model server, OpenAI-compat),
  **embeddings** `:8091` (nomic, CPU), the **claude-shim** `:8100`. Its internet egress is
  transparently **behind PIA via gg's Tailscale exit node** (`gg-pia-exit`), fail-closed —
  toggle with `anvil-vpn on|off` (tray icon). So anvil still reaches AudiobookBay. No local VPN
  anymore (PIA was uninstalled; see `docs/network-and-vpn.md`). This is where Argus runs.
- **nyx** — **Home Assistant** (`:8123`, Docker), Forge/legacy UIs, the **peace** app,
  hermes-gateway, the retired Hermes. On the home ISP IP (no VPN). (A gg HA migration was
  attempted 2026-06-21 but PAUSED — HA stays on nyx; see `docs/network-and-vpn.md` + memory.)
- **glassgarden** — Unraid NAS (`192.168.4.206`, user `root`, `ssh unraid`). Hosts media
  services + downloaders, the **PIA VPN exit** (gluetun + `gg-pia-exit` Tailscale exit node
  that anvil routes through), and **Prometheus `:9091` + Grafana `:3000`** monitoring (Docker,
  scrapes anvil; see `deploy/monitoring/`). (Has a stopped HA container from the paused migration.)
- **claude-box** — Ubuntu VM.

### Services on glassgarden
Radarr `:7878`, Sonarr `:8989`, Lidarr `:8686`, Readarr/Bookshelf `:8787`,
Prowlarr `:9696`, qBittorrent `:8090` (user shane), Navidrome `:4533`,
Audiobookshelf `:13378`. (API keys live in `/home/shane/tools.md` /
`~/.claude/projects/-home-shane/memory/` — Read when needed; don't guess.)

### Other
- Home Assistant: `http://nyx:8123` (Docker on nyx; token in credentials.env `HA_TOKEN` — verified working 2026-06-26). Atlanta, GA / Eastern time. Phone pushes: `notify.mobile_app_shanes_iphone` (helper: `~/.local/bin/ha-remind "msg"`).
- 3D printer: Bambu P1S `192.168.4.31`.
- Local chat models via llama-swap on `anvil:9090` (gemma4-26b, Qwen3-80B, …).

Get homelab facts from this context or by Reading the docs above — never invent IPs,
ports, or paths.
