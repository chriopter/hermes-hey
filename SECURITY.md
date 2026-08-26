# Security Policy

## Trust boundary

HEY senders, comment authors, subjects, message bodies, Collab comment content, links, attachment URLs, and metadata are untrusted input. They enter Hermes with `allow_gateway_control=False`; remote text is never interpreted as gateway configuration or control instructions.

The Go sidecar is a local trusted component, but Python validates frame shape, protocol version, exact event identity, and account binding before persistence or acknowledgement.

## Authorization

Configure exact sender addresses under `platforms.hey.allow_from`. The adapter fails closed with an empty list unless `allow_all_users: true` is explicit. Boolean parsing is strict. Authorization occurs before queue state, session lookup, model dispatch, or visible side effects and is rechecked before dispatch.

The configured account/email pair is validated through the SDK. Own-address entries are filtered in both transport and adapter to prevent loops.

## Credentials

OAuth setup is performed by the official HEY CLI. At runtime, the sidecar supplies the official SDK client with a custom CLI-compatible token provider; it does not use an SDK `AuthManager`. The provider reads the existing headless store at `~/.config/hey-cli/credentials.json`, includes the official public CLI `client_id` and `install_id` during refresh, and serializes token rotation across Watch/Reply processes with a file lock. Credential values are never copied into plugin state, repository files, process arguments, or logs.

Credential directories must be mode `0700` and files mode `0600` on POSIX. Each profile has an exclusive scoped lock on the resolved credential directory so two live profiles cannot consume the same mailbox accidentally.

## Process boundary

- Sidecar executable, account, paths, and IDs are separate argv elements; no shell is used.
- Email-reply and Collab-comment content travels as bounded JSON over stdin.
- Environment propagation is allowlisted and excludes unrelated service-account secrets.
- stdout/NDJSON is size-bounded and schema-checked.
- stderr and remote response bodies are discarded or reduced to redacted operation errors.
- Cursor files are atomic, owner-only, and contain no email content.
- Email-reply and Collab-comment mutations are not automatically repeated after ambiguous network outcomes.

## Data handling

Only authorized events enter the Hermes durable queue. Queue state contains exact event IDs, the typed entry kind, and the compact message or comment data required for restart recovery. It remains until HEY confirms the matching final email reply or Collab comment. No OAuth token, password, TOTP value, recovery code, or credential-store content belongs in queue state or this repository.

All public fixtures use reserved example domains and synthetic IDs. Do not commit real account, sender, thread, customer, subject, message, attachment, or production-event data.

## Reporting

Report vulnerabilities privately to the maintainer. Never include credentials, account identifiers, sender addresses, or email content in public issues.
