# Changelog

## Unreleased

- Keep the plugin registry, adapter, session source, gateway authorization, and startup restore on the same dynamic `hey` platform identity
- Declare HEY's local allowlist contract explicitly so the gateway rechecks sender authorization without requiring global email environment variables
- Document an atomic Hermes configuration command that preserves the account ID as a canonical decimal string
- Dispatch authorized HEY Collab comments as durable assignments and answer them only with same-thread Collab comments, never email
- Read typed comments directly from topic entries without calling `Messages.Get` for comment IDs
- Add exact `message|comment` event typing, migrate legacy pending events to `message`, and bump the sidecar wire protocol to version 2
- Treat SDK-confirmed non-redirect `2xx` Collab-comment responses as successful mutations
- Enforce the persisted box cursor as the lower bound for hydrated posting bursts so later thread updates cannot replay older messages or Collab comments

## 0.2.0 - 2026-08-25

- Replace the runtime HEY CLI transport with a Go sidecar pinned to the official `hey-sdk/go` v0.24.0
- Use the official SDK for identity/account verification, posting changes, topic/message reads, and same-thread replies; use a custom CLI-compatible provider for OAuth load and refresh
- Hydrate only typed `kind="message"` entries so Collab comments cannot invalidate an email thread
- Persist per-box SDK cursors and advance them only after Python durably ingests and acknowledges each event
- Replay unacknowledged changes after restart while retaining exact DurableQueue deduplication
- Bind every sidecar event to the configured account and exact thread/entry identity
- Preserve sender authorization before queue persistence, per-thread dispatch, reconnect handoff, and final-only delivery acknowledgement
- Add Go race tests, vet, build gates, redacted process errors, bounded JSON, and synthetic Comment/image-mail regressions
- Keep the official HEY CLI only for initial OAuth credential creation
- Pin the build backend, Go toolchain, runner image, Hermes source checkout, lint/type tools, and frozen Python lock workflow
- Build and install both wheel and source distributions in CI, including a wheel-content parity check
- Replace remote-script CLI setup instructions with the pinned HEY CLI v1.1.0 artifact and published SHA-256 workflow
- Document the intentionally unsigned Christopher release path and fail-closed mutation retry classification

## 0.1.1 - 2026-08-25

- Serialize durable dispatch per HEY thread, including reconnect replacements
- Keep every pending identity in the deduplication set regardless of history cap
- Acknowledge only the confirmed final text-reply path
- Block error-notice and attachment fallbacks from creating extra emails
- Correlate burst events with `posting.visible_entry_count` instead of timestamps
- Share state-file locks across adapter instances
- Enforce HEY CLI 1.1.0+ at startup
- Pin CI lint/type-check versions and build distributions in CI

## 0.1.0 - 2026-08-25

- Native Hermes HEY platform registration
- Official HEY CLI inbound watch, hydration, and reply transport
- Exact event deduplication by thread and entry ID
- Authorization before persistence, session creation, or model dispatch
- Stable one-session-per-thread routing and atomic owner-only durable state
- Non-editable final-only email delivery with quiet display defaults
- Strict corrupt-state fail-closed behavior and synthetic fixtures
