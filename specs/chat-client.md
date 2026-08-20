# Chat client — inbox

Anvil writes the reply. Finished messages are the source of truth.
Live token SSE is a desktop nicety, never required.

Visual language is **Argus / Material** — tonal surfaces, not iMessage.

This is the inbox client (`ui/static/chat.js`): send once, poll, merge by id.

## Roles

- **Store** is the source of truth (`messages` + `conversations`).
- **Anvil** generates. A turn survives the phone dying.
- **PWA** sends once, polls, renders by db id.
- **SSE** (`/argus/events`) is optional paint. iOS standalone never
  depends on it. Turn lifecycle is `POST /chat` + `GET /history?after_id=`
  + `GET /turn_status`. `turn_status.active` is the only chat heartbeat.
  That is a different clock from SEE’s job `tick` — do not merge them.

## Who talks

- **Dropdown** = local / CPU models (llama-swap + externals). Not Grok, not `z-*`.
- **GK** = SuperGrok OAuth overlay. Does not unload the GPU. Off, or picking a
  dropdown model, sends to that local. On sends to `grok` only.
- **Persona** = voice file (`soul.md` default, `personas/*.md` otherwise). Any
  voice on any model. Not a capability overlay (`soul.d/` stays model-tied).
- Roundtable / `@doc` / `/make` are **not** separate send protocols on the PWA.
  One `POST /argus/chat`.

GPU truth: llama-swap seats one local at a time. GK must never llama-swap.

## Wire contract

### `POST /argus/chat`

```
{
  model?: string,              // fallback if `to` omitted (old clients)
  message: string,
  conversation_id?: string,
  attachments?: [...],
  client_msg_id?: string,      // client uuid; idempotency key
  to?: string[]                // addressed model ids (1+)
}
```

Response:

```
{ id: bubble_id, user_id: int, replay?: bool, done?: bool, to: string[] }
```

Idempotency (`client_msg_id` scoped to the conversation):

| State | Behaviour |
|---|---|
| unknown id | persist user row, start turn, return new `user_id` |
| same id, turn still active | do **not** persist, do **not** start a second generate; return the in-flight `user_id` + `replay: true` |
| same id, all `to` models already replied | return `replay: true, done: true` |
| same id, user row exists, some `to` models missing | resume only the missing models (crash recovery) |

Empty `client_msg_id` = old client = always a new user row.

`to` defaults to `[model]`. Models not yet in the roster are added.
`addressed` is updated to `to`.

One user row, then one assistant row per addressed model, in order.
Each assistant row's `model` is that participant (drives color).

### `GET /argus/history`

Existing `before_id` (page older) plus:

- `after_id=N` — rows with `id > N`, oldest-first, `limit` cap.

### `GET /argus/turn_status`

```
{ active: false }
// or
{ active, elapsed, since_activity, phase, detail, bubble_id, user_id, client_msg_id, model }
```

`active` is the only “is it done?” signal. Token silence is not a stall.

When the turn ends (success, cancel, or error) Anvil **always** writes an
assistant row. The inbox never paints live tokens — no row means a ghost
bubble. Tool-only / crashed Grok turns persist a wrap-up or `[Error: …]`,
never an empty skip.

### Conversations

`GET /argus/conversations` includes `participants` and `addressed`
(inferred from assistant models if the thread has no roster row yet).

`GET /argus/conversations/{id}` — one thread + roster.

`POST /argus/conversations` — `{title?, participants?, addressed?}`.

`PATCH /argus/conversations/{id}` — `{participants?, addressed?, title?}`.
Removing a participant drops them from `addressed`. History is untouched.

## PWA rules

1. **Send lock.** `state.sending` — Enter + tap cannot double-POST.
   The same `client_msg_id` is retried on network failure.
2. **Optimistic user bubble** stamped with `data-client-msg-id`, then
   `data-msg-id` from `user_id`.
3. **No heal-on-silence.** Finalize only when `turn_status.active`
   is false (or the assistant rows for this `user_id` are in history).
4. **Merge by id.** `renderHistory` wipe is allowed only when
   switching conversations. Sync appends/updates `[data-msg-id]`.
5. **iOS standalone:** do not open EventSource. Poll 3–4s + on
   foreground. Desktop may keep SSE for live tokens / inject / warm.
6. **Dropdown + GK + persona** are the send chrome. Participant chips
   are not the send path.
7. Working pill / status pill may stay; they read `turn_status`.

## What dies in `app.js`

- 6s `lastDeltaTs` healer that finalizes a live turn
- `send()` with no in-flight lock
- `_pollSync` → `loadHistory()` mid-turn (full DOM wipe)
- Roundtable / `@doc` / `/make` as separate send protocols
- Treating `bubble_done` as the only way a reply appears
- EventSource as source of truth
