# claude-desktop-next

A from-scratch reimagining of `claude-desktop` using FastAPI +
design-token architecture. Keeps the soul of the old app — the draggable
widget canvas — but ships it wrapped in the modern shell.

Port **8100**.

## Layout

```
claude-desktop-next/
├── server.py                     FastAPI shell, proxies brain/HA/qbt/sab/etc.
├── launch.py                     Starts server + Chromium --app window
├── claude-desktop-next.service   systemd user unit (runs server.py only)
├── static/                       index.html, app.js, styles.css, widgets/
└── presets/                      Layout JSON (current.json + named presets)
```

## Running

Install as a systemd user service:

```bash
# On nyx, once the tree is deployed to ~/claude-desktop-next:
mkdir -p ~/.config/systemd/user
cp ~/claude-desktop-next/claude-desktop-next.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-desktop-next.service
```

Check status:

```bash
systemctl --user status claude-desktop-next.service
journalctl --user -u claude-desktop-next.service -f
```

Open a Chromium app window pointed at it:

```bash
flatpak run org.chromium.Chromium \
  --app=http://127.0.0.1:8100 \
  --user-data-dir=/home/shane/.config/claude-desktop-next-chrome
```

Or run both server+window in one shot:

```bash
python3 ~/claude-desktop-next/launch.py
```

## Secrets

Put overrides in `~/.config/claude-desktop-next/env`, e.g.:

```
HERMES_API_KEY=...
HA_TOKEN=...
SAB_API_KEY=...
QBT_USER=shane
QBT_PASS=...
```

The service file already references it via `EnvironmentFile=-%h/.config/claude-desktop-next/env`.

## Architecture

- **Chat backend**: Hermes gateway on port 8642 (OpenAI-compatible API).
  Hermes uses claude-bridge (port 11500) as its upstream LLM provider,
  which translates to Claude via OAuth credentials or `claude -p` CLI fallback.
- All `/brain/*` requests are proxied through FastAPI so the bearer token
  never ships to the browser. SSE (chat stream) is passed through with
  `StreamingResponse` and an `is_disconnected()` check.
- HA calls go through `/ha/*`. qBittorrent through `/qbt/*` (auto-login
  with session cache). SABnzbd through `/sab/queue` and `/sab/history`.
- Layout + named presets live in `presets/` on disk. `current.json` is
  autosaved on every drag/resize/minimize.
- Widgets are dropped in as individual `<script>` tags in `index.html` and
  register themselves into `WIDGET_REGISTRY`.

## Relationship to old apps

- Old `~/claude-desktop/` — retired, kept as archive.
- Old `~/imessage/` — retired, deleted.
- This repo is the primary web UI, backed by Hermes on port 8642.
