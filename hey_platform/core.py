"""Pure HEY event and routing primitives."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


def strict_bool(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


@dataclass(slots=True)
class HeyEvent:
    event_id: str
    posting_id: int
    thread_id: int
    entry_id: int
    account_id: int | None
    sender_id: int | None
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
        return cls(
            event_id=str(value["event_id"]),
            posting_id=int(value["posting_id"]),
            thread_id=int(value["thread_id"]),
            entry_id=int(value["entry_id"]),
            account_id=int(value["account_id"]) if value.get("account_id") else None,
            sender_id=int(value["sender_id"]) if value.get("sender_id") else None,
            sender_name=str(value.get("sender_name") or ""),
            sender_email=str(value.get("sender_email") or "").lower(),
            subject=str(value.get("subject") or ""),
            content=str(value.get("content") or ""),
            app_url=str(value.get("app_url") or ""),
            created_at=str(value.get("created_at") or ""),
            box_kind=str(value.get("box_kind") or ""),
        )


def parse_context_id(value: str) -> int:
    match = re.fullmatch(r"thread:(\d+)", value or "")
    if not match:
        raise ValueError(f"Invalid HEY context ID: {value!r}")
    return int(match.group(1))


def event_from_watch(
    raw: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    own_email: str,
) -> HeyEvent | None:
    """Build one authoritative event from a HEY watch line and hydrated thread."""
    if raw.get("new") is not True or raw.get("change") not in {"added", "updated"}:
        return None
    if not entries:
        return None
    posting_raw = raw.get("posting")
    posting: dict[str, Any] = posting_raw if isinstance(posting_raw, dict) else {}
    visible_entry_count = posting.get("visible_entry_count")
    if type(visible_entry_count) is not int:
        return None
    if visible_entry_count < 1 or visible_entry_count > len(entries):
        return None
    active = entries[visible_entry_count - 1]
    if str(active.get("body_state") or "") not in {"hydrated", "bodyless"}:
        return None
    creator_raw = active.get("creator")
    creator: dict[str, Any] = creator_raw if isinstance(creator_raw, dict) else {}
    sender_email = str(creator.get("email_address") or "").strip().lower()
    if not sender_email or sender_email == own_email.strip().lower():
        return None
    posting_raw = raw.get("posting")
    posting: dict[str, Any] = posting_raw if isinstance(posting_raw, dict) else {}
    box_raw = raw.get("box")
    box: dict[str, Any] = box_raw if isinstance(box_raw, dict) else {}
    try:
        posting_id = int(raw["posting_id"])
        thread_id = int(raw["thread_id"])
        entry_id = int(active["id"])
    except (KeyError, TypeError, ValueError):
        return None
    body = str(active.get("body") or active.get("summary") or "").strip()
    if not body:
        return None
    return HeyEvent(
        event_id=f"thread:{thread_id}:entry:{entry_id}",
        posting_id=posting_id,
        thread_id=thread_id,
        entry_id=entry_id,
        account_id=int(posting["account_id"]) if posting.get("account_id") else None,
        sender_id=int(creator["id"]) if creator.get("id") else None,
        sender_name=str(creator.get("name") or ""),
        sender_email=sender_email,
        subject=str(posting.get("name") or active.get("summary") or ""),
        content=body,
        app_url=f"https://app.hey.com/topics/{thread_id}",
        created_at=str(active.get("created_at") or raw.get("at") or ""),
        box_kind=str(box.get("kind") or ""),
    )
