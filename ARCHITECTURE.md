# Architecture

## Decision

HEY runtime transport is implemented by a small Go sidecar pinned to Basecamp's official `hey-sdk/go` v0.24.0. The Python plugin owns authorization, durable work, Hermes sessions, and final-delivery acknowledgement. The general HEY CLI is outside the runtime path and is retained only for initial OAuth login.

## Why a sidecar

Hermes platform plugins are Python, while the official SDK is Go. A narrow JSON/NDJSON process boundary keeps SDK models, API HTTP behavior, and HEY operations in Go without weakening the existing Hermes queue and lifecycle contracts. A CLI-compatible token provider supplies the official SDK client and protects rotating OAuth credentials with a cross-process file lock.

## Data flow

```text
SDK box posting-changes cursor
        │ added/updated posting
        ▼
SDK topic entries (typed, paginated)
        │ exact posting.visible_entry_count position
        ├─ comment → typed entry; never Messages.Get
        ├─ message → SDK Messages.Get
        └─ other → skip
                         │ complete typed sidecar event
                         ▼
self-filter → account binding → sender authorization
                         │ accepted events only
                         ▼
DurableQueue (atomic, mode 0600)
                         │ persisted before acknowledgement
                         ▼
NDJSON ack to sidecar → atomically advance box cursor
                         │
                         ▼
Hermes MessageEvent → persistent `thread:<id>` session
                         │ one final response matching input kind
                         ▼
message → SDK Entries reply; comment → SDK Collab comment
                         │ confirmed success
                         ▼
complete exact durable event identity
```

## Process protocol

- `verify` validates protocol version, authenticated identity, account membership, and configured email.
- `watch` emits `ready`, `event`, and redacted `fatal` NDJSON frames. Each event blocks cursor advancement until Python returns `{"ack":"thread:<id>:entry:<id>"}`.
- `reply` reads the body from JSON stdin, asks HEY for server-computed reply recipients, and performs at most one same-thread reply mutation per sidecar invocation.
- `comment` reads the body from JSON stdin and performs at most one same-thread Collab-comment mutation per sidecar invocation.
- Reply/comment content and tokens never appear in process arguments, stdout diagnostics, or logs.

## Identity and context

- `thread:<thread-id>:entry:<entry-id>` is the immutable event identity and must match the frame's numeric IDs.
- `thread:<thread-id>` is the stable Hermes conversation context.
- `visible_entry_count` identifies the authoritative entry; position, kind, and range are validated fail closed.
- The configured account and email are verified before watch startup and every event is account-bound again in Python.
- Own-address events are rejected in both sidecar and Python.

## Authorization

The normalized sender email is matched against `platforms.hey.allow_from`. An empty allowlist fails closed unless `allow_all_users: true` is explicit. Authorization occurs before queue persistence, session creation, model dispatch, or visible side effects. Pending authorization is rechecked before each dispatch.

## Reliability

- First startup establishes per-box cursors without replaying history.
- Cursor state is atomic and mode `0600`; corruption fails closed.
- The sidecar advances a cursor only after every emitted event in that increment is acknowledged by Python.
- Restart before acknowledgement replays from the prior cursor; the DurableQueue deduplicates exact event identities.
- Authorized events are durable before agent dispatch.
- Process-wide per-state-path thread claims prevent concurrent same-thread dispatch across replacement adapters.
- Failures and cancellations release exact work for a live replacement; reconnect gaps retain pending state.
- Sidecar process failures use bounded exponential supervision. External bodies and stderr are normalized to redacted operation errors.
- Reply and comment mutations are never retried after an ambiguous outcome. The retryable process code is limited to classified transient failures before mutation, plus responses that definitively confirm non-application with HTTP 404, 409, 422, or 429. Timeouts, connection loss, 5xx responses, and any other ambiguous mutation outcome are non-retryable.

## Final-response transport

Sent email and Collab comments cannot be edited. `SUPPORTS_MESSAGE_EDITING` is false and HEY display defaults suppress tool, thinking, interim, heartbeat, and busy progress. Direct, error, media, and fallback sends are rejected. Only the final text path may invoke the mutation matching the durable event kind: email reply for `message`, Collab comment for `comment`. Pending work is completed only after confirmed success.
