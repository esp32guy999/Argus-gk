# Chat client — inbox + participants

Anvil writes the reply. The PWA is a **room**: you and the models you
invite. Finished messages are the source of truth. Live token SSE is a
desktop nicety, never required.

Visual language is **Argus / Material** — tonal surfaces, 8dp chips,
colored participant tokens — not iMessage (no tails, no lock-screen
blue/gray bubbles). Each model keeps its accent from `models.yaml`.

This is A+B from the 2026-08-17 plan, plus a participant roster.

## Roles

- **Store** is the source of truth (`messages` + `conversations`).
- **Anvil** generates. A turn survives the phone dying.
- **PWA** sends once, polls, renders by db id.
- **SSE** (`/argus/events`) is optional paint. iOS standalone never
  depends on it. Turn lifecycle is `POST /chat` + `GET /history?after_id=`
  + `GET /turn_status`.

## Participants (messenger)

A conversation is a room.

- **Shane** is implicit (right-aligned user bubbles). No chip.
- **Models** are participants. Each keeps the accent from
  `config/models.yaml` (same palette as today: Loki emerald, 80B violet,
  coder gold, Grok blue, Gemma pink, …).
- **Add** joins the room. **Remove** leaves the room. Their old bubbles
  stay, still colored.
- **Addressed** is who this send goes to — one or more chips, toggled
  by tap. Send is disabled if the roster or the addressee list is empty.
- Adding a model does **not** auto-address them; tap to talk to them.
  The first model in a new room is addressed by default.
- New chat seeds with the last-used chat model (low friction).
- Roundtable is not a special protocol. It is the `roundtable` thread
  with Gemma + Claude in the room. Same send path.
- Grok is a participant you add, not a GK overlay.
- Not inviteable: `z-*` media models, legacy `claude-code`.

GPU truth is unchanged: llama-swap still seats one local model at a
time. Addressing two locals = sequential generate (swap between).

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
6. **Participant bar** replaces the model `<select>` + GK toggle.
   Chip color = accent. Filled = addressed. `+` invites. `×` removes.
7. Working pill / status pill may stay; they read `turn_status`.

## What dies in `app.js`

- 6s `lastDeltaTs` healer that finalizes a live turn
- `send()` with no in-flight lock
- `_pollSync` → `loadHistory()` mid-turn (full DOM wipe)
- Roundtable as a separate send protocol (`/argus/roundtable` may
  linger for old clients; the PWA does not use it)
- GK toggle as the way to talk to Grok
- Treating `bubble_done` as the only way a reply appears
