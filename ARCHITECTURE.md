# Architecture

## Decision

Implement HEY as a standalone Hermes platform plugin. Basecamp's official HEY
CLI and Agent Skill remain the authenticated command layer. Inbound detection
uses the CLI's reconnecting `hey watch --events new` stream; thread hydration
and replies use `hey thread read` and `hey reply`.

## Why a platform adapter

A skill handles agent-initiated HEY commands but cannot create inbound Hermes
sessions. The adapter adds live event detection, authorization, durable
queueing, session routing, loop prevention, and response delivery without
modifying Hermes core or copying OAuth credentials.

## Data flow

```text
HEY change stream (`hey watch --events new`)
        │ new posting/thread hint
        ▼
`hey thread read <thread-id>`
        │ entry at posting.visible_entry_count
        ▼
self-filter → sender authorization
        │ accepted events only
        ▼
DurableQueue (atomic, mode 0600)
        │
        ▼
Hermes MessageEvent → persistent `thread:<id>` session
        │ one final response; no progressive email
        ▼
`hey reply <thread-id>` via stdin
        │ confirmed CLI success
        ▼
complete exact durable event identity
```

## Identity and context

- `thread:<thread-id>:entry:<entry-id>` is the immutable event identity.
- `thread:<thread-id>` is the stable Hermes conversation context.
- The watch posting's `visible_entry_count` selects the authoritative thread
  entry for sender, body, and timestamp. This avoids timestamp precision loss
  and same-thread burst collapse.
- The configured HEY account ID and email are verified against
  `hey account list` before the watch process starts.
- Entries authored by the configured HEY address are ignored to prevent loops.

## Authorization

The adapter matches the selected entry's normalized sender email against
`platforms.hey.allow_from`. An empty allowlist fails closed unless
`allow_all_users: true` is explicitly configured. Authorization happens before
state persistence, session creation, or model dispatch. Pending authorization
is rechecked before every dispatch.

## Reliability

- The official watch client owns server reconnect behavior; the adapter
  supervises process-level failures with bounded exponential backoff.
- Authorized events are persisted before dispatch.
- State writes are atomic; corrupt state fails closed and is never overwritten.
- Process-wide per-state-path thread claims prevent duplicate concurrent
  dispatch across reconnect replacement adapters.
- The live-adapter registry hands successful follow-up drains and failed-event
  retries to the replacement adapter rather than chaining work on stale state.
  If no live adapter exists during a reconnect gap, follow-ups remain durable
  and are drained when the replacement registers.
- A processing failure or cancellation releases the exact event for retry; a
  live replacement immediately drains it.
- Pending work is removed immediately after `hey reply` confirms success.
- Reply bodies travel over stdin and never appear in process arguments.
- CLI stderr and malformed bodies are normalized to redacted operation errors.
- The CLI credential context is protected by a profile-scoped exclusive lock.

## Email transport

Sent email cannot be edited. `SUPPORTS_MESSAGE_EDITING` is therefore false and
HEY-specific display defaults suppress tool, thinking, interim, heartbeat, and
busy progress. Direct sends and attachment/error fallbacks are rejected; only
the final text retry path may invoke `hey reply`. If no final reply is confirmed,
the event remains pending rather than being acknowledged incorrectly.
