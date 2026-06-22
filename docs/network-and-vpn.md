# Homelab network & VPN gateway

Living doc for the 2026-06-21 network fix. **No secrets in here, ever.**

## TL;DR
The "flaky home network" is **not** the switch/cables. Root cause = the **PIA
full-tunnel VPN running on anvil** (its killswitch blocks anvil's own LAN, breaks DNS,
clamps MTU) plus **dual-homed WiFi** left up on anvil & nyx. Fix: move VPN egress **off
anvil onto gg's gluetun** (already a healthy PIA exit), route anvil through it
**fail-closed**, and disable WiFi on the wired servers.

## Evidence (read-only diagnosis, 2026-06-21)
- anvil `eno1`: 0 RX/TX errors, 0 carrier flaps over 7d → physical layer is healthy.
- **Dual-homed:** anvil `eno1` 192.168.6.220 **+** WiFi `wlp9s0` 192.168.6.231, both with
  default routes. nyx the same (`eno1` 192.168.4.29 + flaky Realtek USB WiFi 192.168.4.22).
- anvil **PIA** (`pia-openvpn`, `tun0`): owns default via the `0/1`+`128/1` split; killswitch
  (`fwmark 0x3214`, `suppress_prefixlength 1`, an `unreachable` rule); MTU clamped 1419;
  Tailscale health check reports **DNS broken**.
- Smoking gun: `ip route get 192.168.4.206` is **clean via eno1**, yet the TCP connection is
  **blocked** → it's PIA's fwmark/iptables killswitch, not routing. Tailscale survives only
  through PIA's table-52 carve-out. ⇒ **anvil's own VPN firewalls it off its LAN.**
- gg host routing is clean (`default via br0`). gg runs two PIA containers:
  `binhex-qbittorrentvpn` (qBit, healthy) and **`GluetunVPN` (idle — nothing rides it)**.

## Decisions
- VPN exit lives on **gg**, not a spare Pi: a container network namespace already isolates
  the VPN from the host (that's why gg's host routing stays clean), so a Pi only adds
  hardware for isolation we already get free.
- Use the **idle GluetunVPN** (PIA) as the dedicated anvil exit; leave binhex for qBit.
- Coverage = **whole-machine transparent** (all anvil traffic). **Fail-closed**, with
  `192.168.0.0/16` + Tailscale `100.64.0.0/10` + gg carved out so the box is never
  unreachable — only its internet egress pauses if the tunnel drops.
- Transport anvil→gg = **WireGuard** into gluetun's namespace (anvil WG client → WG server
  in gluetun ns → PIA). The gg→PIA leg stays OpenVPN.

## Crack found (verify-before-build)
gluetun does **not** support PIA over WireGuard — PIA is **OpenVPN-only** in gluetun (its WG
list is Mullvad/Proton/Nord/etc., or `custom`). So WG can't be the gg→PIA protocol; it's only
the anvil↔gg transport. gg→PIA = PIA OpenVPN (healthy, fine). Found via a test switch +
clean rollback — no harm.

## Plan / sequence (nothing destructive without a checkpoint)
0. **Foundation proof:** enable gluetun's HTTP proxy, prove anvil→gg→PIA egress. ⟵ *running*
1. WG server in gluetun's namespace on gg; publish its UDP port; allow via gluetun firewall.
2. Disable PIA on anvil (heals LAN/DNS/MTU) — brief, accepted protection gap.
3. anvil WG client → gg; policy routing: default→WG, exclude LAN + Tailscale + gg;
   **fail-closed**. Verify exit IP = PIA via gg.
4. Uninstall PIA on anvil via its **own uninstaller**; verify the heal.
5. Disable WiFi on anvil + nyx (kill dual-homing / the flaky Realtek).
6. Control CLI `anvil-vpn status|open|close` (scoped NOPASSWD) → later a tray app / Forge
   widget: status display, fail-closed default, click toggles open.
7. Re-home Prometheus/Grafana off anvil once it's a clean LAN citizen.

## Safety / access lifelines
- I run **on anvil** (local). LAN lifeline to gg = `ssh unraid` (192.168.4.206, non-Tailscale).
  nyx LAN = 192.168.4.29.
- Rollbacks: parked gluetun containers; PIA's own uninstaller; the fail-closed carve-outs
  keep LAN + Tailscale up so the box is always reachable.

## HA migration nyx → glassgarden (2026-06-21)
Home Assistant moved off nyx to gg (Docker, host network, `:8123`) as part of consolidating
services onto gg. Export/import: `tar` the `/config` dir nyx→gg, run the HA container on gg.
Upgraded 2026.5.2 → **2026.6.4** by switching the image tag to `:stable` (Watchtower keeps it
current nightly). Clean DB migration, no errors.
- **Re-pointed `nyx:8123` → `glassgarden:8123`** everywhere: Argus `HA_URL` (canonical
  credentials.env + anvil replica), the HA MCP server in `~/.claude.json` + `~/.claude/settings.json`
  (`/mcp_server/sse`), `~/morning-briefing.md`, nyx hermes `cb-homelab/SKILL.md`, argus `CLAUDE.md`,
  memory. Restarted argus-ui + hermes-gateway to reload. (MCP change takes effect next CC session.)
- **Old nyx HA parked** for rollback: stopped, `--restart=no` (won't auto-start on a nyx reboot
  and conflict). Decommission after a soak.
- The 4 gg containers (HA, Prometheus, Grafana, ts-pia-exit) got proper Unraid **templates +
  WebUI labels** (`net.unraid.docker.webui`) so they show icons + WebUI buttons and aren't orphans.
  They still read "3rd party" in CA (cosmetic — only catalog-installed apps avoid it); Watchtower
  updates them regardless of CA status.

## Security note
The one shared password (anvil login/sudo + qBt/ABS/Navidrome/PIA) is a single point of total
compromise and was exposed in chat → **top of the rotation list**. Mitigation: SSH key +
scoped passwordless helper so day-to-day doesn't need it. The value is never written to any
file/doc/commit.

## Transport: Tailscale exit node (chosen over hand-rolled WireGuard)
gg runs **`ts-pia-exit`** (image `tailscale/tailscale`, `--network container:GluetunVPN`,
kernel mode, `--advertise-exit-node`), tailnet IP `100.111.127.31`, approved as exit node.
anvil will just `tailscale set --exit-node=gg-pia-exit --exit-node-allow-lan-access` — **no
anvil-side WG config / routing / killswitch**, and the toggle app becomes a one-liner
(`--exit-node=` cleared vs set).

**Gotcha fixed:** gluetun (iptables-**legacy**, `FORWARD` policy DROP) and Tailscale
(iptables-**nft**) write to different netfilter backends, so gluetun silently dropped the
forwarded traffic. Fix, added to gluetun's **legacy** tables:
`FORWARD ACCEPT tailscale0<->tun0` + `nat POSTROUTING MASQUERADE -s 100.64.0.0/10 -o tun0`.
Made persistent via a watchdog (see below) since they don't survive a gluetun restart.

## Status
- [x] root cause = PIA on anvil; physical layer fine
- [x] gluetun = healthy PIA exit (OpenVPN + HTTP proxy `:8888`)
- [x] `ts-pia-exit` built + approved as exit node
- [x] **PROVEN from nyx**: routed via gg-pia-exit → exit IP PIA `151.240.94.x`, DNS+routing
  both egress PIA, LAN preserved, clean revert
- [x] persistence watchdog: `/boot/config/scripts/ts-pia-exit-watchdog.sh`, cron `*/2`,
  go-file boot-recreate; re-asserts fwd rules + recovers ts-pia-exit on gluetun restart
- [x] **anvil cutover verified GREEN**: PIA daemon stopped, exit-node set; anvil exit IP =
  gg's PIA `151.240.94.x`, DNS ok, LAN+Tailscale ok, network healed (tun0/fwmark gone,
  anvil→gg LAN open). *Reversible state* — PIA stopped but still installed+enabled.
- [x] **PIA fully removed from anvil** (uninstaller stopped+disabled daemon, removed files +
  all 4 `piavpn*` routing tables). anvil default route now `dev tailscale0` (exit node);
  exit IP = gg PIA `151.240.94.x`; dns/lan/ts all ok; no PIA processes or rules remain.
- [x] **WiFi disabled, both hosts** → both single-homed on ethernet.
  - **anvil:** `nmcli radio wifi off` ALONE was NOT enough — it reverted when eno1 briefly
    deactivated and NM auto-failed-over to wifi (radio got re-enabled). Hardened properly:
    `Wired connection 1` → `autoconnect-priority 100`, `autoconnect-retries 0` (infinite);
    `Hogwarts` (wifi) → `connection.autoconnect no` (+ radio off). Now wifi can't take over.
  - **nyx:** networkd `00-wlx-down.network` (ActivationPolicy=always-down) **+** `rtw_8821cu`
    blacklist (`/etc/modprobe.d/disable-usb-wifi.conf`).
  - NB: eno1 was found *deactivated by NM* (carrier was fine the whole time) — root cause of
    that NM deactivation unknown; the autoconnect priority/retries should keep eth sticky.
    Watch for recurrence (could be a DHCP-renewal hiccup from the eero).
- [x] **`anvil-vpn` CLI + tray icon**: `/usr/local/bin/anvil-vpn {status|on|off|toggle|ip}`
  (privileged ops via root `/usr/local/sbin/anvil-vpn-apply on|off` + scoped NOPASSWD
  `/etc/sudoers.d/anvil-vpn`). GNOME tray app `~/.local/bin/anvil-vpn-tray`
  (AyatanaAppIndicator3): 🟢 protected / 🟡 bypassed / 🔴 exit-node down, click-menu toggle,
  10s poll, autostarts (`~/.config/autostart/anvil-vpn-tray.desktop`).
- [x] cleanup: `:8888` proxy disabled (gluetun rebuilt `HTTPPROXY=off`), parked backups removed,
  orphan `my-Gluetun.xml` deleted
- [x] **gg→anvil routing fixed**: gg had `br0` + pi-hole macvlan `shim-br0` both on the `/22`,
  mis-routing `.6.x` out the macvlan. Fix: `ip route replace 192.168.6.0/24 dev br0` (persisted in
  `/boot/config/go`); pi-hole DNS unaffected. (anvil being back on ethernet was the other half.)
- [x] **Prometheus + Grafana re-homed to gg** (Docker, `monitoring` network): `prometheus` `:9091`
  scrapes anvil `192.168.6.220:8210`+`:9090` over LAN; `grafana` `:3000` provisions the datasource
  + Argus dashboard. anvil's systemd copies stopped+disabled. **Bonus:** gg-based Prom now *sees*
  anvil-unreachable events (localhost-scraping on anvil was blind to them). New URLs:
  `http://glassgarden:3000` / `:9091`.
- [ ] disable WiFi on anvil + nyx · control CLI/toggle · re-home Prom/Grafana

## Robustness notes + a gotcha that bit us
- ts-pia-exit shares gluetun's netns. `--network container:GluetunVPN` binds to gluetun's
  **container ID at creation** — so when gluetun is rebuilt (new ID/netns), ts-pia-exit must be
  **RECREATED (`docker rm`+`run`), NOT restarted** (`restart` errors "cannot join network of a
  non running container"). The watchdog now does the recreate; this self-heals a gluetun rebuild
  in ≤2 min. (Learned the hard way 2026-06-21: a `restart` during the proxy-off rebuild killed
  the exit node → anvil lost internet until Bypass was hit.)
- LESSON: don't trust `anvil-vpn status` ("protected") for destructive cleanup — Tailscale can
  lag marking a peer offline. Gate on **actual egress** (`anvil-vpn ip` non-empty) before
  deleting a rollback container.
- Exit node sits behind PIA NAT → anvil↔exit likely DERP-relayed (works, slightly higher latency).

## Cleanup debts (reconcile at the end)
- gluetun now runs from a `docker run` (proxy enabled) that **diverges from the Unraid
  template** — sync the template (`HTTPPROXY=on`) so a UI recreate doesn't revert it.
- Remove parked containers (`GluetunVPN_bak2`) once the build is finalized.
- Delete the orphan `my-Gluetun.xml` template.
