from __future__ import annotations

import subprocess

import pytest

from hey_platform.client import HeyCLI, make_subprocess_runner


def watch_line() -> dict:
    return {
        "change": "added",
        "at": "2026-08-25T16:10:00Z",
        "posting_id": 123,
        "thread_id": 456,
        "new": True,
        "box": {"id": 7, "kind": "imbox", "name": "Imbox"},
        "posting": {
            "id": 123,
            "name": "Request",
            "account_id": 12345,
            "active_at": "2026-08-25T16:09:59Z",
            "visible_entry_count": 1,
        },
    }


def thread_entry(email: str = "authorized@example.com") -> dict:
    return {
        "id": 900,
        "created_at": "2026-08-25T16:09:59Z",
        "creator": {"id": 77, "name": "Christopher", "email_address": email},
        "body": "Please handle this.",
        "body_state": "hydrated",
    }


def test_cli_hydrates_watch_event_from_thread_read() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def runner(args: list[str], stdin: str | None = None) -> dict:
        calls.append((args, stdin))
        return {"ok": True, "data": [thread_entry()]}

    event = HeyCLI(runner, account="12345", own_email="agent@example.com").hydrate_event(watch_line())

    assert event is not None
    assert event.sender_email == "authorized@example.com"
    assert calls == [(["thread", "read", "456"], None)]


def test_reply_uses_stdin_not_process_arguments() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def runner(args: list[str], stdin: str | None = None) -> dict:
        calls.append((args, stdin))
        return {"ok": True, "data": {"id": 991}}

    result = HeyCLI(runner, account="12345", own_email="agent@example.com").reply(456, "private reply")

    assert result["data"]["id"] == 991
    assert calls == [(["reply", "456"], "private reply")]
    assert "private reply" not in calls[0][0]


def test_cli_identity_must_match_configured_account_and_email() -> None:
    response = {
        "ok": True,
        "data": [
            {"id": "12345", "name": "Work", "email": "agent@example.com"}
        ],
    }
    cli = HeyCLI(lambda _args, _stdin=None: response, account="12345", own_email="agent@example.com")
    assert cli.verify_identity() is True

    mismatch = HeyCLI(lambda _args, _stdin=None: response, account="12345", own_email="other@example.com")
    with pytest.raises(RuntimeError, match="identity does not match"):
        mismatch.verify_identity()


def test_cli_rejects_versions_older_than_1_1_0() -> None:
    supported = HeyCLI(
        lambda _args, _stdin=None: {"ok": True, "data": {"version": "1.1.0"}},
        account="12345",
        own_email="agent@example.com",
    )
    assert supported.verify_version() is True

    outdated = HeyCLI(
        lambda _args, _stdin=None: {"ok": True, "data": {"version": "1.0.9"}},
        account="12345",
        own_email="agent@example.com",
    )
    with pytest.raises(RuntimeError, match="1.1.0 or newer"):
        outdated.verify_version()


@pytest.mark.parametrize(
    "version",
    [
        "1.1.0-rc1",
        "1.1.0-",
        "garbage",
        "",
        "01.1.0",
        "1.01.0",
        "1.1.00",
        "1.1.0+.",
        "1.1.0+..",
        "1.1.0+foo..bar",
        "1١.1.0",
        "1.1٢.0",
        "1.1.٠",
    ],
)
def test_cli_rejects_prerelease_or_malformed_versions(version: str) -> None:
    cli = HeyCLI(
        lambda _args, _stdin=None: {"ok": True, "data": {"version": version}},
        account="12345",
        own_email="agent@example.com",
    )
    with pytest.raises(RuntimeError, match="1.1.0 or newer"):
        cli.verify_version()


@pytest.mark.parametrize(
    "version",
    [
        "1.1.0",
        "1.1.0+build.7",
        "1.1.0+-",
        "1.1.0+-foo",
        "1.1.0+foo-",
        "1.2.0",
        "2.0.0",
    ],
)
def test_cli_accepts_final_supported_versions(version: str) -> None:
    cli = HeyCLI(
        lambda _args, _stdin=None: {"ok": True, "data": {"version": version}},
        account="12345",
        own_email="agent@example.com",
    )
    assert cli.verify_version() is True


def test_runner_redacts_command_values_on_failure(monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="secret response")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = make_subprocess_runner(account="12345", config_dir=str(tmp_path))

    with pytest.raises(RuntimeError, match=r"hey reply failed \(exit 7\)") as exc:
        runner(["reply", "456"], "private reply")

    assert "private reply" not in str(exc.value)
    assert seen["command"] == ["hey", "reply", "456", "--json", "--account", "12345"]
    assert seen["kwargs"]["input"] == "private reply"
    assert seen["kwargs"]["env"]["XDG_CONFIG_HOME"] == str(tmp_path.resolve())
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in seen["kwargs"]["env"]


def test_runner_rejects_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="not-json", stderr=""),
    )
    with pytest.raises(RuntimeError, match="invalid JSON"):
        make_subprocess_runner()(["account", "list"], None)
