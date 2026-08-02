# Glassgarden Media Stack — Accumulation → Sorting → Serving

Host: **glassgarden** — Unraid NAS (`192.168.4.206`), everything runs as Docker
containers. Remote access is via **Tailscale** (no port-forwarding). Below is the full
pipeline, grouped by the role each piece plays.

## The end-to-end flow (one line)
**Request → an *arr grabs it → Prowlarr finds a release → qBittorrent/SAB downloads it
(through the VPN) → the *arr renames + hardlinks it into the library → Jellyfin /
Navidrome / Audiobookshelf serve it → reachable anywhere over Tailscale.**

---

## 1. Discovery & requests (what to get)
- **Prowlarr** `:9696` — indexer manager. Configure every torrent + Usenet indexer **once
  here**; it feeds all the *arrs. This is the hub — set it up first.
- **Jellyseerr** `:5055` — request front-end. You/guests request a movie or show; it
  auto-hands it to Radarr/Sonarr. This is the "Netflix-looking" button for users.
- **FreshRSS** `:8085` — RSS reader (feeds / release automation).

## 2. Automation & management — the *arr stack (decide + track)
Each one keeps a "wanted" list, searches via Prowlarr, sends the best release to a
download client, then imports/renames/moves the finished file into the library.
- **Radarr** `:7878` — movies
- **Sonarr** `:8989` — TV
- **Lidarr** `:8686` — music
- **Bookshelf** `:8787` — books/audiobooks (a Readarr-style app w/ Hardcover metadata)

## 3. Download clients (actually fetch it)
- **qBittorrent + VPN** (binhex) `:8090` — torrents, with the VPN built in, **fail-closed**
  (if the VPN drops, torrents stop — no leaks).
- **SABnzbd** `:8080` — Usenet (often faster/cleaner than torrents; needs a Usenet provider).
- **VPN plumbing:** **Gluetun** + a **Tailscale PIA exit node** (`ts-pia-exit`) carry the
  private traffic (PIA). (Anvil's whole internet egress also routes through this exit node.)

## 4. Post-processing / tagging (make it tidy)
- **beets** `:8337` — music library tagger/organizer (cleans up what Lidarr brings in).
- ***arr rename + hardlink** into the library shares — atomic moves + hardlinks so files
  import instantly **and** keep seeding from one copy (no duplication).
- **Watch-folder drop path** (`torrent-grab <type>`): manually drop a magnet/file into a
  per-type folder (movies/tv/music/audiobooks) → auto-routed into the right library.

## 5. Serving — the sharing layer (the part your coworker cares about)
- **Jellyfin** `:8096` — movies + TV. Streams/casts to TVs, phones, browsers. Free apps
  on everything; multi-user with per-user libraries + PINs.
- **Navidrome** `:4533` — music. **Subsonic-compatible**, so it works with lots of apps
  (Tempo, Feishin, Symfonium, DSub, play:Sub). Great for sharing a music library.
- **Audiobookshelf** `:13378` — audiobooks **and** podcasts. Excellent mobile apps,
  per-user progress sync, multi-user.
- **Immich** `:2283` — photos/home video (self-hosted Google Photos; phone auto-backup).

## 6. Access, DNS & dashboard
- **Tailscale** — the front door. Private mesh VPN w/ MagicDNS; share services to a
  coworker's device without exposing anything to the public internet.
- **Pi-hole** + **Unbound** — network ad-blocking DNS + a private recursive resolver.
- **Homarr** `:1975` — dashboard that links/monitors all the above in one page.

## 7. Reliability / ops
- **Prometheus** `:9091` + **Grafana** `:3000` — metrics + dashboards.
- **Uptime-Kuma** `:3001` — per-service uptime monitoring + alerts.
- **Syncthing** `:8384` / **FileRise** — P2P file sync + web file manager.

---

## Storage model (Unraid)
- Unraid **array** (parity-protected disks) + SSD **cache** for in-flight downloads.
- User **shares**, e.g. `/media/{movies,tv,music,audiobooks,books}` and `/downloads`.
- Keep downloads and the library **on the same share/filesystem** so the *arrs can
  **hardlink** (instant import + continue seeding, zero copy).

## Minimal viable subset (if the coworker wants to start small)
**Prowlarr + qBittorrent(+VPN) + one *arr + one server.** e.g. for music:
Prowlarr → Lidarr → qBittorrent → **Navidrome**. For audiobooks, swap in Bookshelf →
Audiobookshelf. Add pieces once the core loop works.

## Order of operations to build it
1. Tailscale (access) → 2. qBittorrent + VPN (verify fail-closed) → 3. Prowlarr + indexers
→ 4. one *arr, pointed at Prowlarr + qBit → 5. the matching server (Jellyfin/Navidrome/ABS)
→ 6. hardlink paths correct → 7. Jellyseerr/Homarr polish → 8. monitoring last.
