from __future__ import annotations

import inspect
import io
import json
import threading
import time

import pytest

from hey_platform.client import (
    SDK_VERSION,
    HeySDKClient,
    _WindowsJob,
    canonical_account,
    make_sidecar_runner,
)


class FakeWindowsJob:
    creation_flags = 0x4

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def attach(self, process) -> None:
        self.events.append("attach")

    def terminate(self) -> None:
        self.events.append("terminate")

    def close(self) -> None:
        self.events.append("close")


class FailingWindowsJob(FakeWindowsJob):
    def attach(self, process) -> None:
        self.events.append("attach")
        raise OSError("assignment failed")


def test_windows_job_source_contract_is_cross_platform_importable() -> None:
    source = inspect.getsource(_WindowsJob)

    assert _WindowsJob.creation_flags == 0x00000004
    assert "CreateJobObjectW" in source
    assert "SetInformationJobObject" in source
    assert "AssignProcessToJobObject" in source
    assert "ResumeThread" in source
    assert "TerminateJobObject" in source
    assert "CloseHandle" in source
    assert "0x00002000" in source


def test_sdk_client_uses_protocol_verify_and_reply_without_argument_content() -> None:
    calls: list[tuple[list[str], dict | None]] = []

    def runner(args: list[str], payload: dict | None = None) -> dict:
        calls.append((args, payload))
        if args[0] == "verify":
            return {
                "ok": True,
                "protocol_version": 1,
                "sdk_version": "0.24.0",
                "account": 12345,
                "email": "agent@example.com",
            }
        return {"ok": True}

    client = HeySDKClient(
        runner,
        account="12345",
        own_email="agent@example.com",
    )

    assert client.verify() is True
    assert client.reply(456, "private reply") == {"ok": True}
    assert calls == [
        (["verify", "--own-email", "agent@example.com"], None),
        (["reply", "--thread-id", "456"], {"content": "private reply"}),
    ]
    assert "private reply" not in calls[1][0]


def test_sdk_client_rejects_boolean_protocol_version() -> None:
    client = HeySDKClient(
        lambda _args, _payload: {
            "ok": True,
            "protocol_version": True,
            "sdk_version": "0.24.0",
            "account": 12345,
            "email": "agent@example.com",
        },
        account="12345",
        own_email="agent@example.com",
    )

    with pytest.raises(RuntimeError, match="sidecar verification failed"):
        client.verify()


def test_sdk_client_requires_the_single_pinned_sdk_version() -> None:
    assert SDK_VERSION == "0.24.0"
    client = HeySDKClient(
        lambda _args, _payload: {
            "ok": True,
            "protocol_version": 1,
            "sdk_version": "0.24.1",
            "account": 12345,
            "email": "agent@example.com",
        },
        account="12345",
        own_email="agent@example.com",
    )

    with pytest.raises(RuntimeError, match="sidecar verification failed"):
        client.verify()


def test_canonical_account_accepts_exact_go_int64_maximum() -> None:
    assert canonical_account("9223372036854775807") == "9223372036854775807"


@pytest.mark.parametrize(
    "account",
    ["9223372036854775808", "9" * 10_000],
    ids=["max-plus-one", "huge"],
)
def test_canonical_account_rejects_values_above_go_int64_maximum(account: str) -> None:
    with pytest.raises(ValueError, match="canonical ASCII account"):
        canonical_account(account)


@pytest.mark.parametrize(
    "account",
    [
        "",
        "0",
        "01",
        "+1",
        "-1",
        " 1",
        "1 ",
        "١",
        "9223372036854775808",
        "9" * 10_000,
        1,
        True,
        None,
    ],
)
def test_sdk_client_rejects_noncanonical_configured_account(account: object) -> None:
    with pytest.raises(ValueError, match="canonical ASCII account"):
        HeySDKClient(
            lambda _args, _payload: {},
            account=account,  # type: ignore[arg-type]
            own_email="agent@example.com",
        )


@pytest.mark.parametrize(
    "result",
    [
        {"protocol_version": 1, "sdk_version": "0.24.0", "account": 12345, "email": "agent@example.com"},
        {"ok": True, "sdk_version": "0.24.0", "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "account": 12345},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "account": 12345, "email": "agent@example.com", "extra": None},
        {"ok": 1, "protocol_version": 1, "sdk_version": "0.24.0", "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": "1", "sdk_version": "0.24.0", "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 2, "sdk_version": "0.24.0", "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": 0.24, "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "v0.24.0", "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24", "account": 12345, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "account": "12345", "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "account": True, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "account": 54321, "email": "agent@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "account": 12345, "email": "AGENT@example.com"},
        {"ok": True, "protocol_version": 1, "sdk_version": "0.24.0", "account": 12345, "email": "other@example.com"},
    ],
    ids=[
        "missing-ok",
        "missing-protocol",
        "missing-sdk-version",
        "missing-account",
        "missing-email",
        "extra-key",
        "coerced-ok",
        "coerced-protocol",
        "wrong-protocol",
        "coerced-sdk-version",
        "prefixed-sdk-version",
        "incomplete-sdk-version",
        "coerced-account",
        "boolean-account",
        "wrong-account",
        "unnormalized-email",
        "wrong-email",
    ],
)
def test_sdk_client_verify_rejects_non_exact_response_schema(result: dict) -> None:
    client = HeySDKClient(
        lambda _args, _payload: result,
        account="12345",
        own_email=" Agent@Example.COM ",
    )

    with pytest.raises(RuntimeError, match="sidecar verification failed"):
        client.verify()


@pytest.mark.parametrize(
    "result",
    [{}, {"ok": False}, {"ok": 1}, {"ok": True, "data": {}}, {"ok": True, "extra": None}],
)
def test_sdk_client_reply_rejects_non_exact_response_schema(result: dict) -> None:
    client = HeySDKClient(
        lambda _args, _payload: result,
        account="12345",
        own_email="agent@example.com",
    )

    with pytest.raises(RuntimeError, match="HEY SDK reply failed"):
        client.reply(456, "private reply")


def test_sidecar_runner_uses_json_stdin_and_redacts_failures(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "must-not-propagate")
    binary = tmp_path / "hermes-hey-sidecar"
    capture = tmp_path / "capture.json"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{"
        "'argv': sys.argv[1:], 'stdin': sys.stdin.read(), "
        "'no_keyring': os.environ.get('HEY_NO_KEYRING'), "
        "'has_secret': 'OP_SERVICE_ACCOUNT_TOKEN' in os.environ}))\n"
        "sys.stderr.write('private remote response')\n"
        "raise SystemExit(75)\n"
    )
    binary.chmod(0o700)
    runner = make_sidecar_runner(
        binary=str(binary),
        account="12345",
        credential_dir=str(tmp_path / "credentials"),
    )

    with pytest.raises(RuntimeError, match=r"HEY SDK reply failed \(exit 75\)") as exc:
        runner(["reply", "--thread-id", "456"], {"content": "private reply"})

    assert "private reply" not in str(exc.value)
    assert "private remote response" not in str(exc.value)
    captured = json.loads(capture.read_text())
    assert captured["argv"] == [
        "reply",
        "--thread-id",
        "456",
        "--account",
        "12345",
        "--config-dir",
        str((tmp_path / "credentials").resolve()),
    ]
    assert json.loads(captured["stdin"]) == {"content": "private reply"}
    assert captured["no_keyring"] == "1"
    assert captured["has_secret"] is False


@pytest.mark.parametrize(
    ("operation", "output"),
    [
        ("verify", '{"ok":false,"ok":true}'),
        ("verify", '{"protocol_version":0,"protocol_version":1}'),
        ("reply", '{"ok":false,"ok":true}'),
    ],
    ids=["verify-ok", "verify-protocol-version", "reply-ok"],
)
def test_sidecar_runner_rejects_duplicate_response_keys_without_echoing_output(
    operation: str, output: str, tmp_path
) -> None:
    binary = tmp_path / "duplicate-key-sidecar"
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' {output!r}\n")
    binary.chmod(0o700)
    runner = make_sidecar_runner(
        binary=str(binary), account="12345", credential_dir=str(tmp_path)
    )
    client = HeySDKClient(
        runner, account="12345", own_email="agent@example.com"
    )

    with pytest.raises(RuntimeError, match=f"HEY SDK {operation} returned invalid JSON") as exc:
        if operation == "verify":
            client.verify()
        else:
            client.reply(456, "private reply")

    assert output not in str(exc.value)


@pytest.mark.parametrize(
    "account", ["01", "9223372036854775808", "9" * 10_000, 1, True]
)
def test_sidecar_runner_rejects_noncanonical_configured_account(
    account: object, tmp_path
) -> None:
    with pytest.raises(ValueError, match="canonical ASCII account"):
        make_sidecar_runner(
            binary="sidecar",
            account=account,  # type: ignore[arg-type]
            credential_dir=str(tmp_path),
        )


def test_runner_and_client_accept_exact_go_int64_maximum(tmp_path) -> None:
    maximum = "9223372036854775807"
    runner = make_sidecar_runner(
        binary="sidecar", account=maximum, credential_dir=str(tmp_path)
    )
    client = HeySDKClient(
        runner, account=maximum, own_email="agent@example.com"
    )

    assert client.account == maximum


def test_sidecar_runner_assigns_suspended_windows_child_to_job_before_use(
    monkeypatch, tmp_path
) -> None:
    events: list[str] = []

    class Process:
        pid = 123
        returncode = 0
        stdout = io.BytesIO(b'{"ok":true}')

        def poll(self):
            events.append("poll")
            return self.returncode

    def popen(*_args, **kwargs):
        events.append(f"spawn:{kwargs['creationflags']}")
        return Process()

    monkeypatch.setattr("hey_platform.client.subprocess.Popen", popen)
    runner = make_sidecar_runner(
        binary=str(tmp_path / "sidecar"),
        account="12345",
        credential_dir=str(tmp_path),
        _process_tree_factory=lambda: FakeWindowsJob(events),
    )

    assert runner(["verify"], None) == {"ok": True}
    assert events[:3] == ["spawn:4", "attach", "poll"]
    assert events[-1] == "close"


def test_sidecar_runner_kills_suspended_child_when_job_assignment_fails(
    monkeypatch, tmp_path
) -> None:
    events: list[str] = []

    class Process:
        stdout = io.BytesIO()

        def kill(self):
            events.append("kill")

        def wait(self):
            events.append("wait")

    monkeypatch.setattr("hey_platform.client.subprocess.Popen", lambda *_a, **_kw: Process())
    runner = make_sidecar_runner(
        binary=str(tmp_path / "sidecar"),
        account="12345",
        credential_dir=str(tmp_path),
        _process_tree_factory=lambda: FailingWindowsJob(events),
    )

    with pytest.raises(RuntimeError, match="could not start"):
        runner(["verify"], None)

    assert events == ["attach", "kill", "wait", "close"]


def test_sidecar_runner_redacts_windows_job_creation_failure(tmp_path) -> None:
    def fail_job_creation():
        raise OSError("private Windows error")

    runner = make_sidecar_runner(
        binary=str(tmp_path / "sidecar"),
        account="12345",
        credential_dir=str(tmp_path),
        _process_tree_factory=fail_job_creation,
    )

    with pytest.raises(RuntimeError, match="HEY SDK verify could not start") as exc:
        runner(["verify"], None)

    assert "private Windows error" not in str(exc.value)


def test_sidecar_runner_terminates_process_as_soon_as_output_is_oversized(tmp_path) -> None:
    binary = tmp_path / "oversized-sidecar"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdout.buffer.write(b'x' * 1_048_577)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(10)\n"
    )
    binary.chmod(0o700)
    runner = make_sidecar_runner(
        binary=str(binary), account="12345", credential_dir=str(tmp_path)
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="returned oversized JSON"):
        runner(["verify"], None)

    assert time.monotonic() - started < 4


def test_sidecar_runner_does_not_wait_for_descendant_inheriting_stdout(tmp_path) -> None:
    binary = tmp_path / "sidecar-with-stdout-descendant"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'])\n"
        "print(json.dumps({'ok': True, 'protocol_version': 1}))\n"
    )
    binary.chmod(0o700)
    runner = make_sidecar_runner(
        binary=str(binary), account="12345", credential_dir=str(tmp_path)
    )
    existing_threads = set(threading.enumerate())

    started = time.monotonic()
    result = runner(["verify"], None)

    assert result == {"ok": True, "protocol_version": 1}
    assert time.monotonic() - started < 3
    assert set(threading.enumerate()) <= existing_threads


def test_sidecar_runner_force_kills_pipe_descendant_ignoring_termination(
    tmp_path,
) -> None:
    binary = tmp_path / "sidecar-with-resistant-descendant"
    ready = tmp_path / "descendant-ready"
    descendant = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(6)"
    )
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, subprocess, sys, time\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
        "while not ready.exists(): time.sleep(0.01)\n"
        "print(json.dumps({'ok': True, 'protocol_version': 1}))\n"
    )
    binary.chmod(0o700)
    runner = make_sidecar_runner(
        binary=str(binary), account="12345", credential_dir=str(tmp_path)
    )
    existing_threads = set(threading.enumerate())

    started = time.monotonic()
    result = runner(["verify"], None)

    assert result == {"ok": True, "protocol_version": 1}
    assert time.monotonic() - started < 3
    assert set(threading.enumerate()) <= existing_threads
