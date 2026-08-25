"""Native Hermes gateway adapter for HEY email."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from collections.abc import AsyncIterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any, ClassVar, Protocol

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import build_session_key

from .client import HeyCLI, make_subprocess_runner
from .core import HeyEvent, parse_context_id, strict_bool
from .engine import DurableQueue

logger = logging.getLogger(__name__)


async def run_blocking(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, lambda: func(*args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        try:
            await future
        except Exception as exc:  # noqa: BLE001
            logger.debug("HEY blocking worker failed during cancellation: %s", exc)
        raise


class HeyClient(Protocol):
    def verify_version(self) -> bool: ...
    def hydrate_event(self, raw: dict[str, Any]) -> HeyEvent | None: ...
    def reply(self, thread_id: int, text: str) -> dict[str, Any]: ...


class HeyWatch:
    """One official `hey watch` process, exposed as parsed NDJSON."""

    def __init__(self, *, account: str, config_dir: str | None = None):
        self.account = str(account)
        self.config_dir = config_dir
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None

    def _env(self) -> dict[str, str]:
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
            if key in os.environ
        }
        if self.config_dir:
            env["XDG_CONFIG_HOME"] = str(Path(self.config_dir).expanduser().resolve())
        return env

    async def _discard_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        while await self.process.stderr.read(4096):
            pass

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        self.process = await asyncio.create_subprocess_exec(
            "hey",
            "watch",
            "--events",
            "new",
            "--account",
            self.account,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                raise RuntimeError("HEY watch returned invalid JSON") from None
            if isinstance(value, dict):
                yield value
        code = await self.process.wait()
        if self._stderr_task:
            await self._stderr_task
        if code != 0:
            raise RuntimeError(f"HEY watch failed (exit {code})")
        raise RuntimeError("HEY watch stopped unexpectedly")

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._stderr_task and not self._stderr_task.done():
            await self._stderr_task


class HeyAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False
    _claim_lock = threading.RLock()
    _claims: ClassVar[dict[str, dict[int, str]]] = {}
    _live_adapters: ClassVar[dict[str, HeyAdapter]] = {}

    def __init__(
        self,
        config,
        *,
        cli: HeyClient | None = None,
        state_path: Path | None = None,
        platform: Platform | None = None,
    ):
        super().__init__(config=config, platform=platform or Platform("hey"))
        extra = getattr(config, "extra", {}) or {}
        extra.setdefault("group_sessions_per_user", False)
        self.account = str(extra.get("account") or "").strip()
        self.own_email = str(extra.get("own_email") or "").strip().lower()
        self.config_dir = str(extra.get("config_dir") or "~/.config")
        configured = extra.get("allow_from") or []
        if isinstance(configured, str):
            configured = configured.split(",")
        self.allowed_senders = {
            str(value).strip().lower() for value in configured if str(value).strip()
        }
        self.allow_all_users = strict_bool(extra.get("allow_all_users", False))
        self.failure_threshold = max(
            1, min(100, int(extra.get("watch_failure_threshold", 5)))
        )
        if cli is None:
            runner = make_subprocess_runner(
                account=self.account or None, config_dir=self.config_dir
            )
            cli = HeyCLI(runner, account=self.account, own_email=self.own_email)
        self.cli = cli
        resolved_state_path = state_path
        if resolved_state_path is None:
            from hermes_constants import get_hermes_home

            resolved_state_path = get_hermes_home() / "state" / "hey-platform.json"
        self.queue = DurableQueue(Path(resolved_state_path))
        self._state_key = str(Path(resolved_state_path).resolve())
        self._inflight: set[str] = set()
        self._delivery_context: ContextVar[tuple[str, list[str]] | None] = ContextVar(
            "hey_delivery_context", default=None
        )
        self._final_send_allowed: ContextVar[bool] = ContextVar(
            "hey_final_send_allowed", default=False
        )
        self._watch_task: asyncio.Task | None = None
        self._watch: HeyWatch | None = None
        self._lock_acquired = False
        self._credential_key = str(Path(self.config_dir).expanduser().resolve())

    @property
    def name(self) -> str:
        return "HEY"

    def _authorized(self, event: HeyEvent) -> bool:
        return self.allow_all_users or event.sender_email.lower() in self.allowed_senders

    def _claim(self, event: HeyEvent) -> bool:
        with self._claim_lock:
            claims = self._claims.setdefault(self._state_key, {})
            if event.thread_id in claims:
                return False
            claims[event.thread_id] = event.identity
            self._inflight.add(event.identity)
            return True

    def _claimed_identity(self, thread_id: int) -> str | None:
        with self._claim_lock:
            return self._claims.get(self._state_key, {}).get(thread_id)

    def _release_claim(self, identity: str) -> None:
        with self._claim_lock:
            claims = self._claims.get(self._state_key, {})
            for thread_id, claimed in list(claims.items()):
                if claimed == identity:
                    del claims[thread_id]
            if not claims:
                self._claims.pop(self._state_key, None)
        self._inflight.discard(identity)

    def _register_live_adapter(self) -> None:
        with self._claim_lock:
            self._live_adapters[self._state_key] = self

    def _unregister_live_adapter(self) -> None:
        with self._claim_lock:
            if self._live_adapters.get(self._state_key) is self:
                self._live_adapters.pop(self._state_key, None)

    def _live_adapter(self) -> HeyAdapter | None:
        with self._claim_lock:
            return self._live_adapters.get(self._state_key)

    def _release_lock(self) -> None:
        if not self._lock_acquired:
            return
        try:
            from gateway.status import release_scoped_lock

            release_scoped_lock("hey", self._credential_key)
        except ImportError:
            pass
        self._lock_acquired = False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.account or not self.own_email:
            self._set_fatal_error(
                "config_missing", "HEY requires extra.account and extra.own_email", retryable=False
            )
            return False
        try:
            from gateway.status import acquire_scoped_lock

            result = acquire_scoped_lock("hey", self._credential_key)
            acquired = bool(result[0]) if isinstance(result, tuple) else bool(result)
            if not acquired:
                self._set_fatal_error(
                    "lock_conflict",
                    "HEY credential context is already used by another profile",
                    retryable=False,
                )
                return False
            self._lock_acquired = True
        except ImportError:
            pass
        try:
            verify_version = getattr(self.cli, "verify_version", None)
            if not callable(verify_version):
                raise TypeError("HEY client cannot verify CLI version")
            await run_blocking(verify_version)
            verify_identity = getattr(self.cli, "verify_identity", None)
            if not callable(verify_identity):
                raise TypeError("HEY client cannot verify authenticated identity")
            await run_blocking(verify_identity)
            self._register_live_adapter()
            await self._drain_pending()
            self._watch_task = asyncio.create_task(self._watch_supervisor())
        except Exception as exc:  # noqa: BLE001
            self._unregister_live_adapter()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            self._release_lock()
            return False
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            if self._watch:
                await self._watch.stop()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        self._watch_task = None
        self._watch = None
        self._unregister_live_adapter()
        self._release_lock()
        self._mark_disconnected()

    async def _watch_supervisor(self) -> None:
        failures = 0
        while True:
            self._watch = HeyWatch(account=self.account, config_dir=self.config_dir)
            try:
                async for raw in self._watch.lines():
                    if raw.get("change") == "ready":
                        failures = 0
                        continue
                    if raw.get("change") == "disconnected":
                        continue
                    await self.process_watch_line(raw)
                raise RuntimeError("HEY watch ended")
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.exception("HEY watch failed; reconnecting")
                if failures >= self.failure_threshold:
                    self._set_fatal_error(
                        "watch_failed", "HEY watch failed repeatedly", retryable=True
                    )
                    await self._notify_fatal_error()
                    return
                await asyncio.sleep(min(60, 2 ** (failures - 1)))

    async def process_watch_line(self, raw: dict[str, Any]) -> None:
        event = await run_blocking(self.cli.hydrate_event, raw)
        if event is None:
            return
        accepted = self.queue.ingest(event, self._authorized)
        if accepted:
            await self._drain_pending()

    async def _drain_pending(self) -> None:
        for event in self.queue.pending():
            if not self._authorized(event):
                self.queue.complete(event.identity)
                self._inflight.discard(event.identity)
                continue
            if not self._claim(event):
                continue
            try:
                await self._dispatch(event)
            except Exception:
                self._release_claim(event.identity)
                raise

    async def _dispatch(self, event: HeyEvent) -> None:
        source = self.build_source(
            chat_id=event.context_id,
            chat_name=f"HEY: {event.subject}",
            chat_type="dm",
            user_id=event.sender_email,
            user_name=event.sender_name,
            scope_id=str(event.account_id) if event.account_id else self.account,
            message_id=event.identity,
        )
        payload = {
            "thread_id": event.thread_id,
            "posting_id": event.posting_id,
            "entry_id": event.entry_id,
            "account_id": event.account_id,
            "app_url": event.app_url,
            "delivery_ids": [event.identity],
        }
        text = f"HEY email: {event.subject}\n\n{event.content}\n\nHEY event metadata:\n"
        text += json.dumps(payload, ensure_ascii=False, sort_keys=True)
        message = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=event.sender_email,
            user_name=event.sender_name,
            source=source,
            raw_message=event.to_dict(),
            message_id=event.identity,
            timestamp=event.timestamp,
            metadata={"hey": payload},
            allow_gateway_control=False,
        )
        await self.handle_message(message)

    async def on_processing_start(self, event: MessageEvent) -> None:
        ids = ((event.metadata or {}).get("hey") or {}).get("delivery_ids", [])
        if ids:
            self._delivery_context.set((event.source.chat_id, list(ids)))

    async def _drain_when_session_idle(self, event: MessageEvent) -> None:
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            profile=self._session_key_profile(event.source),
        )
        while session_key in self._active_sessions:
            await asyncio.sleep(0.01)
        await self._drain_pending()

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        ids = list(((event.metadata or {}).get("hey") or {}).get("delivery_ids", []))
        self._delivery_context.set(None)
        completed = False
        pending_ids = {item.identity for item in self.queue.pending()}
        for identity in ids:
            self._release_claim(identity)
            completed = completed or identity not in pending_ids
        target = self._live_adapter()
        if target is not None and (completed or target is not self):
            task = asyncio.create_task(target._drain_when_session_idle(event))
            target._background_tasks.add(task)
            task.add_done_callback(target._background_tasks.discard)

    @staticmethod
    def _retryable(error: str) -> bool:
        match = re.search(r"exit\s+(\d+)", error.lower())
        return bool(match and int(match.group(1)) in {5, 6, 7})

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Any = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> SendResult:
        """Send only the final text response; acknowledge only its confirmed reply."""
        thread_id = parse_context_id(chat_id)
        token = self._final_send_allowed.set(True)
        try:
            result = await self.send(chat_id, content, reply_to=reply_to, metadata=metadata)
            for attempt in range(max_retries):
                if result.success or not result.retryable:
                    break
                await asyncio.sleep(base_delay * (2**attempt))
                result = await self.send(
                    chat_id, content, reply_to=reply_to, metadata=metadata
                )
        finally:
            self._final_send_allowed.reset(token)
        if result.success:
            identity = self._claimed_identity(thread_id)
            if identity:
                self.queue.complete(identity)
        return result

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not self._final_send_allowed.get():
            return SendResult(
                success=False,
                error="HEY permits only the final text response",
                retryable=False,
            )
        thread_id = parse_context_id(chat_id)
        try:
            result = await run_blocking(self.cli.reply, thread_id, content)
        except RuntimeError as exc:
            error = str(exc)
            return SendResult(
                success=False, error=error, retryable=self._retryable(error)
            )
        except Exception:
            logger.exception("Unexpected HEY reply failure")
            return SendResult(success=False, error="HEY reply failed", retryable=False)
        data = result.get("data") if isinstance(result, dict) else None
        message_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
        return SendResult(success=True, message_id=message_id, raw_response=result)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        return SendResult(
            success=False,
            error="HEY email replies cannot be edited after sending",
            retryable=False,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"chat_id": chat_id, "name": f"HEY thread {parse_context_id(chat_id)}", "type": "dm"}
