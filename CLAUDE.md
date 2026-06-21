# Argus — homelab AI assistant / agent harness

This is the Argus project: an orchestrator driving a Pydantic AI loop over local models,
with tool lanes, SQLite storage, and a forked-Forge UI. See `SPEC.md` and `docs/`.

**Catch up on recent work first:** read the latest `docs/session-*.md` (e.g.
`docs/session-2026-06-20.md`) and `git log --oneline -25` before doing project work —
they're the running log of what changed and the hard-won gotchas.

## Homelab context

(Inlined here because Claude Code won't expand `@import`s outside this dir. The full,
authoritative index is `/home/shane/CLAUDE.md` + `/home/shane/tools.md` — Read them for
anything not covered below.)

### Hosts (Tailscale MagicDNS hostnames)
- **anvil** — GPU workstation (RTX 5080). Runs **Argus** (UI `:8210`), **llama-swap** `:9090`
  (local model server, OpenAI-compat), **embeddings** `:8091` (nomic, CPU), the
  **claude-shim** `:8100`, and has a **VPN (`tun0`)** — the only host that reaches
  AudiobookBay. This is where Argus runs.
- **nyx** — Home Assistant (`:8123`, Docker), Forge/legacy UIs, the **peace** app, the
  retired Hermes. On the home ISP IP (no VPN).
- **glassgarden** — Unraid NAS (`192.168.4.206`, user `root`, `ssh unraid`). Hosts all
  media services + downloaders.
- **claude-box** — Ubuntu VM.

### Services on glassgarden
Radarr `:7878`, Sonarr `:8989`, Lidarr `:8686`, Readarr/Bookshelf `:8787`,
Prowlarr `:9696`, qBittorrent `:8090` (user shane), Navidrome `:4533`,
Audiobookshelf `:13378`. (API keys live in `/home/shane/tools.md` /
`~/.claude/projects/-home-shane/memory/` — Read when needed; don't guess.)

### Other
- Home Assistant: `http://nyx:8123` (token in tools.md). Atlanta, GA / Eastern time.
- 3D printer: Bambu P1S `192.168.4.31`.
- Local chat models via llama-swap on `anvil:9090` (gemma4-26b, Qwen3-80B, …).

Get homelab facts from this context or by Reading the docs above — never invent IPs,
ports, or paths.
