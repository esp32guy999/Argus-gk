# Argus monitoring stack — Prometheus + Grafana

Prometheus (TSDB + scrape) and Grafana (dashboards) for the metrics `argus/metrics.py` emits.
This is the "trends & dashboards" layer; `argus/observability.py`'s in-process SelfObserver is
"buzz me when it breaks", and uptime-kuma is "is it up". Complementary.

## Where it runs — glassgarden (Docker)

Both run **on glassgarden (Unraid) as Docker containers** on a `monitoring` network, scraping
**anvil over the LAN**. (Originally on anvil scraping localhost — moved to gg 2026-06-21 once the
gg→anvil routing was fixed. gg-based scraping is strictly better: it *sees* anvil-unreachable
events, which localhost scraping was blind to.)

| Service | Host | Port | Container | Notes |
|---|---|---|---|---|
| Prometheus | glassgarden | `9091`→9090 | `prometheus` | 90d retention; scrapes anvil `:8210`+`:9090` |
| Grafana | glassgarden | `3000` | `grafana` | admin pw `GF_SECURITY_ADMIN_PASSWORD` |

Access over Tailscale/LAN: Grafana `http://glassgarden:3000`, Prometheus `http://glassgarden:9091`.

**Prereq (already done, persisted in gg `/boot/config/go`):** gg routes anvil's `.6` subnet via the
real LAN, not the pi-hole macvlan shim — `ip route replace 192.168.6.0/24 dev br0`.

## Files here
- `prometheus.yml` — scrape config (anvil targets). Lives on gg at
  `/mnt/user/appdata/prometheus/prometheus.yml`.
- `grafana/provisioning/*` + `grafana/dashboards/argus-overview.json` — Grafana datasource +
  dashboard, provisioned into the gg container.
- `systemd/*.service` — **legacy** (the old anvil systemd deployment), kept for reference only.

## Reproduce on gg

```bash
ssh unraid
docker network create monitoring 2>/dev/null
mkdir -p /mnt/user/appdata/prometheus/data && chown -R 65534:65534 /mnt/user/appdata/prometheus/data
# copy prometheus.yml -> /mnt/user/appdata/prometheus/prometheus.yml
docker run -d --name prometheus --restart unless-stopped --network monitoring -p 9091:9090 \
  -v /mnt/user/appdata/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro \
  -v /mnt/user/appdata/prometheus/data:/prometheus \
  prom/prometheus:latest --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=90d --web.enable-lifecycle

# copy grafana/provisioning + grafana/dashboards into /mnt/user/appdata/grafana/...
chown -R 472:472 /mnt/user/appdata/grafana
docker run -d --name grafana --restart unless-stopped --network monitoring -p 3000:3000 \
  -v /mnt/user/appdata/grafana:/var/lib/grafana \
  -v /mnt/user/appdata/grafana/provisioning:/etc/grafana/provisioning \
  -e GF_SECURITY_ADMIN_PASSWORD="$GF_SECURITY_ADMIN_PASSWORD" grafana/grafana:latest
```

## Verify
```bash
curl -s localhost:9091/api/v1/targets?state=active   # argus/llama-swap/prometheus all up
curl -s localhost:3000/api/health                    # {"database":"ok",...}
```
Grafana auto-provisions the **Argus → Argus — Overview** dashboard.
