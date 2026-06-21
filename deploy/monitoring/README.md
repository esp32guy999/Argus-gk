# Argus monitoring stack — Prometheus + Grafana

Real Prometheus (TSDB + scrape) and Grafana (dashboards) for the Argus metrics that
`argus/metrics.py` emits. This is the "trends & dashboards" layer; the in-process
`argus/observability.py` SelfObserver is the "buzz me when it breaks" layer, and
uptime-kuma is "is it up". All three are complementary.

## Where it runs and why — anvil, not glassgarden

Both services run **on anvil** as **systemd *user* services**, scraping `localhost`.

The obvious home was glassgarden (next to uptime-kuma), but **glassgarden cannot reach
anvil:8210 at all** — anvil is on a different subnet (192.168.6.x) *and* its default
route is the VPN (`tun0`), so inbound replies mis-route (asymmetric routing). Running
the stack on anvil sidesteps cross-host routing entirely and co-locates Prometheus with
the metrics it scrapes. anvil already hosts every other Argus service this way.

| Service | Port | Unit | Notes |
|---|---|---|---|
| Prometheus | `9091` | `prometheus.service` (user) | 9090 is taken by llama-swap. 90d retention. |
| Grafana | `3000` | `grafana.service` (user) | admin pw via `GF_SECURITY_ADMIN_PASSWORD` in `credentials.env` |

Scrape targets (all localhost): `argus :8210/metrics/`, `llama-swap :9090/metrics`,
Prometheus itself.

## Access

Same model as the rest of anvil: reach it over Tailscale.
- Grafana: `http://anvil:3000` (or `http://100.123.249.30:3000`) — admin user `admin`,
  password in `~/.config/secrets/credentials.env` (`GF_SECURITY_ADMIN_PASSWORD`).
- Prometheus: `http://anvil:9091`.

Optional polish: `tailscale serve` a TLS front for `:3000` (set `GF_SERVER_ROOT_URL` if
you put it behind a path).

## Reproduce from scratch

```bash
# Prometheus 3.x
curl -sL -o /tmp/p.tgz https://github.com/prometheus/prometheus/releases/download/v3.12.0/prometheus-3.12.0.linux-amd64.tar.gz
mkdir -p ~/prometheus/data && tar xzf /tmp/p.tgz -C /tmp
cp /tmp/prometheus-3.12.0.linux-amd64/{prometheus,promtool} ~/prometheus/
cp deploy/monitoring/prometheus.yml ~/prometheus/prometheus.yml

# Grafana 13.x
curl -sL -o /tmp/g.tgz https://dl.grafana.com/oss/release/grafana-13.0.2.linux-amd64.tar.gz
tar xzf /tmp/g.tgz -C ~ && mv ~/grafana-* ~/grafana
cp -r deploy/monitoring/grafana/provisioning/* ~/grafana/conf/provisioning/
mkdir -p ~/grafana/dashboards && cp deploy/monitoring/grafana/dashboards/* ~/grafana/dashboards/

# admin password (alphanumeric so EnvironmentFile is safe), stored in canonical secrets
PW=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)
sed -i '/^GF_SECURITY_ADMIN_PASSWORD=/d' ~/.config/secrets/credentials.env
printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$PW" >> ~/.config/secrets/credentials.env

# units
cp deploy/monitoring/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now prometheus.service grafana.service
```

## Verify

```bash
curl -s localhost:9091/api/v1/targets?state=active   # 3 targets, health=up
curl -s localhost:3000/api/health                    # {"database":"ok",...}
# datasource + dashboard are auto-provisioned (folder "Argus", dashboard "Argus — Overview")
```

## Dashboard

`argus-overview.json` (provisioned, folder **Argus**): agent turns/s by outcome, tool
calls/s + p95 latency + errors by tool, Claude Code spend ($) + turns, watchdog
loops/no-progress. Edit in-UI (allowUiUpdates is on) or edit the JSON and restart.
