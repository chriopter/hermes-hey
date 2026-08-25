from __future__ import annotations

import asyncio
from typing import cast

import pytest

from hey_platform.adapter import HeyWatch


class FakeProcess:
    def __init__(self, stdout_lines: list[bytes], *, code: int = 0):
        self.stdout = asyncio.StreamReader()
        for line in stdout_lines:
            self.stdout.feed_data(line)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode: int | None = code
        self.terminated = False
        self.killed = False

    async def wait(self):
        return int(self.returncode or 0)

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_watch_parses_ndjson_and_redacts_process_failure(monkeypatch, tmp_path) -> None:
    process = FakeProcess(
        [
            b'{"change":"ready","at":"2026-08-25T16:20:25Z"}\n',
            b'{"change":"added","thread_id":456,"new":true}\n',
        ],
        code=6,
    )
    captured = {}

    async def fake_create(*command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "must-not-propagate")
    watch = HeyWatch(account="12345", config_dir=str(tmp_path))
    values = []

    with pytest.raises(RuntimeError, match=r"HEY watch failed \(exit 6\)"):
        async for value in watch.lines():
            values.append(value)

    assert [value["change"] for value in values] == ["ready", "added"]
    assert captured["command"] == (
        "hey",
        "watch",
        "--events",
        "new",
        "--account",
        "12345",
    )
    assert captured["kwargs"]["env"]["XDG_CONFIG_HOME"] == str(tmp_path.resolve())
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in captured["kwargs"]["env"]


@pytest.mark.asyncio
async def test_watch_rejects_non_json_output_without_echoing_it(monkeypatch) -> None:
    process = FakeProcess([b"private malformed body\n"])

    async def fake_create(*_command, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    watch = HeyWatch(account="12345")

    with pytest.raises(RuntimeError, match="invalid JSON") as exc:
        async for _value in watch.lines():
            pass

    assert "private malformed body" not in str(exc.value)


@pytest.mark.asyncio
async def test_watch_stop_terminates_live_process() -> None:
    process = FakeProcess([])
    process.returncode = None
    watch = HeyWatch(account="12345")
    watch.process = cast(asyncio.subprocess.Process, process)

    await watch.stop()

    assert process.terminated is True
    assert process.killed is False
