"""Native Hermes gateway adapter for HEY email."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
from collections.abc import AsyncIterator
from contextvars import ContextVar
from pathlib import Path
from time import monotonic
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

from .client import HeySDKClient, HeySDKWatch, canonical_account, make_sidecar_runner
from .core import (
    HeyEvent,
    parse_context_id,
    parse_event_frame,
    parse_fatal_frame,
    parse_ready_frame,
    strict_bool,
)
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
    def verify(self) -> bool: ...
    def reply(self, thread_id: int, text: str) -> dict[str, Any]: ...
    def comment(self, thread_id: int, text: str) -> dict[str, Any]: ...


class HeyWatcher(Protocol):
    def lines(self) -> AsyncIterator[dict[str, Any]]: ...
    async def ack(self, event_id: str) -> None: ...
    async def stop(self) -> None: ...


class HeyAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False
    WATCH_STABLE_SECONDS = 30.0
    GATEWAY_READY_POLL_SECONDS = 0.05
    _claim_lock = threading.RLock()
    _claims: ClassVar[dict[str, dict[int, str]]] = {}
    _live_adapters: ClassVar[dict[str, HeyAdapter]] = {}

    def __init__(
        self,
        config,
        *,
        client: HeyClient | None = None,
        state_path: Path | None = None,
        platform: Platform | None = None,
    ):
        super().__init__(config=config, platform=platform or Platform.EMAIL)
        extra = getattr(config, "extra", {}) or {}
        extra.setdefault("group_sessions_per_user", False)
        self.account = canonical_account(extra.get("account"))
        self.account_id = int(self.account)
        self.own_email = str(extra.get("own_email") or "").strip().lower()
        self.config_dir = str(extra.get("config_dir") or "~/.config")
        config_root = Path(self.config_dir).expanduser().resolve()
        self.credential_dir = str(
            Path(extra.get("credential_dir") or config_root / "hey-cli")
            .expanduser()
            .resolve()
        )
        self.sidecar_binary = str(
            extra.get("sidecar_binary")
            or shutil.which("hermes-hey-sidecar")
            or "hermes-hey-sidecar"
        )
        self.poll_interval = str(extra.get("poll_interval") or "1s")
        configured = extra.get("allow_from") or []
        if isinstance(configured, str):
            configured = configured.split(",")
        self.allowed_senders = {
            str(value).strip().lower() for value in configured if str(value).strip()
        }
        self.allow_all_users = strict_bool(extra.get("allow_all_users", False))
        self._dm_policy = "allowlist"
        self.failure_threshold = max(
            1, min(100, int(extra.get("watch_failure_threshold", 5)))
        )
        resolved_state_path = state_path
        if resolved_state_path is None:
            from hermes_constants import get_hermes_home

            resolved_state_path = get_hermes_home() / "state" / "hey-platform.json"
        resolved_state_path = Path(resolved_state_path)
        self.cursor_state_path = resolved_state_path.with_name("hey-sdk-cursors.json")
        selected_client = client
        if selected_client is None:
            runner = make_sidecar_runner(
                binary=self.sidecar_binary,
                account=self.account,
                credential_dir=self.credential_dir,
            )
            selected_client = HeySDKClient(
                runner, account=self.account, own_email=self.own_email
            )
        self.client = selected_client
        self.queue = DurableQueue(resolved_state_path)
        self._state_key = str(resolved_state_path.resolve())
        self._inflight: set[str] = set()
        self._delivery_context: ContextVar[tuple[str, list[str]] | None] = ContextVar(
            "hey_delivery_context", default=None
        )
        self._final_send_allowed: ContextVar[bool] = ContextVar(
            "hey_final_send_allowed", default=False
        )
        self._watch_task: asyncio.Task | None = None
        self._gateway_ready_task: asyncio.Task | None = None
        self._watch: HeyWatcher | None = None
        self._lock_acquired = False
        self._credential_key = self.credential_dir

    @property
    def name(self) -> str:
        return "HEY"

    @property
    def enforces_own_access_policy(self) -> bool:
        return True

    def _is_dm_allowed(self, user_id: str) -> bool:
        sender = str(user_id or "").strip().lower()
        return bool(sender) and sender != self.own_email and (
            self.allow_all_users or sender in self.allowed_senders
        )

    def _authorized(self, event: HeyEvent) -> bool:
        sender = event.sender_email.lower()
        if not sender or sender == self.own_email:
            return False
        return self._is_dm_allowed(sender)

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

    def _claimed_event(self, thread_id: int) -> HeyEvent | None:
        identity = self._claimed_identity(thread_id)
        if identity is None:
            return None
        return next(
            (
                event
                for event in self.queue.pending()
                if event.identity == identity and event.thread_id == thread_id
            ),
            None,
        )

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
            verify = getattr(self.client, "verify", None)
            if not callable(verify):
                raise TypeError("HEY SDK client cannot verify identity and protocol")
            verification = await run_blocking(verify)
            if verification is not True:
                raise RuntimeError("HEY SDK client verification did not return true")
            self._register_live_adapter()
            await self._drain_pending()
            self._gateway_ready_task = asyncio.create_task(
                self._drain_when_gateway_ready()
            )
            self._watch_task = asyncio.create_task(self._watch_supervisor())
        except Exception as exc:  # noqa: BLE001
            self._unregister_live_adapter()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            self._release_lock()
            return False
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        if self._gateway_ready_task and not self._gateway_ready_task.done():
            self._gateway_ready_task.cancel()
            try:
                await self._gateway_ready_task
            except asyncio.CancelledError:
                pass
        self._gateway_ready_task = None
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
            started_at = monotonic()
            watch = HeySDKWatch(
                binary=self.sidecar_binary,
                account=self.account,
                own_email=self.own_email,
                credential_dir=self.credential_dir,
                cursor_state=str(self.cursor_state_path),
                poll_interval=self.poll_interval,
            )
            self._watch = watch
            retry_delay: int | None = None
            ready_seen = False
            try:
                async for raw in watch.lines():
                    if not ready_seen:
                        try:
                            fatal = parse_fatal_frame(raw)
                            if fatal is not None:
                                raise ValueError(
                                    f"HEY SDK watch reported a fatal error: {fatal}"
                                )
                            parse_ready_frame(raw, HeySDKClient.PROTOCOL_VERSION)
                        except ValueError as exc:
                            raise RuntimeError(str(exc)) from None
                        ready_seen = True
                        await self._drain_pending()
                        continue
                    await self.process_watch_line(raw)
                raise RuntimeError("HEY SDK watch ended")
            except asyncio.CancelledError:
                raise
            except Exception:
                if monotonic() - started_at >= self.WATCH_STABLE_SECONDS:
                    failures = 0
                failures += 1
                logger.exception("HEY SDK watch failed; reconnecting")
                if failures >= self.failure_threshold:
                    self._set_fatal_error(
                        "watch_failed", "HEY SDK watch failed repeatedly", retryable=True
                    )
                    await self._notify_fatal_error()
                    return
                retry_delay = min(60, 2 ** (failures - 1))
            finally:
                try:
                    await asyncio.shield(watch.stop())
                except Exception:
                    logger.exception("HEY SDK watch cleanup failed")
                if self._watch is watch:
                    self._watch = None
            if retry_delay is not None:
                await asyncio.sleep(retry_delay)

    async def process_watch_line(self, raw: dict[str, Any]) -> None:
        try:
            fatal = parse_fatal_frame(raw)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        if fatal is not None:
            raise RuntimeError(f"HEY SDK watch reported a fatal error: {fatal}")
        try:
            event = parse_event_frame(raw)
        except (KeyError, TypeError, ValueError) as exc:
            if "invalid post-ready frame" in str(exc):
                raise RuntimeError(str(exc)) from None
            raise RuntimeError("HEY SDK watch returned an invalid event") from None
        if event.account_id != self.account_id:
            raise RuntimeError("HEY SDK watch event account does not match configuration")
        accepted = self.queue.ingest(event, self._authorized)
        if self._watch is None:
            raise RuntimeError("HEY SDK watch acknowledgement channel is unavailable")
        await self._watch.ack(event.identity)
        if accepted:
            await self._drain_pending()

    async def _drain_pending(self) -> None:
        for event in self.queue.pending():
            if event.account_id != self.account_id:
                raise RuntimeError(
                    "HEY pending event account does not match configuration"
                )
            if not self._authorized(event):
                self.queue.complete(event.identity)
                self._inflight.discard(event.identity)
                continue
            gateway_authorized = self._is_sender_authorized(
                event.sender_email, "dm", event.context_id
            )
            if gateway_authorized is False:
                continue
            if not self._claim(event):
                continue
            try:
                await self._dispatch(event)
            except Exception:
                self._release_claim(event.identity)
                raise

    async def _drain_when_gateway_ready(self) -> None:
        probe_senders = sorted(
            sender for sender in self.allowed_senders if self._is_dm_allowed(sender)
        )
        if probe_senders:
            probe_sender = probe_senders[0]
        elif self.allow_all_users:
            probe_sender = "hermes-hey-readiness-probe@invalid"
        else:
            return
        while True:
            gateway_authorized = self._is_sender_authorized(
                probe_sender, "dm", "thread:gateway-readiness"
            )
            if gateway_authorized is None:
                return
            if gateway_authorized is True:
                await self._drain_pending()
                return
            await asyncio.sleep(self.GATEWAY_READY_POLL_SECONDS)

    async def _dispatch(self, event: HeyEvent) -> None:
        source = self.build_source(
            chat_id=event.context_id,
            chat_name=f"HEY: {event.subject}",
            chat_type="dm",
            user_id=event.sender_email,
            user_name=event.sender_name,
            scope_id=self.account,
            message_id=event.identity,
        )
        payload = {
            "thread_id": event.thread_id,
            "posting_id": event.posting_id,
            "entry_id": event.entry_id,
            "entry_kind": event.kind,
            "account_id": event.account_id,
            "app_url": event.app_url,
            "delivery_ids": [event.identity],
        }
        label = "HEY Collab note" if event.kind == "comment" else "HEY email"
        text = f"{label}: {event.subject}\n\n{event.content}\n\nHEY event metadata:\n"
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
        return bool(match and int(match.group(1)) == 75)

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Any = None,
        max_retries: int = 3,
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
        event = self._claimed_event(thread_id)
        if event is None:
            return SendResult(
                success=False,
                error="HEY final response has no claimed pending event",
                retryable=False,
            )
        mutation = self.client.comment if event.kind == "comment" else self.client.reply
        mutation_name = "comment" if event.kind == "comment" else "reply"
        try:
            result = await run_blocking(mutation, thread_id, content)
        except RuntimeError as exc:
            error = str(exc)
            return SendResult(
                success=False, error=error, retryable=self._retryable(error)
            )
        except Exception:
            logger.exception("Unexpected HEY %s failure", mutation_name)
            return SendResult(
                success=False, error=f"HEY {mutation_name} failed", retryable=False
            )
        if (
            type(result) is not dict
            or set(result) != {"ok"}
            or result.get("ok") is not True
        ):
            return SendResult(
                success=False,
                error=f"HEY SDK {mutation_name} returned invalid response",
                retryable=False,
            )
        return SendResult(success=True, raw_response=result)

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
