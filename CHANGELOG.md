# Changelog

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
- Official `hey watch --events new` inbound stream
- Full-thread hydration with latest-entry sender and content authority
- Exact event deduplication by thread and entry ID
- Authorization before persistence, session creation, or model dispatch
- Sender-address allowlist and authenticated-identity self filtering
- Stable one-session-per-thread routing
- Atomic owner-only durable pending queue with restart recovery
- Exact pending completion only after confirmed `hey reply`
- Reply bodies delivered over stdin rather than process arguments
- Non-editable email transport with one final response and quiet display defaults
- Per-profile HEY CLI config isolation and credential locking
- Configured account/email identity verification before watch startup
- Bounded watch-process restart supervision and fatal reconnect signaling
- Minimal subprocess environment and redacted CLI failures
- Strict corrupt-state fail-closed behavior
- Synthetic fixtures with no real account, customer, sender, or message data
- Unit, security-regression, Hermes runtime-contract, and packaging tests
