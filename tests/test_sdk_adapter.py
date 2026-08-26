from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from gateway.config import Platform
from gateway.platforms.base import MessageEvent

from hey_platform.adapter import HeyAdapter
from hey_platform.core import HeyEvent


class Config:
    enabled = True
    extra: ClassVar = {
        "account": "12345",
        "own_email": "agent@example.com",
        "allow_from": ["authorized@example.com"],
        "sidecar_binary": "/synthetic/hermes-hey-sidecar",
    }


class FakeSDKClient:
    def verify(self) -> bool:
        return True

    def reply(self, thread_id: int, text: str) -> dict:
        return {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_result", [False, 1, None, {"ok": True}])
async def test_adapter_connect_requires_verify_to_return_exactly_true(
    verify_result: object, tmp_path: Path, monkeypatch
) -> None:
    from gateway import status

    class AdversarialClient:
        def verify(self):
            return verify_result

        def reply(self, thread_id: int, text: str) -> dict:
            return {"ok": True}

    monkeypatch.setattr(status, "acquire_scoped_lock", lambda *_args: (True, None))
    monkeypatch.setattr(status, "release_scoped_lock", lambda *_args: None)
    instance = HeyAdapter(
        Config(),
        client=cast(Any, AdversarialClient()),
        state_path=tmp_path / "state.json",
        platform=Platform.EMAIL,
    )

    async def no_watch() -> None:
        return None

    instance._watch_supervisor = no_watch

    assert await instance.connect() is False
    assert instance._watch_task is None


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
def test_adapter_rejects_noncanonical_configured_account(
    account: object, tmp_path: Path
) -> None:
    config = type(
        "InvalidAccountConfig",
        (),
        {
            "enabled": True,
            "extra": {
                **Config.extra,
                "account": account,
            },
        },
    )()

    with pytest.raises(ValueError, match="canonical ASCII account"):
        HeyAdapter(
            config,
            client=FakeSDKClient(),
            state_path=tmp_path / "state.json",
            platform=Platform.EMAIL,
        )


def test_adapter_accepts_exact_go_int64_maximum(tmp_path: Path) -> None:
    maximum = "9223372036854775807"
    config = type(
        "MaximumAccountConfig",
        (),
        {"enabled": True, "extra": {**Config.extra, "account": maximum}},
    )()

    instance = HeyAdapter(
        config,
        client=FakeSDKClient(),
        state_path=tmp_path / "state.json",
        platform=Platform.EMAIL,
    )

    assert instance.account == maximum


def event(sender: str = "authorized@example.com") -> HeyEvent:
    return HeyEvent(
        event_id="thread:456:entry:900",
        posting_id=123,
        thread_id=456,
        entry_id=900,
        account_id=12345,
        sender_id=77,
        sender_name="Synthetic Sender",
        sender_email=sender,
        subject="Synthetic request",
        content="<p>Please handle this.</p>",
        app_url="https://app.hey.com/topics/456",
        created_at="2026-08-25T16:10:00Z",
        box_kind="imbox",
    )


class RecordingWatch:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.acks: list[str] = []

    async def lines(self):
        if False:
            yield {}

    async def ack(self, event_id: str) -> None:
        state = json.loads(self.state_path.read_text())
        pending = {item["event_id"] for item in state["pending"]}
        assert event_id in pending
        self.acks.append(event_id)

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_sdk_event_is_durable_before_sidecar_ack(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    instance = HeyAdapter(
        Config(),
        client=FakeSDKClient(),
        state_path=state_path,
        platform=Platform.EMAIL,
    )
    watch = RecordingWatch(state_path)
    instance._watch = watch
    handled: list[MessageEvent] = []

    async def capture(event: MessageEvent) -> None:
        handled.append(event)

    instance.handle_message = capture
    await instance.process_watch_line({"type": "event", "event": event().to_dict()})

    assert watch.acks == [event().identity]
    assert [message.message_id for message in handled] == [event().identity]
    assert [item.identity for item in instance.queue.pending()] == [event().identity]


@pytest.mark.asyncio
async def test_sdk_event_from_other_account_is_not_persisted_or_acked(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    instance = HeyAdapter(
        Config(),
        client=FakeSDKClient(),
        state_path=state_path,
        platform=Platform.EMAIL,
    )
    watch = RecordingWatch(state_path)
    instance._watch = watch
    foreign = event().to_dict()
    foreign["account_id"] = 99999

    with pytest.raises(RuntimeError, match="account does not match"):
        await instance.process_watch_line({"type": "event", "event": foreign})

    assert watch.acks == []
    assert not state_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply_result",
    [{}, {"ok": False}, {"ok": 1}, {"ok": True, "data": {}}, {"ok": True, "extra": None}],
)
async def test_non_exact_reply_response_never_completes_durable_event(
    reply_result: dict, tmp_path: Path
) -> None:
    class AdversarialReplyClient(FakeSDKClient):
        def reply(self, thread_id: int, text: str) -> dict:
            return reply_result

    instance = HeyAdapter(
        Config(),
        client=AdversarialReplyClient(),
        state_path=tmp_path / "state.json",
        platform=Platform.EMAIL,
    )
    pending = event()
    instance.queue.ingest(pending, lambda _event: True)
    assert instance._claim(pending) is True

    result = await instance._send_with_retry("thread:456", "Done", base_delay=0)

    assert result.success is False
    assert result.retryable is False
    assert [item.identity for item in instance.queue.pending()] == [pending.identity]


@pytest.mark.asyncio
async def test_post_ready_unknown_frame_type_fails_closed(tmp_path: Path) -> None:
    instance = HeyAdapter(
        Config(),
        client=FakeSDKClient(),
        state_path=tmp_path / "state.json",
        platform=Platform.EMAIL,
    )

    with pytest.raises(RuntimeError, match="invalid post-ready frame"):
        await instance.process_watch_line({"type": "ack", "ack": event().identity})


@pytest.mark.asyncio
async def test_event_frame_requires_exact_outer_keys(tmp_path: Path) -> None:
    instance = HeyAdapter(
        Config(),
        client=FakeSDKClient(),
        state_path=tmp_path / "state.json",
        platform=Platform.EMAIL,
    )

    with pytest.raises(RuntimeError, match="invalid post-ready frame"):
        await instance.process_watch_line(
            {"type": "event", "event": event().to_dict(), "unexpected": True}
        )
