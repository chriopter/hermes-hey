# Hermes HEY

Native [HEY](https://www.hey.com/) email platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent), built on Basecamp's official [`hey-sdk`](https://github.com/basecamp/hey-sdk).

> Technology preview. Runtime transport uses `hey-sdk/go` v0.24.0 through a small Go sidecar; the general HEY CLI is used only to create the OAuth credential store.

## Behavior

- Polls HEY's SDK posting-changes feeds with persisted per-box cursors.
- Hydrates topic entries by type and calls `Messages.Get` only for a selected `kind="message"` entry. Collab comments are never treated as email messages.
- Selects the exact position reported by `posting.visible_entry_count`; ambiguous events fail closed.
- Requires a Python acknowledgement after durable ingestion before advancing the SDK cursor. A restart before acknowledgement replays the event safely.
- Ignores messages authored by the configured HEY identity.
- Authorizes senders before writing event data to disk.
- Routes every HEY thread to its own durable Hermes session.
- Sends one final text reply to the same thread and removes pending work only after the SDK confirms success.
- Serializes work per thread across adapter reconnects and rejects progress, error, media-fallback, and duplicate sends.

Remote email is untrusted input. The adapter requires either an explicit `allow_from` list or the deliberate opt-in `allow_all_users: true`.

## Requirements

- Python 3.11+
- Hermes Agent with platform plugin support
- Go 1.26+ to build the sidecar
- HEY OAuth credentials created by the official CLI

Create the OAuth credential store once. Do not execute a remote installer script. The following Linux x86-64 example downloads the official HEY CLI v1.1.0 release artifact and its published checksum file, requires the pinned checksum entry, and verifies the archive before installation (use the matching official artifact and checksum entry on another platform):

```bash
HEY_CLI_VERSION=1.1.0
HEY_CLI_ARCHIVE="hey_${HEY_CLI_VERSION}_linux_amd64.tar.gz"
HEY_CLI_SHA256=0210ea0fc516183a5c770402abdc6305309ff680fea9d772313201bd270d1629
HEY_CLI_RELEASE="https://github.com/basecamp/hey-cli/releases/download/v${HEY_CLI_VERSION}"
tmpdir="$(mktemp -d)"
curl -fL --proto '=https' --tlsv1.2 -o "$tmpdir/$HEY_CLI_ARCHIVE" "$HEY_CLI_RELEASE/$HEY_CLI_ARCHIVE"
curl -fL --proto '=https' --tlsv1.2 -o "$tmpdir/checksums.txt" "$HEY_CLI_RELEASE/checksums.txt"
grep -Fx "$HEY_CLI_SHA256  $HEY_CLI_ARCHIVE" "$tmpdir/checksums.txt"
(cd "$tmpdir" && sha256sum -c <(grep -Fx "$HEY_CLI_SHA256  $HEY_CLI_ARCHIVE" checksums.txt))
tar -xzf "$tmpdir/$HEY_CLI_ARCHIVE" -C "$tmpdir" hey
install -Dm0755 "$tmpdir/hey" "$HOME/.local/bin/hey"
rm -rf "$tmpdir"
hey version
hey auth login --no-browser
hey account list --json
```

The CLI is not used for watch, hydration, identity checks, or replies after setup.

## Installation

```bash
git clone --branch v0.2.0 --depth 1 https://github.com/chriopter/hermes-hey.git
cd hermes-hey
RELEASE_COMMIT="$(git rev-parse HEAD)"
mkdir -p "$HOME/.local/bin"
(cd sidecar && go build -trimpath -o "$HOME/.local/bin/hermes-hey-sidecar" .)
hermes plugins install chriopter/hermes-hey --ref "$RELEASE_COMMIT" --enable
```

Ensure `$HOME/.local/bin` is on the gateway's `PATH`. For a system service, `/usr/local/bin/hermes-hey-sidecar` is a suitable installation path.

For development, link the checkout into the active profile:

```bash
ln -s /absolute/path/to/hermes-hey ~/.hermes/plugins/hey-platform
```

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

Fields:

- `account`: immutable HEY account ID validated through the SDK.
- `own_email`: authenticated HEY address used for identity validation and loop prevention.
- `config_dir`: root containing `hey-cli/credentials.json`.
- `credential_dir`: optional direct credential-store directory; defaults to `<config_dir>/hey-cli`.
- `sidecar_binary`: optional sidecar path; defaults to `hermes-hey-sidecar` on `PATH`.
- `poll_interval`: SDK changes-feed interval; defaults to `1s`.
- `allow_from`: exact sender-address allowlist, case-insensitive.
- `allow_all_users`: explicit opt-in to every sender; defaults to false.
- `watch_failure_threshold`: consecutive process failures before the platform becomes fatal; defaults to 5.

## Durable state

Authorized pending work and transport cursors live under the active profile:

```text
$HERMES_HOME/state/hey-platform.json
$HERMES_HOME/state/hey-sdk-cursors.json
```

State is written atomically with owner-only POSIX permissions. Cursor state contains no email bodies. Unauthorized content and identifiers are not added to the Hermes queue. Corrupt state fails closed and is never silently replaced.

## Testing

```bash
uv sync --frozen --extra dev
uv run --frozen pytest -q
uvx ruff@0.16.4 check .
uvx pyright@1.1.411
(cd sidecar && go test -race ./...)
(cd sidecar && go vet ./...)
(cd sidecar && go build .)
uvx --from build==1.5.0 pyproject-build
```

All fixtures are synthetic; never add real account, customer, sender, subject, thread, message, attachment, or token data.

See [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), and [RELEASE.md](RELEASE.md).

## Limitations

- HEY email cannot be edited after sending, so Hermes emits no progress mail and only one final response.
- Pending work remains queued if processing finishes without a confirmed SDK reply.
- Final responses are text-only; attachment fallbacks do not create a second email.
- Initial startup intentionally baselines existing box cursors and does not replay mailbox history.

## License

MIT. HEY is a trademark of 37signals. This is an independent community plugin.
