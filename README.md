# Hermes HEY

Native [HEY](https://www.hey.com/) email platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed exclusively by Basecamp's official [`hey` CLI](https://github.com/basecamp/hey-cli).

> Technology preview for Hermes Agent and HEY CLI 1.1.0+.

## Behavior

- Consumes the reconnecting `hey watch --events new` stream.
- Hydrates the full thread and selects the exact entry position reported by
  `posting.visible_entry_count`; ambiguous events fail closed.
- Ignores messages authored by the configured HEY identity.
- Authorizes the sender before writing any event data to disk.
- Persists accepted events atomically with owner-only permissions.
- Routes every HEY thread to its own durable Hermes session.
- Sends a single final email reply to the same thread.
- Serializes pending work per HEY thread across adapter reconnects.
- Rejects direct status, error-notice, and attachment fallback sends so only
  the intended final text response can create an outgoing email.
- Removes pending work only after `hey reply` confirms delivery.
- Disables progressive message editing and operational chatter because sent email cannot be edited.

Remote email is always untrusted input. The adapter defaults to fail-closed: it requires either an explicit `allow_from` list or `allow_all_users: true`.

## Requirements

- Python 3.11+
- Hermes Agent with platform plugin support
- Official HEY CLI 1.1.0 or newer
- An authenticated HEY CLI account

```bash
curl -fsSL https://hey.com/install-cli | bash
hey auth login --no-browser
hey account list --json
hey skill install
```

## Installation

Install the plugin from GitHub:

```bash
hermes plugins install chriopter/hermes-hey --enable
```

For development, link or copy this repository into the active profile's plugin directory:

```bash
ln -s /absolute/path/to/hermes-hey ~/.hermes/plugins/hey-platform
```

Hermes discovers `plugin.yaml` and the `hermes_agent.plugins` entry point automatically.

## Configuration

Use `hermes config set`; do not hand-edit `config.yaml`.

```bash
hermes config set platforms.hey.enabled true
hermes config set platforms.hey.account '12345'
hermes config set platforms.hey.own_email 'agent@example.com'
hermes config set platforms.hey.config_dir '~/.config'
hermes config set platforms.hey.allow_from '["authorized@example.com"]'
hermes config set platforms.hey.allow_all_users false
```

Configuration fields:

- `account`: immutable HEY account ID selected by the CLI.
- `own_email`: authenticated HEY address used for identity validation and loop prevention.
- `config_dir`: XDG configuration root containing `hey-cli/credentials.json`.
- `allow_from`: exact sender-address allowlist, matched case-insensitively.
- `allow_all_users`: explicit opt-in to process every sender; defaults to false.
- `watch_failure_threshold`: consecutive watch failures before Hermes marks the platform fatal; defaults to 5.

The adapter verifies that `account` and `own_email` match `hey account list` before starting the stream.

## Durable state

Authorized pending work is stored below the active Hermes profile at:

```text
$HERMES_HOME/state/hey-platform.json
```

The directory is mode `0700` and the state file is mode `0600` on POSIX. Unauthorized content and identifiers are never persisted. Corrupt state fails closed and is never overwritten automatically.

## Testing

```bash
python -m pytest -q
ruff check .
pyright
python -m build
```

All fixtures are synthetic. Do not add real account, customer, sender, subject, thread, or message data to this repository.

See [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), and
[RELEASE.md](RELEASE.md) for the detailed contracts and release gates.

## Limitations

- HEY email has no edit API, typing indicator, or acknowledgement reaction. Hermes therefore sends no progress mail and delivers only the final response.
- Pending events intentionally remain queued if the agent finishes without a confirmed HEY reply.
- Responses containing attachments deliver the final text portion only. An
  attachment-only response remains pending and unacknowledged.
- Authentication tokens stay in the HEY CLI credential store rather than being duplicated into this project.

## License

MIT. HEY is a trademark of 37signals. This is an independent community plugin.
