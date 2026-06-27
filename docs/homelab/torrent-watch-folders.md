# Policy: grab torrents via the qBittorrent watch folders (by file type)

**Rule:** when Argus adds a torrent *manually* (i.e. not through an \*arr app), it MUST
drop the magnet/.torrent into the qBittorrent **watch folder for that file type** — never
API-add without routing. A category-less `torrents/add` dumps the download into the default
`/data/torrents` with no library home, which then needs hand-importing (we paid for this
once). The watch folders route each grab straight to the correct library.

## The folders (host paths on glassgarden; qBit `/data` = `/mnt/user/downloads/complete`)

| Type | Drop a `.magnet`/`.torrent` here | qBit saves to | = on disk |
|---|---|---|---|
| **music** | `/mnt/user/downloads/complete/watch/music` | `/data/music` | `/mnt/user/media/music` (library) |
| **movies** | `/mnt/user/downloads/complete/watch/movies` | `/data/movies` | `/mnt/user/media/movies` (library) |
| **audiobooks** | `/mnt/user/downloads/complete/watch/audiobooks` | `/data/audiobooks` | `/mnt/user/media/audiobooks` (library) |
| **tv** | `/mnt/user/downloads/complete/watch/tv` | `/data/torrents/tv` | staging (Sonarr import territory) |

qBit consumes the dropped file within seconds and adds it with the routed `save_path`
(category stays empty — routing is by path, not label). `.magnet` files (a text file
containing the magnet URI) and `.torrent` files both work.

## The tool

`~/.local/bin/torrent-grab <music|movies|tv|audiobooks> <magnet-or-url>` — resolves the
source to a magnet (follows one redirect, e.g. a Prowlarr `downloadUrl` that 301s to a
magnet; else fetches the `.torrent`), then ssh-drops it into the right watch folder.
`DRY_RUN=1` reports the target without writing. anvil mounts the *media* shares but not the
downloads share, so the drop goes over ssh (`unraid`).

## After it lands
- **music** → run beets to tag (`docker exec beets beet import -qA …`), then Navidrome rescan.
- **movies / audiobooks** → land directly in the library; a library rescan surfaces them.
- **tv** → Sonarr handles import from staging.

## Scope / exceptions
- This governs **manual grabs** (Prowlarr search → qBit, the music/audiobook flows).
- **\*arr-managed acquisition** (Radarr/Sonarr/Lidarr via the `arr-acquire` lane) is unchanged —
  those apps own their own download routing + import.
- The **audiobook tool lane** (`argus/tools/audiobook.py`) currently API-adds with
  `category: audiobooks`, which already routes to the audiobook library — so it's compliant in
  effect. Migrate it to `torrent-grab` if strict uniformity is wanted.

## Rule for content
No R&B / soul / rap compilations (see memory `music-grab-via-watch-folder`). Otherwise
alt/rock/grunge/pop/etc. is fair game.
