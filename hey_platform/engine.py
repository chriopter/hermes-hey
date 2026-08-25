"""Durable exactly-once queue for authorized HEY events."""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from .core import HeyEvent

Authorize = Callable[[HeyEvent], bool]


class DurableQueue:
    _locks_guard: ClassVar = threading.Lock()
    _locks: ClassVar[dict[str, threading.RLock]] = {}

    def __init__(self, path: Path, max_seen: int = 10_000):
        self.path = Path(path)
        self.max_seen = max(100, int(max_seen))
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"seen": [], "pending": []}
        except OSError:
            raise RuntimeError("HEY state is unreadable; refusing overwrite") from None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("HEY state is unreadable; refusing overwrite") from None
        if not isinstance(value, dict) or not isinstance(value.get("seen", []), list) or not isinstance(value.get("pending", []), list):
            raise RuntimeError("HEY state is unreadable; refusing overwrite")  # noqa: TRY004
        if any(not isinstance(item, dict) for item in value.get("pending", [])):
            raise RuntimeError("HEY state is unreadable; refusing overwrite")
        value.setdefault("seen", [])
        value.setdefault("pending", [])
        return value

    @staticmethod
    def _trim(values: list[str], cap: int) -> list[str]:
        result: list[str] = []
        known: set[str] = set()
        for value in reversed(values):
            if value in known:
                continue
            known.add(value)
            result.append(value)
        result.reverse()
        return result[-cap:]

    def _save(self, *, seen: list[str], pending: list[dict[str, Any]]) -> None:
        pending_ids = [HeyEvent.from_dict(item).identity for item in pending]
        pending_set = set(pending_ids)
        completed_seen = [identity for identity in seen if identity not in pending_set]
        durable_seen = self._trim(completed_seen, self.max_seen)
        durable_seen.extend(self._trim(pending_ids, len(pending_ids)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"seen": durable_seen, "pending": pending},
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def ingest(self, event: HeyEvent, authorize: Authorize) -> bool:
        if not authorize(event):
            return False
        with self._lock:
            value = self._load()
            seen = [str(item) for item in value["seen"]]
            if event.identity in seen:
                return False
            seen.append(event.identity)
            pending = list(value["pending"])
            pending.append(event.to_dict())
            self._save(seen=seen, pending=pending)
            return True

    def pending(self) -> list[HeyEvent]:
        with self._lock:
            return [HeyEvent.from_dict(item) for item in self._load()["pending"]]

    def complete(self, identity: str) -> None:
        with self._lock:
            value = self._load()
            pending = [
                item
                for item in value["pending"]
                if HeyEvent.from_dict(item).identity != identity
            ]
            self._save(
                seen=[str(item) for item in value["seen"]], pending=pending
            )
