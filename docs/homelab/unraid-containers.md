# Rule: homelab containers are Unraid templates (icon + WebUI)

When adding a service to **glassgarden** (Unraid), define it as an **Unraid Docker
template** (Community-Applications XML) — not a bare `docker run`. Every template MUST
set an **`<Icon>`** and a **`<WebUI>`** so the service is first-class in the Docker tab:
icon shows on the dashboard, and **"WebUI"** appears in the container's dropdown menu.

## Required
- **`<Icon>`** — URL to a PNG/SVG (any reachable image). Renders in the Docker tab.
- **`<WebUI>`** — `http://[IP]:[PORT:nnnn]/` — adds the WebUI link to the dropdown.
- A **`<Config>`** block per port / path / variable (with `Name` + `Target` +
  `Description`) so each is editable in the UI. Use Unraid defaults `PUID=99 PGID=100`
  and a `TZ`. Mount media shares from `/mnt/user/media/...`, appdata under
  `/mnt/user/appdata/<name>`.

Templates live on the Unraid box at:
`/boot/config/plugins/dockerMan/templates-user/my-<name>.xml`
Drop the XML there → it appears under **Add Container → user templates**, or just shows
in the Docker tab.

## Example — beets (music tagging / organizer)
```xml
<?xml version="1.0"?>
<Container version="2">
  <Name>beets</Name>
  <Repository>lscr.io/linuxserver/beets:latest</Repository>
  <Registry>https://hub.docker.com/r/linuxserver/beets</Registry>
  <Network>bridge</Network>
  <Privileged>false</Privileged>
  <Icon>https://raw.githubusercontent.com/linuxserver/docker-templates/master/linuxserver.io/img/beets-logo.png</Icon>
  <WebUI>http://[IP]:[PORT:8337]/</WebUI>
  <Config Name="WebUI Port" Target="8337" Default="8337" Mode="tcp" Type="Port" Description="beets web plugin">8337</Config>
  <Config Name="Config"  Target="/config"    Default="/mnt/user/appdata/beets" Mode="rw" Type="Path" Description="beets config + db">/mnt/user/appdata/beets</Config>
  <Config Name="Music"   Target="/music"     Default="/mnt/user/media/music"   Mode="rw" Type="Path" Description="Navidrome library (write to re-tag/move)">/mnt/user/media/music</Config>
  <Config Name="Imports" Target="/downloads" Default="/mnt/user/downloads"      Mode="rw" Type="Path" Description="where new comps land">/mnt/user/downloads</Config>
  <Config Name="PUID" Target="PUID" Default="99"  Type="Variable">99</Config>
  <Config Name="PGID" Target="PGID" Default="100" Type="Variable">100</Config>
  <Config Name="TZ"   Target="TZ"   Default="America/New_York" Type="Variable">America/New_York</Config>
</Container>
```

Notes:
- Verify the `<Icon>` URL resolves; swap for any PNG if the LSIO path moved.
- Enable the beets **`web`** plugin in `/config/config.yaml` so `:8337` serves the UI.
- Import a decade comp: `docker exec -it beets beet import /downloads/<comp>`
  (set `va_name`/compilation handling so "Hits of the 70s" tags as Various Artists).

## Example — SearXNG (Argus web_search backend)

Installed from **Community Apps** (Kilrah's Repository template:
`https://raw.githubusercontent.com/kilrah/unraid-docker-templates/main/templates/searxng.xml`).
User template on glassgarden: `/boot/config/plugins/dockerMan/templates-user/my-SearXNG.xml`.

| | |
|--|--|
| Image | `searxng/searxng` |
| Host port | **8089** → container 8080 (8080 is sabnzbd on this box) |
| Appdata | `/mnt/user/appdata/searxng` (`settings.yml` enables `json` format, `limiter: false`) |
| WebUI | `http://glassgarden:8089` / `http://192.168.4.206:8089` |
| Argus env | `SEARXNG_URL=http://192.168.4.206:8089` (LAN; set on `argus-ui` systemd) |

JSON smoke: `curl 'http://192.168.4.206:8089/search?q=test&format=json'`.
Do **not** install with a bare `docker run` and no user template — that shows as an
orphan on the Docker tab.
