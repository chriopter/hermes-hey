from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hey_platform.core import HeyEvent, parse_context_id, parse_ready_frame


def event_dict() -> dict:
    return {
        "event_id": "thread:456:entry:900",
        "kind": "message",
        "posting_id": 123,
        "thread_id": 456,
        "entry_id": 900,
        "account_id": 12345,
        "sender_id": 77,
        "sender_name": "Synthetic Sender",
        "sender_email": "sender@example.com",
        "subject": "Synthetic subject",
        "content": "Synthetic body",
        "app_url": "https://app.hey.com/topics/456",
        "created_at": "2026-08-25T16:09:59Z",
        "box_kind": "imbox",
    }


def test_sidecar_event_round_trips_exactly() -> None:
    event = HeyEvent.from_dict(event_dict())

    assert event.identity == "thread:456:entry:900"
    assert event.context_id == "thread:456"
    assert event.sender_email == "sender@example.com"
    assert event.timestamp == datetime(2026, 8, 25, 16, 9, 59, tzinfo=UTC)
    assert event.to_dict() == event_dict()
    assert HeyEvent.from_dict(event.to_dict()) == event


@pytest.mark.parametrize("kind", ["message", "comment"])
def test_sidecar_event_accepts_only_supported_entry_kinds(kind: str) -> None:
    value = event_dict()
    value["kind"] = kind

    assert HeyEvent.from_dict(value).kind == kind


@pytest.mark.parametrize("kind", ["", "note", "Comment", 1, None])
def test_sidecar_event_rejects_unsupported_entry_kinds(kind: object) -> None:
    value = event_dict()
    value["kind"] = kind

    with pytest.raises((TypeError, ValueError), match="kind"):
        HeyEvent.from_dict(value)


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_sidecar_event_requires_exact_payload_keys(mutation: str) -> None:
    value = event_dict()
    if mutation == "extra":
        value["unexpected"] = "value"
    else:
        del value["subject"]

    with pytest.raises(ValueError, match="exact keys"):
        HeyEvent.from_dict(value)


def test_sidecar_event_identity_must_match_thread_and_entry() -> None:
    value = event_dict()
    value["event_id"] = "thread:999:entry:900"

    with pytest.raises(ValueError, match="identity does not match"):
        HeyEvent.from_dict(value)


def test_sidecar_event_app_url_must_match_thread_context() -> None:
    value = event_dict()
    value["app_url"] = "https://app.hey.com/topics/999"

    with pytest.raises(ValueError, match="app_url does not match"):
        HeyEvent.from_dict(value)


@pytest.mark.parametrize("sender_id", [None, True, "77", 77.0, 0, -1])
def test_sidecar_event_sender_id_must_be_an_exact_positive_integer(sender_id: object) -> None:
    value = event_dict()
    value["sender_id"] = sender_id

    with pytest.raises(ValueError, match="sender_id must be a positive integer"):
        HeyEvent.from_dict(value)


@pytest.mark.parametrize(
    "field",
    [
        "event_id",
        "kind",
        "sender_name",
        "sender_email",
        "subject",
        "content",
        "app_url",
        "created_at",
        "box_kind",
    ],
)
def test_sidecar_event_string_fields_do_not_coerce(field: str) -> None:
    value = event_dict()
    value[field] = 123

    with pytest.raises(TypeError, match=f"{field} must be a string"):
        HeyEvent.from_dict(value)


def test_ready_frame_is_exact() -> None:
    parse_ready_frame({"type": "ready", "protocol_version": 1}, 1)

    with pytest.raises(ValueError, match="exact ready frame"):
        parse_ready_frame(
            {"type": "ready", "protocol_version": 1, "unexpected": True}, 1
        )


def test_invalid_context_is_rejected() -> None:
    assert parse_context_id("thread:456") == 456
    with pytest.raises(ValueError):
        parse_context_id("mail:456")


@pytest.mark.parametrize("context_id", ["thread:0", "thread:01", "thread:١"])
def test_context_id_must_use_canonical_positive_ascii_digits(context_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid HEY context ID"):
        parse_context_id(context_id)
