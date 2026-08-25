from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hey_platform.core import HeyEvent
from hey_platform.engine import DurableQueue


def event(entry_id: int, sender: str = "authorized@example.com", content: str = "work") -> HeyEvent:
    return HeyEvent(
        event_id=f"thread:456:entry:{entry_id}",
        posting_id=123,
        thread_id=456,
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


def test_unauthorized_content_is_never_persisted(tmp_path: Path) -> None:
    queue = DurableQueue(tmp_path / "state.json")
    item = event(900, sender="outsider@example.com", content="private outsider text")

    assert queue.ingest(item, lambda e: e.sender_email == "authorized@example.com") is False

    assert not (tmp_path / "state.json").exists()


def test_authorized_event_survives_restart_until_exact_delivery_completion(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = DurableQueue(path)
    item = event(900)

    assert first.ingest(item, lambda _e: True) is True
    restarted = DurableQueue(path)
    assert restarted.pending() == [item]

    restarted.complete(item.identity)
    assert DurableQueue(path).pending() == []


def test_duplicate_event_is_dispatched_only_once(tmp_path: Path) -> None:
    queue = DurableQueue(tmp_path / "state.json")
    item = event(900)

    assert queue.ingest(item, lambda _e: True) is True
    assert queue.ingest(item, lambda _e: True) is False
    assert queue.pending() == [item]


def test_completing_old_event_preserves_newly_ingested_event(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    queue = DurableQueue(path)
    old = event(900)
    new = event(901)
    queue.ingest(old, lambda _e: True)
    queue.ingest(new, lambda _e: True)

    queue.complete(old.identity)

    assert queue.pending() == [new]


def test_pending_identity_is_never_evicted_from_seen_window(tmp_path: Path) -> None:
    queue = DurableQueue(tmp_path / "state.json", max_seen=100)
    first = event(900)
    assert queue.ingest(first, lambda _e: True) is True
    for entry_id in range(901, 1001):
        assert queue.ingest(event(entry_id), lambda _e: True) is True

    assert queue.ingest(first, lambda _e: True) is False
    assert [item.identity for item in queue.pending()].count(first.identity) == 1


def test_multiple_queue_instances_serialize_state_updates(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    queues = [DurableQueue(path) for _ in range(4)]

    def ingest(entry_id: int) -> bool:
        return queues[entry_id % len(queues)].ingest(event(entry_id), lambda _e: True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(ingest, range(900, 1000)))

    assert all(accepted)
    assert len(DurableQueue(path).pending()) == 100


def test_corrupt_state_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{broken")
    queue = DurableQueue(path)

    with pytest.raises(RuntimeError, match="HEY state is unreadable"):
        queue.ingest(event(900), lambda _e: True)

    assert path.read_text() == "{broken"
