# SUPER-SEE (SS) — retired

> **Status:** Retired 2026-08-20. SEE is the only supervisor.
> **Do not** wire `argus/ss/` back into SEE, chat, or llama-swap.

SS was a shadow annotator on SEE (`authority: false`) with a second vocabulary
(`WAIT` vs `STALL`, `REPEATED_TOOL_CALL` vs `LOOP_DETECTED`). That is a second
part. The best part is no part.

- **Job state** is SEE (`specs/see.md`, `specs/see-protocol.md`).
- **Chat liveness** is `GET /argus/turn_status` (`specs/chat-client.md`).
- Remaining files under `argus/ss/` are inert. Do not call `observe_task`.
- `docs/ROADMAP-ss-v1.md` is historical.

Kill switch `ARGUS_SS=0` is set on `argus-ui`. The SEE hook `_ss_shadow` is gone.
