from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, ProcessingOutcome

from hey_platform.adapter import HeyAdapter
from hey_platform.core import HeyEvent


class Config:
    enabled = True
    extra: ClassVar = {
        "account": "12345",
        "own_email": "agent@example.com",
        "allow_from": ["authorized@example.com"],
    }


class FakeCLI:
    def __init__(self, event: HeyEvent | None):
        self.event = event
        self.replies: list[tuple[int, str]] = []
        self.fail_reply = False
        self.identity_matches = True

    def verify_identity(self):
        if not self.identity_matches:
            raise RuntimeError("HEY authenticated identity does not match configuration")
        return True

    def verify_version(self):
        return True

    def hydrate_event(self, raw):
        return self.event

    def reply(self, thread_id: int, text: str):
        self.replies.append((thread_id, text))
        if self.fail_reply:
            raise RuntimeError("hey reply failed (exit 6)")
        return {"ok": True, "data": {"id": 991}}


def event(
    sender="authorized@example.com",
    content="please act",
    *,
    entry_id: int = 900,
    thread_id: int = 456,
) -> HeyEvent:
    return HeyEvent(
        event_id=f"thread:{thread_id}:entry:{entry_id}",
        posting_id=123,
        thread_id=thread_id,
        entry_id=entry_id,
        account_id=12345,
        sender_id=77,
        sender_name="Christopher",
        sender_email=sender,
        subject="Request",
        content=content,
        app_url="https://app.hey.com/topics/456",
        created_at="2026-08-25T16:10:00Z",
        box_kind="imbox",
    )


def adapter(tmp_path: Path, cli: FakeCLI) -> HeyAdapter:
    return HeyAdapter(
        Config(),
        cli=cli,
        state_path=tmp_path / "state.json",
        platform=Platform.EMAIL,
    )


@pytest.mark.asyncio
async def test_disconnected_old_adapter_does_not_chain_before_replacement_exists(
    tmp_path: Path,
) -> None:
    old_cli = FakeCLI(event())
    old = adapter(tmp_path, old_cli)
    first = event(entry_id=900)
    second = event(entry_id=901)
    old.queue.ingest(first, lambda _event: True)
    old.queue.ingest(second, lambda _event: True)
    started = asyncio.Event()
    release = asyncio.Event()
    old_events: list[str] = []

    async def old_handler(message: MessageEvent):
        old_events.append(str(message.message_id))
        started.set()
        await release.wait()
        return "First final response"

    old.set_message_handler(old_handler)
    old._register_live_adapter()
    await old._drain_pending()
    await asyncio.wait_for(started.wait(), timeout=1)
    old._unregister_live_adapter()
    release.set()

    for _ in range(100):
        if len(old_cli.replies) == 1:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)

    assert old_events == [first.identity]
    assert old_cli.replies == [(456, "First final response")]
    assert [item.identity for item in old.queue.pending()] == [second.identity]


def test_email_platform_disables_progressive_message_editing(tmp_path: Path) -> None:
    instance = adapter(tmp_path, FakeCLI(event()))
    assert instance.SUPPORTS_MESSAGE_EDITING is False


@pytest.mark.asyncio
async def test_connect_fails_closed_when_cli_identity_mismatches(tmp_path: Path, monkeypatch) -> None:
    from gateway import status

    monkeypatch.setattr(status, "acquire_scoped_lock", lambda *_args: True)
    monkeypatch.setattr(status, "release_scoped_lock", lambda *_args: None)
    cli = FakeCLI(event())
    cli.identity_matches = False
    instance = adapter(tmp_path, cli)

    assert await instance.connect() is False
    assert instance._watch_task is None


@pytest.mark.asyncio
async def test_ready_watch_handshake_resets_consecutive_failure_budget(
    tmp_path: Path, monkeypatch
) -> None:
    import hey_platform.adapter as adapter_module

    class SequenceWatch:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        async def lines(self):
            SequenceWatch.calls += 1
            if SequenceWatch.calls == 1:
                if False:
                    yield {}
                raise RuntimeError("first disconnect")
            if SequenceWatch.calls == 2:
                yield {"change": "ready"}
                raise RuntimeError("later disconnect")
            raise asyncio.CancelledError
            yield {}

    async def no_sleep(_seconds):
        return None

    fatal_notified = False

    async def capture_fatal():
        nonlocal fatal_notified
        fatal_notified = True

    instance = adapter(tmp_path, FakeCLI(event()))
    instance.failure_threshold = 2
    instance._notify_fatal_error = capture_fatal
    monkeypatch.setattr(adapter_module, "HeyWatch", SequenceWatch)
    monkeypatch.setattr(adapter_module.asyncio, "sleep", no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await instance._watch_supervisor()

    assert fatal_notified is False
    assert SequenceWatch.calls == 3


@pytest.mark.asyncio
async def test_drain_dispatches_only_one_pending_event_per_thread(tmp_path: Path) -> None:
    instance = adapter(tmp_path, FakeCLI(event()))
    first = event(entry_id=900)
    second = event(entry_id=901)
    instance.queue.ingest(first, lambda _event: True)
    instance.queue.ingest(second, lambda _event: True)
    handled: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        handled.append(event)

    instance.handle_message = capture
    await instance._drain_pending()

    assert [message.message_id for message in handled] == [first.identity]


@pytest.mark.asyncio
async def test_completed_event_releases_thread_and_dispatches_next(tmp_path: Path) -> None:
    instance = adapter(tmp_path, FakeCLI(event()))
    first = event(entry_id=900)
    second = event(entry_id=901)
    instance.queue.ingest(first, lambda _event: True)
    instance.queue.ingest(second, lambda _event: True)
    handled: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        handled.append(event)

    instance.handle_message = capture
    instance._register_live_adapter()
    await instance._drain_pending()
    assert [message.message_id for message in handled] == [first.identity]
    assert (await instance._send_with_retry("thread:456", "Done")).success is True
    await instance.on_processing_complete(handled[0], ProcessingOutcome.SUCCESS)
    await asyncio.sleep(0.02)

    assert [message.message_id for message in handled] == [first.identity, second.identity]


@pytest.mark.asyncio
async def test_replacement_adapter_does_not_redrain_claimed_event(tmp_path: Path) -> None:
    first_adapter = adapter(tmp_path, FakeCLI(event()))
    replacement = adapter(tmp_path, FakeCLI(event()))
    first_adapter.queue.ingest(event(), lambda _event: True)
    first_handled: list[MessageEvent] = []
    replacement_handled: list[MessageEvent] = []

    async def capture_first(event: MessageEvent):
        first_handled.append(event)

    async def capture_replacement(event: MessageEvent):
        replacement_handled.append(event)

    first_adapter.handle_message = capture_first
    replacement.handle_message = capture_replacement
    await first_adapter._drain_pending()
    await replacement._drain_pending()

    assert [message.message_id for message in first_handled] == [event().identity]
    assert replacement_handled == []


@pytest.mark.asyncio
async def test_failed_old_adapter_hands_pending_retry_to_live_replacement(tmp_path: Path) -> None:
    old = adapter(tmp_path, FakeCLI(event()))
    replacement = adapter(tmp_path, FakeCLI(event()))
    pending = event()
    old.queue.ingest(pending, lambda _event: True)
    assert old._claim(pending) is True
    captured: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        captured.append(event)

    old.handle_message = capture
    await old._dispatch(pending)
    replacement_drains = 0

    async def capture_replacement_drain():
        nonlocal replacement_drains
        replacement_drains += 1

    replacement._drain_pending = capture_replacement_drain
    replacement._register_live_adapter()

    await old.on_processing_complete(captured[0], ProcessingOutcome.FAILURE)
    await asyncio.sleep(0.02)

    assert replacement_drains == 1
    assert [item.identity for item in replacement.queue.pending()] == [pending.identity]


@pytest.mark.asyncio
async def test_successful_old_adapter_hands_followup_drain_to_live_replacement(tmp_path: Path) -> None:
    old = adapter(tmp_path, FakeCLI(event()))
    replacement = adapter(tmp_path, FakeCLI(event()))
    first = event(entry_id=900)
    second = event(entry_id=901)
    old.queue.ingest(first, lambda _event: True)
    old.queue.ingest(second, lambda _event: True)
    assert old._claim(first) is True
    captured: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        captured.append(event)

    old.handle_message = capture
    await old._dispatch(first)
    old.queue.complete(first.identity)
    old_drains = 0
    replacement_drains = 0

    async def capture_old_drain():
        nonlocal old_drains
        old_drains += 1

    async def capture_replacement_drain():
        nonlocal replacement_drains
        replacement_drains += 1

    old._drain_pending = capture_old_drain
    replacement._drain_pending = capture_replacement_drain
    replacement._register_live_adapter()

    await old.on_processing_complete(captured[0], ProcessingOutcome.SUCCESS)
    await asyncio.sleep(0.02)

    assert old_drains == 0
    assert replacement_drains == 1


@pytest.mark.asyncio
async def test_real_background_lifecycle_hands_successful_chain_to_replacement(
    tmp_path: Path,
) -> None:
    old_cli = FakeCLI(event())
    replacement_cli = FakeCLI(event())
    old = adapter(tmp_path, old_cli)
    replacement = adapter(tmp_path, replacement_cli)
    first = event(entry_id=900)
    second = event(entry_id=901)
    old.queue.ingest(first, lambda _event: True)
    old.queue.ingest(second, lambda _event: True)
    started = asyncio.Event()
    release = asyncio.Event()
    old_events: list[str] = []
    replacement_events: list[str] = []

    async def old_handler(message: MessageEvent):
        old_events.append(str(message.message_id))
        started.set()
        await release.wait()
        return "First final response"

    async def replacement_handler(message: MessageEvent):
        replacement_events.append(str(message.message_id))
        return "Second final response"

    class Runner:
        def _adapter_for_source(self, _source):
            return replacement

    old.set_message_handler(old_handler)
    replacement.set_message_handler(replacement_handler)
    old.gateway_runner = cast(Any, Runner())
    old._register_live_adapter()
    await old._drain_pending()
    await asyncio.wait_for(started.wait(), timeout=1)
    replacement._register_live_adapter()
    await replacement._drain_pending()
    release.set()

    async def completed() -> bool:
        return not replacement.queue.pending() and len(replacement_cli.replies) == 2

    for _ in range(100):
        if await completed():
            break
        await asyncio.sleep(0.01)

    assert old_events == [first.identity]
    assert replacement_events == [second.identity]
    assert old_cli.replies == []
    assert [text for _thread, text in replacement_cli.replies] == [
        "First final response",
        "Second final response",
    ]
    assert replacement.queue.pending() == []


@pytest.mark.asyncio
async def test_real_background_failure_retries_on_replacement(tmp_path: Path) -> None:
    old_cli = FakeCLI(event())
    replacement_cli = FakeCLI(event())
    old = adapter(tmp_path, old_cli)
    replacement = adapter(tmp_path, replacement_cli)
    pending = event()
    old.queue.ingest(pending, lambda _event: True)
    started = asyncio.Event()
    release = asyncio.Event()
    replacement_events: list[str] = []

    async def old_handler(_message: MessageEvent):
        started.set()
        await release.wait()
        raise RuntimeError("synthetic handler failure")

    async def replacement_handler(message: MessageEvent):
        replacement_events.append(str(message.message_id))
        return "Recovered final response"

    old.set_message_handler(old_handler)
    replacement.set_message_handler(replacement_handler)
    old._register_live_adapter()
    await old._drain_pending()
    await asyncio.wait_for(started.wait(), timeout=1)
    replacement._register_live_adapter()
    await replacement._drain_pending()
    release.set()

    for _ in range(100):
        if not replacement.queue.pending() and replacement_cli.replies:
            break
        await asyncio.sleep(0.01)

    assert replacement_events == [pending.identity]
    assert old_cli.replies == []
    assert replacement_cli.replies == [(456, "Recovered final response")]
    assert replacement.queue.pending() == []


@pytest.mark.asyncio
async def test_real_background_cancellation_retries_on_replacement(tmp_path: Path) -> None:
    old_cli = FakeCLI(event())
    replacement_cli = FakeCLI(event())
    old = adapter(tmp_path, old_cli)
    replacement = adapter(tmp_path, replacement_cli)
    pending = event()
    old.queue.ingest(pending, lambda _event: True)
    started = asyncio.Event()
    replacement_events: list[str] = []

    async def old_handler(_message: MessageEvent):
        started.set()
        await asyncio.Event().wait()

    async def replacement_handler(message: MessageEvent):
        replacement_events.append(str(message.message_id))
        return "Recovered after cancellation"

    old.set_message_handler(old_handler)
    replacement.set_message_handler(replacement_handler)
    old._register_live_adapter()
    await old._drain_pending()
    await asyncio.wait_for(started.wait(), timeout=1)
    replacement._register_live_adapter()
    await replacement._drain_pending()
    old_task = next(iter(old._session_tasks.values()))
    old_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_task

    for _ in range(100):
        if not replacement.queue.pending() and replacement_cli.replies:
            break
        await asyncio.sleep(0.01)

    assert replacement_events == [pending.identity]
    assert old_cli.replies == []
    assert replacement_cli.replies == [(456, "Recovered after cancellation")]
    assert replacement.queue.pending() == []


@pytest.mark.asyncio
async def test_authorized_email_routes_one_session_per_thread_and_stays_pending(tmp_path: Path) -> None:
    instance = adapter(tmp_path, FakeCLI(event()))
    handled = []

    async def capture(event: MessageEvent):
        handled.append(event)

    instance.handle_message = capture
    await instance.process_watch_line({"change": "added"})

    assert len(handled) == 1
    message = handled[0]
    assert message.source.chat_id == "thread:456"
    assert message.source.user_id == "authorized@example.com"
    assert message.text.startswith("HEY email: Request")
    assert [item.identity for item in instance.queue.pending()] == [event().identity]


@pytest.mark.asyncio
async def test_unauthorized_email_is_not_persisted_or_dispatched(tmp_path: Path) -> None:
    outsider = event(sender="outsider@example.com", content="private outsider content")
    instance = adapter(tmp_path, FakeCLI(outsider))
    handled = []

    async def capture(event: MessageEvent):
        handled.append(event)

    instance.handle_message = capture
    await instance.process_watch_line({"change": "added"})

    assert handled == []
    assert instance.queue.pending() == []
    assert not (tmp_path / "state.json").exists()


@pytest.mark.asyncio
async def test_direct_send_never_completes_pending_event(tmp_path: Path) -> None:
    cli = FakeCLI(event())
    instance = adapter(tmp_path, cli)
    instance.queue.ingest(event(), lambda _event: True)
    instance._delivery_context.set(("thread:456", [event().identity]))

    result = await instance.send("thread:456", "Done")

    assert result.success is False
    assert cli.replies == []
    assert [item.identity for item in instance.queue.pending()] == [event().identity]


@pytest.mark.asyncio
async def test_confirmed_final_retry_path_completes_exact_pending_event(tmp_path: Path) -> None:
    cli = FakeCLI(event())
    instance = adapter(tmp_path, cli)
    instance.queue.ingest(event(), lambda _event: True)
    assert instance._claim(event()) is True

    result = await instance._send_with_retry("thread:456", "Done")

    assert result.success is True
    assert cli.replies == [(456, "Done")]
    assert instance.queue.pending() == []


@pytest.mark.asyncio
async def test_failed_reply_keeps_pending_event(tmp_path: Path) -> None:
    cli = FakeCLI(event())
    cli.fail_reply = True
    instance = adapter(tmp_path, cli)
    instance.queue.ingest(event(), lambda _event: True)
    assert instance._claim(event()) is True

    result = await instance._send_with_retry("thread:456", "Done", base_delay=0)

    assert result.success is False
    assert cli.replies == [(456, "Done"), (456, "Done"), (456, "Done")]
    assert [item.identity for item in instance.queue.pending()] == [event().identity]


@pytest.mark.asyncio
async def test_media_fallback_cannot_send_a_second_email(tmp_path: Path) -> None:
    cli = FakeCLI(event())
    instance = adapter(tmp_path, cli)
    instance.queue.ingest(event(), lambda _event: True)
    assert instance._claim(event()) is True

    assert (await instance._send_with_retry("thread:456", "Final answer")).success
    media_result = await instance.send_image(
        "thread:456", "https://example.com/image.png", caption="Image"
    )

    assert media_result.success is False
    assert cli.replies == [(456, "Final answer")]


@pytest.mark.asyncio
async def test_replacement_adapter_can_ack_old_adapters_shared_claim(tmp_path: Path) -> None:
    old = adapter(tmp_path, FakeCLI(event()))
    replacement_cli = FakeCLI(event())
    replacement = adapter(tmp_path, replacement_cli)
    old.queue.ingest(event(), lambda _event: True)
    assert old._claim(event()) is True

    result = await replacement._send_with_retry("thread:456", "Final answer")

    assert result.success is True
    assert replacement.queue.pending() == []
    assert replacement_cli.replies == [(456, "Final answer")]


@pytest.mark.asyncio
async def test_processing_success_without_delivery_does_not_ack_queue(tmp_path: Path) -> None:
    instance = adapter(tmp_path, FakeCLI(event()))
    instance.queue.ingest(event(), lambda _event: True)
    instance._inflight.add(event().identity)
    message = cast(
        MessageEvent,
        type("Message", (), {"metadata": {"hey": {"delivery_ids": [event().identity]}}})(),
    )

    await instance.on_processing_complete(message, ProcessingOutcome.SUCCESS)

    assert [item.identity for item in instance.queue.pending()] == [event().identity]
    assert event().identity not in instance._inflight
