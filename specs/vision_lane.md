# Vision / Attachments Lane — paperclip end-to-end

> Status: **IMPLEMENTED** (2026-08-03). Replaces the silent-drop paperclip path.
> Origin: Shane photographed a document; the paperclip silently dropped it (2026-07-10).

## Principle

**No black holes.** Every staged attachment is either:
1. materialised to disk and consumed by the model path, or
2. refused with a **visible** user-facing error.

## Architecture

```
UI paperclip / paste / drop
  → stagedFiles [{filename, isImage, dataUrl}]
  → POST /argus/chat {attachments}
       ↓
  argus.attachments.materialize()  → ~/.cache/argus/uploads/<cid>/…
       ↓
  ┌─ grok / claude-code ── native vision (image content blocks)
  │     grok: --prompt-json  (argus/grok_code.py)
  │     cc:   stream-json image blocks (argus/claude_code.py)
  └─ local / other ────── enrich_prompt() OCR + text inline + path markers
                            + read_document tool for re-OCR by path
```

| Path | Images | Other files |
|------|--------|-------------|
| **GK (grok)** | `--prompt-json` base64 image blocks | path text blocks on disk |
| **claude-code** | Anthropic image blocks | path markers in text |
| **Local models** | tesseract OCR + path | inline text / pdftotext / path |
| **Roundtable** | **Refused** with visible message (no vision peer) | same |

## Key modules

- `argus/attachments.py` — materialise, vision blocks, OCR enrich, store text
- `argus/grok_code.py` — `--prompt-json` when attachments present
- `argus/tools/native.py` — `read_document(path)` tool
- `ui/server.py` — never strips; 400 on hard attachment errors
- `ui/static/app.js` — roundtable refuse; show API error detail; re-stage on 400

## Env

| Var | Default | Meaning |
|-----|---------|---------|
| `ARGUS_UPLOAD_ROOT` | `~/.cache/argus/uploads` | on-disk store |
| `ARGUS_ATTACH_MAX_BYTES` | 12 MiB | per-file cap |
| `ARGUS_ATTACH_MAX_FILES` | 8 | per turn |

## Tests

```bash
.venv/bin/python tests/test_attachments.py
```

## Out of scope / later

- Persist thumbnails into the conversation store for reload (history still has `[attached: …]` text).
- Roundtable vision peer.
- Local VLM (Qwen2.5-VL) for messy photos when OCR fails.
