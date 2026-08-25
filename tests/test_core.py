from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hey_platform.core import HeyEvent, event_from_watch, parse_context_id


def sample_watch_event() -> dict:
    return {
        "change": "updated",
        "at": "2026-08-25T16:10:00Z",
        "posting_id": 123,
        "thread_id": 456,
        "new": True,
        "box": {"id": 7, "kind": "imbox", "name": "Imbox"},
        "posting": {
            "id": 123,
            "name": "Please review",
            "summary": "A request",
            "account_id": 12345,
            "active_at": "2026-08-25T16:09:59Z",
            "visible_entry_count": 1,
        },
    }


def sample_thread() -> list[dict]:
    return [
        {
            "id": 900,
            "created_at": "2026-08-25T16:09:59Z",
            "creator": {
                "id": 77,
                "name": "Christopher",
                "email_address": "authorized@example.com",
            },
            "body": "Please review this.",
            "body_state": "hydrated",
        }
    ]


def test_watch_event_uses_active_entry_as_authoritative_sender_and_identity() -> None:
    event = event_from_watch(sample_watch_event(), sample_thread(), own_email="agent@example.com")

    assert event is not None
    assert event == HeyEvent(
        event_id="thread:456:entry:900",
        posting_id=123,
        thread_id=456,
        entry_id=900,
        account_id=12345,
        sender_id=77,
        sender_name="Christopher",
        sender_email="authorized@example.com",
        subject="Please review",
        content="Please review this.",
        app_url="https://app.hey.com/topics/456",
        created_at="2026-08-25T16:09:59Z",
        box_kind="imbox",
    )
    assert event.context_id == "thread:456"
    assert event.timestamp == datetime(2026, 8, 25, 16, 9, 59, tzinfo=UTC)


def test_same_thread_burst_uses_visible_entry_count_not_timestamps() -> None:
    raw = sample_watch_event()
    raw["posting"]["active_at"] = "2026-08-25T16:54:11Z"
    entries = sample_thread()
    entries[0]["created_at"] = "2026-08-25T16:53"
    entries.append(
        {
            "id": 901,
            "created_at": "2026-08-25T16:53",
            "creator": {
                "id": 88,
                "name": "Second sender",
                "email_address": "second@example.com",
            },
            "body": "Second message.",
            "body_state": "hydrated",
        }
    )

    event = event_from_watch(raw, entries, own_email="agent@example.com")

    assert event is not None
    assert event.entry_id == 900
    assert event.content == "Please review this."

    raw["posting"]["visible_entry_count"] = 2
    second = event_from_watch(raw, entries, own_email="agent@example.com")
    assert second is not None
    assert second.entry_id == 901


def test_missing_or_out_of_range_visible_entry_count_fails_closed() -> None:
    raw = sample_watch_event()
    del raw["posting"]["visible_entry_count"]
    assert event_from_watch(raw, sample_thread(), own_email="agent@example.com") is None

    raw["posting"]["visible_entry_count"] = 2
    assert event_from_watch(raw, sample_thread(), own_email="agent@example.com") is None


@pytest.mark.parametrize("value", [1.0, 1.9, True, False, "1", None])
def test_non_integer_visible_entry_count_fails_closed(value: object) -> None:
    raw = sample_watch_event()
    raw["posting"]["visible_entry_count"] = value
    assert event_from_watch(raw, sample_thread(), own_email="agent@example.com") is None


def test_non_new_watch_event_is_ignored() -> None:
    raw = sample_watch_event()
    raw["new"] = False
    assert event_from_watch(raw, sample_thread(), own_email="agent@example.com") is None


def test_own_latest_entry_is_ignored_even_when_watch_marks_it_new() -> None:
    entries = sample_thread()
    entries[-1]["creator"]["email_address"] = "AGENT@EXAMPLE.COM"
    assert event_from_watch(sample_watch_event(), entries, own_email="agent@example.com") is None


def test_partial_or_missing_latest_body_fails_closed() -> None:
    entries = sample_thread()
    entries[-1]["body_state"] = "over_limit"
    assert event_from_watch(sample_watch_event(), entries, own_email="agent@example.com") is None
    assert event_from_watch(sample_watch_event(), [], own_email="agent@example.com") is None


def test_invalid_context_is_rejected() -> None:
    assert parse_context_id("thread:456") == 456
    with pytest.raises(ValueError):
        parse_context_id("mail:456")
