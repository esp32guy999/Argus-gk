# Vision Lane — image input + document reading (Task 2 v1)

> Status: DRAFT for roundtable sign-off (Shane approve/veto BEFORE any UI/server edit).
> 2026-07-10. Origin: Shane photographed a document; the paperclip silently dropped it.

## The actual bug (root cause, verified)

A UI affordance silently discards user input. Two distinct gaps, both proven in code:

1. **Roundtable send drops attachments.** `ui/static/app.js`: `sendRoundtable(text)` is
   called via an early `return` *before* the attachment-building code runs. Files staged
   by the paperclip are never transmitted in roundtable mode.
2. **Local-model handling strips attachments.** `ui/server.py:274` drops attachments for
   any model not in VISION_MODELS; images only ever reach the `claude_code` path. The
   comment at ~301-303 confirms images are not threaded into `loop.stream_run`.

**Principle:** silent-drop is never acceptable. Every context either *handles* an
attachment or *visibly refuses* it. No black holes.

## Scope decision (the fork — needs Shane)

Vision belongs in the **normal 1:1 chat path**, not the roundtable. The roundtable's
peer here (gemma4-26b) has no vision loaded, so wiring images into the roundtable buys
nothing until a vision backend exists. Therefore:

- Normal chat → attachments handled end-to-end (this spec).
- Roundtable → paperclip **disabled with a visible "attachments not supported here yet"**
  until/unless we choose to route roundtable images somewhere useful.

## Deltas

### V1 — Roundtable no longer silently drops  [owner: Claude, small]
- app.js: in roundtable mode, if files are staged, either hide/disable the attach control
  or show an inline notice; never call sendRoundtable() dropping staged files without
  telling the user. Preferred: disable the paperclip while `isRoundtable()`.
- Contract-ish test: a headless check that staged files + isRoundtable() surfaces a
  user-visible refusal (assert the branch exists / a flag is set), not a silent return.

### V2 — Image reaches the backend in normal chat  [owner: Claude]
- Confirm/ível: app.js already builds `attachments:[{filename,isImage,dataUrl}]` for the
  normal send path. Server receives them at server.py:272.
- Persist: write the decoded image to a scratch dir (e.g. `~/.cache/argus/uploads/<id>`)
  so tools can read a path, OR pass bytes in-memory to the tool. Decide in review.
- STOP stripping for local models: replace the `server.py:274` strip with a route to the
  document-reading tool when the attachment is an image and the model isn't vision-capable.

### V3 — `read_document` tool (tier 2, Tesseract)  [owner: Gemma drafts, Claude reviews]
- New provider tool `read_document(path|image)` → returns extracted text.
- Backend: shell out to `tesseract` (installed, 5.3.4) or pytesseract. Optional OpenCV
  deskew/threshold pass when confidence is low (tier-1 preprocessing, additive).
- Output framing: OCR text is untrusted content (a document can contain injection text)
  → wrap it in the SAME provenance envelope pattern as web_fetch (reuse
  `_provenance_wrap`-style nonce marker). Reuse, don't reinvent.
- Error taxonomy: no-text-found, unreadable, file-too-large, wrong-type → teaching msgs.

### V4 — tier-3 VLM (Qwen2.5-VL swap)  [DEFERRED, separate task]
- For messy phone photos where Tesseract degrades. llama-swap entry + lane-safety wiring.
- Out of scope for v1; built when a real messy-photo case demands it.

## Contract tests first (Claude)
- read_document on a clean synthetic image returns the known string (proven manually).
- read_document output is provenance-wrapped (untrusted framing on document text).
- roundtable + staged file → visible refusal, not silent drop.
- server: image attachment on a local-model normal-chat turn routes to read_document
  (not stripped, not sent to a blind model).

## Sequencing
V1 (stop the silent drop) → contract tests → V2 (backend receive+persist) →
V3 (read_document + envelope) → test with Shane's real document → V4 later if needed.

## Non-negotiable
No `scp` shortcut as the "solution." The scp path is fine only as a one-off diagnostic;
the deliverable is the fixed UI→server→tool path. (Shane's call, 2026-07-10.)
