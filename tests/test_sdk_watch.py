from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from hey_platform.client import HeySDKWatch


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


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self, stdout_lines: list[bytes], *, code: int = 75) -> None:
        self.stdout = asyncio.StreamReader()
        for line in stdout_lines:
            self.stdout.feed_data(line)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdin = FakeStdin()
        self.returncode: int | None = code
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        return int(self.returncode or 0)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.parametrize(
    "account", ["01", "9223372036854775808", "9" * 10_000, 1, True]
)
def test_sdk_watch_rejects_noncanonical_configured_account(account: object, tmp_path) -> None:
    with pytest.raises(ValueError, match="canonical ASCII account"):
        HeySDKWatch(
            binary="sidecar",
            account=account,  # type: ignore[arg-type]
            own_email="agent@example.com",
            credential_dir=str(tmp_path),
            cursor_state=str(tmp_path / "cursor"),
        )


def test_sdk_watch_accepts_exact_go_int64_maximum(tmp_path) -> None:
    maximum = "9223372036854775807"
    watch = HeySDKWatch(
        binary="sidecar",
        account=maximum,
        own_email="agent@example.com",
        credential_dir=str(tmp_path),
        cursor_state=str(tmp_path / "cursor"),
    )

    assert watch.account == maximum


@pytest.mark.asyncio
async def test_sdk_watch_emits_event_and_writes_explicit_ack(monkeypatch, tmp_path) -> None:
    event = {
        "event_id": "thread:456:entry:900",
        "posting_id": 123,
        "thread_id": 456,
        "entry_id": 900,
        "sender_email": "authorized@example.com",
    }
    process = FakeProcess(
        [
            b'{"type":"ready","protocol_version":1}\n',
            (json.dumps({"type": "event", "event": event}) + "\n").encode(),
        ]
    )
    captured: dict = {}

    async def fake_create(*command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "must-not-propagate")
    watch = HeySDKWatch(
        binary=str(tmp_path / "hermes-hey-sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path / "credentials"),
        cursor_state=str(tmp_path / "cursors.json"),
        poll_interval="1s",
    )
    values = []

    with pytest.raises(RuntimeError, match=r"HEY SDK watch failed \(exit 75\)"):
        async for value in watch.lines():
            values.append(value)
            if value.get("type") == "event":
                await watch.ack(value["event"]["event_id"])

    assert [value["type"] for value in values] == ["ready", "event"]
    assert process.stdin.writes == [b'{"ack":"thread:456:entry:900"}\n']
    assert captured["command"] == (
        str(tmp_path / "hermes-hey-sidecar"),
        "watch",
        "--own-email",
        "agent@example.com",
        "--cursor-state",
        str((tmp_path / "cursors.json").resolve()),
        "--poll-interval",
        "1s",
        "--account",
        "12345",
        "--config-dir",
        str((tmp_path / "credentials").resolve()),
    )
    assert captured["kwargs"]["env"]["HEY_NO_KEYRING"] == "1"
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["limit"] == (4 << 20) + 1


@pytest.mark.asyncio
async def test_sdk_watch_assigns_suspended_windows_child_to_job_before_reading(
    monkeypatch, tmp_path
) -> None:
    events: list[str] = []
    process = FakeProcess([b'{"type":"ready","protocol_version":1}\n'], code=75)

    async def fake_create(*_command, **kwargs):
        events.append(f"spawn:{kwargs['creationflags']}")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    watch = HeySDKWatch(
        binary=str(tmp_path / "hermes-hey-sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path / "credentials"),
        cursor_state=str(tmp_path / "cursors.json"),
        _process_tree_factory=lambda: FakeWindowsJob(events),
    )

    with pytest.raises(RuntimeError, match=r"HEY SDK watch failed \(exit 75\)"):
        async for _value in watch.lines():
            events.append("read")

    assert events[:3] == ["spawn:4", "attach", "read"]
    assert "terminate" in events
    assert events[-1] == "close"


@pytest.mark.asyncio
async def test_sdk_watch_closes_windows_job_when_output_is_invalid(
    monkeypatch, tmp_path
) -> None:
    events: list[str] = []
    process = FakeProcess([b"not json\n"], code=0)

    async def fake_create(*_command, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    watch = HeySDKWatch(
        binary=str(tmp_path / "sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path),
        cursor_state=str(tmp_path / "cursor"),
        _process_tree_factory=lambda: FakeWindowsJob(events),
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        async for _value in watch.lines():
            pass

    assert events[-2:] == ["terminate", "close"]


@pytest.mark.asyncio
async def test_sdk_watch_kills_suspended_child_when_job_assignment_fails(
    monkeypatch, tmp_path
) -> None:
    events: list[str] = []
    process = FakeProcess([], code=0)

    async def fake_create(*_command, **_kwargs):
        process.returncode = None
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    watch = HeySDKWatch(
        binary=str(tmp_path / "sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path),
        cursor_state=str(tmp_path / "cursor"),
        _process_tree_factory=lambda: FailingWindowsJob(events),
    )

    with pytest.raises(RuntimeError, match="could not start"):
        async for _value in watch.lines():
            pass

    assert process.killed is True
    assert events == ["attach", "close"]


@pytest.mark.asyncio
async def test_sdk_watch_rejects_non_json_without_echoing_content(
    monkeypatch, tmp_path
) -> None:
    process = FakeProcess([b"private malformed body\n"], code=0)

    async def fake_create(*_command, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    watch = HeySDKWatch(
        binary=str(tmp_path / "hermes-hey-sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path / "credentials"),
        cursor_state=str(tmp_path / "cursors.json"),
    )

    with pytest.raises(RuntimeError, match="invalid JSON") as exc:
        async for _value in watch.lines():
            pass

    assert "private malformed body" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "duplicate",
    ["type", "event", "nested-event-key"],
)
async def test_sdk_watch_rejects_duplicate_keys_before_event_delivery_or_ack(
    duplicate: str, monkeypatch, tmp_path
) -> None:
    payload = {
        "event_id": "thread:456:entry:900",
        "posting_id": 123,
        "thread_id": 456,
        "entry_id": 900,
        "account_id": 12345,
        "sender_id": 77,
        "sender_name": "Synthetic Sender",
        "sender_email": "authorized@example.com",
        "subject": "Synthetic subject",
        "content": "private event body",
        "app_url": "https://app.hey.com/topics/456",
        "created_at": "2026-08-25T16:09:59Z",
        "box_kind": "imbox",
    }
    encoded = json.dumps(payload, separators=(",", ":"))
    if duplicate == "type":
        frame = f'{{"type":"ready","type":"event","event":{encoded}}}'
    elif duplicate == "event":
        frame = f'{{"type":"event","event":{encoded},"event":{encoded}}}'
    else:
        nested = encoded.replace('"account_id":12345', '"account_id":1,"account_id":12345')
        frame = f'{{"type":"event","event":{nested}}}'
    process = FakeProcess(
        [b'{"type":"ready","protocol_version":1}\n', (frame + "\n").encode()],
        code=0,
    )

    async def fake_create(*_command, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    watch = HeySDKWatch(
        binary=str(tmp_path / "sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path),
        cursor_state=str(tmp_path / "cursor"),
    )
    delivered: list[dict[str, Any]] = []
    durable_state = tmp_path / "durable-state.json"

    with pytest.raises(RuntimeError, match="HEY SDK watch returned invalid JSON") as exc:
        async for value in watch.lines():
            if value.get("type") == "event":
                delivered.append(value)
                durable_state.write_text(json.dumps(value))
                await watch.ack(value["event"]["event_id"])

    assert delivered == []
    assert not durable_state.exists()
    assert process.stdin.writes == []
    assert "private event body" not in str(exc.value)


@pytest.mark.asyncio
async def test_sdk_watch_stop_terminates_live_process(tmp_path) -> None:
    process = FakeProcess([], code=0)
    process.returncode = None
    watch = HeySDKWatch(
        binary=str(tmp_path / "hermes-hey-sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path / "credentials"),
        cursor_state=str(tmp_path / "cursors.json"),
    )
    watch.process = cast(Any, process)

    await watch.stop()

    assert process.terminated is True
    assert process.killed is False


@pytest.mark.asyncio
async def test_sdk_watch_stop_terminates_and_closes_windows_job(tmp_path) -> None:
    events: list[str] = []
    process = FakeProcess([], code=0)
    process.returncode = None
    watch = HeySDKWatch(
        binary=str(tmp_path / "sidecar"),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path),
        cursor_state=str(tmp_path / "cursor"),
    )
    watch.process = cast(Any, process)
    watch._process_tree = FakeWindowsJob(events)

    await watch.stop()

    assert events[-2:] == ["terminate", "close"]


@pytest.mark.asyncio
async def test_sdk_watch_lines_do_not_wait_for_descendant_inheriting_pipes(
    tmp_path,
) -> None:
    binary = tmp_path / "watch-with-pipe-descendant"
    ready = tmp_path / "watch-descendant-ready"
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
        "print(json.dumps({'type': 'ready', 'protocol_version': 1}), flush=True)\n"
    )
    binary.chmod(0o700)
    watch = HeySDKWatch(
        binary=str(binary),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path / "credentials"),
        cursor_state=str(tmp_path / "cursors.json"),
    )
    values = []

    async def consume() -> None:
        with pytest.raises(RuntimeError, match="stopped unexpectedly"):
            async for value in watch.lines():
                values.append(value)

    try:
        await asyncio.wait_for(consume(), timeout=3)
    finally:
        await watch.stop()

    assert values == [{"type": "ready", "protocol_version": 1}]
    assert watch._stderr_task is not None
    assert watch._stderr_task.done()


@pytest.mark.asyncio
async def test_sdk_watch_stop_does_not_wait_for_descendant_inheriting_pipes(
    tmp_path,
) -> None:
    binary = tmp_path / "live-watch-with-pipe-descendant"
    ready = tmp_path / "live-watch-descendant-ready"
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
        "print(json.dumps({'type': 'ready', 'protocol_version': 1}), flush=True)\n"
        "time.sleep(6)\n"
    )
    binary.chmod(0o700)
    watch = HeySDKWatch(
        binary=str(binary),
        account="12345",
        own_email="agent@example.com",
        credential_dir=str(tmp_path / "credentials"),
        cursor_state=str(tmp_path / "cursors.json"),
    )
    lines = watch.lines()
    assert await anext(lines) == {"type": "ready", "protocol_version": 1}

    try:
        await asyncio.wait_for(watch.stop(), timeout=3)
    finally:
        watch._terminate_process_group()
        if watch._stderr_task:
            await watch._stderr_task
        await cast(Any, lines).aclose()

    assert watch._stderr_task is not None
    assert watch._stderr_task.done()
