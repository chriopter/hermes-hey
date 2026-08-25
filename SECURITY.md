# Security Policy

## Trust boundary

HEY senders, subjects, bodies, links, and event metadata are untrusted user
content. They are delivered as conversational input with
`allow_gateway_control=False`; slash-like text cannot invoke Hermes gateway
controls. Remote content is never interpreted as configuration.

## Authorization

Configure exact sender email addresses under `platforms.hey.allow_from`. The
adapter fails closed when the list is empty. Authorization happens before
state persistence, session lookup, or model dispatch. `allow_all_users: true`
is intended only for explicitly trusted mailboxes.

Boolean settings use strict fail-closed parsing: values such as `"false"` never
enable access. Authorization remains profile-scoped and is not expanded through
process environment variables. Pending authorization is checked again before
every dispatch.

The adapter validates the configured account/email pair and filters the
authenticated HEY identity's own entries to prevent response loops.

## Credentials

Authentication is owned by the official HEY CLI. This plugin never reads,
logs, copies, exports, or stores OAuth token values. Protect the operating
system keyring. On headless systems, HEY CLI may use
`~/.config/hey-cli/credentials.json`; require directory mode `0700`, file mode
`0600`, and treat it as a bearer credential.

Each Hermes profile must use an isolated `extra.config_dir`. The adapter takes
an exclusive scoped lock on the resolved HEY CLI credential context so two live
profiles cannot consume the same mailbox accidentally.

## Data handling

Only authorized events are written to plugin state. State contains exact event
IDs and the compact message data required for restart recovery. The state
directory and file are owner-only on POSIX. Pending work remains durable until
HEY confirms a reply was accepted. No OAuth token, password, TOTP value, or
recovery code is stored in plugin state or this repository.

All repository fixtures use reserved example domains and synthetic IDs. Do not
commit real account, sender, thread, customer, subject, or message data.

## Reporting

Report vulnerabilities privately to the repository maintainer. Do not include
HEY tokens, credential files, account identifiers, sender addresses, or email
content in public issues.
