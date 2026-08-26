"""Pure HEY event and routing primitives."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

_EVENT_KEYS = {
    "event_id",
    "posting_id",
    "thread_id",
    "entry_id",
    "account_id",
    "sender_id",
    "sender_name",
    "sender_email",
    "subject",
    "content",
    "app_url",
    "created_at",
    "box_kind",
}


def strict_bool(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"HEY {name} must be a positive integer")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"HEY {name} must be a string")
    return value


@dataclass(slots=True)
class HeyEvent:
    event_id: str
    posting_id: int
    thread_id: int
    entry_id: int
    account_id: int
    sender_id: int
    sender_name: str
    sender_email: str
    subject: str
    content: str
    app_url: str
    created_at: str
    box_kind: str

    @property
    def identity(self) -> str:
        return self.event_id

    @property
    def context_id(self) -> str:
        return f"thread:{self.thread_id}"

    @property
    def timestamp(self) -> datetime:
        try:
            parsed = datetime.fromisoformat(self.created_at)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HeyEvent:
        if set(value) != _EVENT_KEYS:
            raise ValueError("HEY event payload must contain exact keys")
        event_id = _string(value["event_id"], "event_id")
        thread_id = _positive_int(value["thread_id"], "thread_id")
        entry_id = _positive_int(value["entry_id"], "entry_id")
        if event_id != f"thread:{thread_id}:entry:{entry_id}":
            raise ValueError("HEY event identity does not match thread and entry")
        app_url = _string(value["app_url"], "app_url")
        if app_url != f"https://app.hey.com/topics/{thread_id}":
            raise ValueError("HEY event app_url does not match thread context")
        return cls(
            event_id=event_id,
            posting_id=_positive_int(value["posting_id"], "posting_id"),
            thread_id=thread_id,
            entry_id=entry_id,
            account_id=_positive_int(value["account_id"], "account_id"),
            sender_id=_positive_int(value["sender_id"], "sender_id"),
            sender_name=_string(value["sender_name"], "sender_name"),
            sender_email=_string(value["sender_email"], "sender_email"),
            subject=_string(value["subject"], "subject"),
            content=_string(value["content"], "content"),
            app_url=app_url,
            created_at=_string(value["created_at"], "created_at"),
            box_kind=_string(value["box_kind"], "box_kind"),
        )


def parse_ready_frame(value: dict[str, Any], protocol_version: int) -> None:
    if set(value) != {"type", "protocol_version"}:
        raise ValueError("HEY SDK watch must send an exact ready frame")
    if value["type"] != "ready":
        raise ValueError("HEY SDK watch must start with ready")
    actual = value["protocol_version"]
    if type(actual) is not int or actual != protocol_version:
        raise ValueError("HEY SDK watch protocol mismatch")


def parse_fatal_frame(value: dict[str, Any]) -> str | None:
    """Return the sidecar's error text if *value* is a fatal frame, else None.

    The sidecar closes a watch by emitting ``{"type": "fatal", "error": ...}``
    (see sidecar/app.go). Recognising it here keeps the real cause in the log
    instead of reporting the generic invalid-frame error.
    """
    if value.get("type") != "fatal":
        return None
    if set(value) != {"type", "error"}:
        raise ValueError("HEY SDK watch sent a malformed fatal frame")
    error = value["error"]
    if type(error) is not str or not error.strip():
        raise ValueError("HEY SDK watch sent a malformed fatal frame")
    return error.strip()


def parse_event_frame(value: dict[str, Any]) -> HeyEvent:
    if set(value) != {"type", "event"} or value.get("type") != "event":
        raise ValueError("HEY SDK watch returned an invalid post-ready frame")
    payload = value["event"]
    if not isinstance(payload, dict):
        raise TypeError("HEY SDK watch returned an invalid event payload")
    event = HeyEvent.from_dict(payload)
    if event.to_dict() != payload:
        raise ValueError("HEY SDK watch event payload did not round-trip exactly")
    return event


def parse_context_id(value: str) -> int:
    match = re.fullmatch(r"thread:([1-9][0-9]*)", value or "")
    if not match:
        raise ValueError(f"Invalid HEY context ID: {value!r}")
    return int(match.group(1))
