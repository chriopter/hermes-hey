"""Official HEY SDK sidecar transport for the Hermes platform adapter."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

PayloadRunner = Callable[[list[str], dict[str, Any] | None], dict[str, Any]]
SDK_VERSION = "0.24.0"
_MAX_SIDECAR_OUTPUT = 1_048_576
_MAX_WATCH_LINE = (4 << 20) + 1
_MAX_ACCOUNT = "9223372036854775807"


class _DuplicateJSONKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONKey
        value[key] = item
    return value


def _decode_sidecar_json(value: str | bytes | bytearray) -> Any:
    return json.loads(value, object_pairs_hook=_reject_duplicate_keys)


def canonical_account(value: object) -> str:
    """Return an already-canonical ASCII account identifier."""
    if (
        type(value) is not str
        or re.fullmatch(r"[1-9][0-9]*", value) is None
        or len(value) > len(_MAX_ACCOUNT)
        or (len(value) == len(_MAX_ACCOUNT) and value > _MAX_ACCOUNT)
    ):
        raise ValueError("HEY account must be a canonical ASCII account identifier")
    return value


class _ProcessTree(Protocol):
    creation_flags: int

    def attach(self, process: Any) -> None: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class _NoopProcessTree:
    creation_flags = 0

    def attach(self, process: Any) -> None:
        del process

    def terminate(self) -> None:
        pass

    def close(self) -> None:
        pass


class _WindowsJob:  # pragma: no cover - Windows only
    """Kill-on-close Job Object used as a Windows process-tree boundary."""

    creation_flags = 0x00000004  # CREATE_SUSPENDED

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        win_dll = ctypes.__dict__["WinDLL"]
        self._win_error = ctypes.__dict__["WinError"]
        self._get_last_error = ctypes.__dict__["get_last_error"]
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ThreadEntry),
        ]
        kernel32.Thread32FirstW.restype = wintypes.BOOL
        kernel32.Thread32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ThreadEntry),
        ]
        kernel32.Thread32NextW.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise self._win_error(self._get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = self._get_last_error()
            kernel32.CloseHandle(job)
            raise self._win_error(error)
        self._kernel32 = kernel32
        self._job = job
        self._thread_entry_type = ThreadEntry
        self._thread_entry_size = ctypes.sizeof(ThreadEntry)
        self._byref = ctypes.byref
        self._invalid_handle = ctypes.c_void_p(-1).value

    @staticmethod
    def _popen(process: Any) -> Any:
        transport = getattr(process, "_transport", None)
        if transport is not None:
            native = transport.get_extra_info("subprocess")
            if native is not None:
                return native
        return process

    def attach(self, process: Any) -> None:
        native = self._popen(process)
        if not self._kernel32.AssignProcessToJobObject(self._job, native._handle):
            raise self._win_error(self._get_last_error())
        snapshot = self._kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot == self._invalid_handle:
            raise self._win_error(self._get_last_error())
        try:
            entry = self._thread_entry_type()
            entry.dwSize = self._thread_entry_size
            found = self._kernel32.Thread32FirstW(snapshot, self._byref(entry))
            while found:
                if entry.th32OwnerProcessID == native.pid:
                    thread = self._kernel32.OpenThread(
                        0x0002, False, entry.th32ThreadID
                    )
                    if not thread:
                        raise self._win_error(self._get_last_error())
                    try:
                        if self._kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise self._win_error(self._get_last_error())
                    finally:
                        self._kernel32.CloseHandle(thread)
                    return
                found = self._kernel32.Thread32NextW(snapshot, self._byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        raise OSError("suspended Windows process thread was not found")

    def terminate(self) -> None:
        if self._job:
            self._kernel32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        if self._job:
            self._kernel32.CloseHandle(self._job)
            self._job = None


def _new_process_tree() -> _ProcessTree:
    return _WindowsJob() if os.name == "nt" else _NoopProcessTree()


def make_sidecar_runner(
    *,
    binary: str,
    account: str,
    credential_dir: str,
    _process_tree_factory: Callable[[], _ProcessTree] = _new_process_tree,
) -> PayloadRunner:
    binary_path = str(Path(binary).expanduser())
    account = canonical_account(account)
    credentials = str(Path(credential_dir).expanduser().resolve())
    base_env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
        if key in os.environ
    }
    base_env["HEY_NO_KEYRING"] = "1"

    def run(
        args: list[str], payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        operation = args[0] if args else "operation"
        command = [
            binary_path,
            *args,
            "--account",
            account,
            "--config-dir",
            credentials,
        ]
        stdin = json.dumps(payload, ensure_ascii=False) + "\n" if payload is not None else None
        stdin_file = None
        process_tree: _ProcessTree | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            process_tree = _process_tree_factory()
            stdin_file = tempfile.TemporaryFile()  # noqa: SIM115 - spans child lifetime
            if stdin is not None:
                stdin_file.write(stdin.encode("utf-8"))
                stdin_file.seek(0)
            process = subprocess.Popen(
                command,
                stdin=stdin_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=base_env,
                start_new_session=os.name == "posix",
                creationflags=process_tree.creation_flags,
            )
            process_tree.attach(process)
        except OSError:
            if process is not None:
                process.kill()
                process.wait()
            if stdin_file is not None:
                stdin_file.close()
            if process_tree is not None:
                process_tree.close()
            raise RuntimeError(f"HEY SDK {operation} could not start") from None
        output = bytearray()
        oversized = threading.Event()

        def terminate(*, force: bool = False) -> None:
            process_tree.terminate()
            if os.name == "posix":
                try:
                    os.killpg(
                        process.pid,
                        signal.SIGKILL if force else signal.SIGTERM,
                    )
                except ProcessLookupError:
                    pass
            if os.name != "posix" and process.poll() is None:
                if force:
                    process.kill()
                else:
                    process.terminate()

        def read_stdout() -> None:
            assert process.stdout is not None
            read_chunk = getattr(process.stdout, "read1", process.stdout.read)
            while chunk := read_chunk(64 << 10):
                remaining = _MAX_SIDECAR_OUTPUT + 1 - len(output)
                output.extend(chunk[:remaining])
                if len(output) > _MAX_SIDECAR_OUTPUT:
                    oversized.set()
                    return

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 120
            while process.poll() is None and not oversized.is_set():
                if time.monotonic() >= deadline:
                    terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        terminate(force=True)
                        process.wait()
                    raise RuntimeError(f"HEY SDK {operation} timed out")
                oversized.wait(0.01)
            if oversized.is_set() and process.poll() is None:
                terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    terminate(force=True)
                    process.wait()
        finally:
            reader.join(timeout=0.1)
            if reader.is_alive():
                terminate()
                reader.join(timeout=0.5)
            if reader.is_alive():
                terminate(force=True)
                reader.join(timeout=5)
            stdin_file.close()
            process_tree.close()
        if oversized.is_set():
            raise RuntimeError(f"HEY SDK {operation} returned oversized JSON")
        if process.returncode != 0:
            raise RuntimeError(
                f"HEY SDK {operation} failed (exit {process.returncode})"
            )
        try:
            result = _decode_sidecar_json(output)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKey):
            raise RuntimeError(f"HEY SDK {operation} returned invalid JSON") from None
        if not isinstance(result, dict):
            raise TypeError(f"HEY SDK {operation} returned invalid JSON")
        return result

    return run


class HeySDKWatch:
    """Bidirectional NDJSON watch process backed by the official HEY SDK."""

    def __init__(
        self,
        *,
        binary: str,
        account: str,
        own_email: str,
        credential_dir: str,
        cursor_state: str,
        poll_interval: str = "1s",
        _process_tree_factory: Callable[[], _ProcessTree] = _new_process_tree,
    ):
        self.binary = str(Path(binary).expanduser())
        self.account = canonical_account(account)
        self.own_email = own_email.strip().lower()
        self.credential_dir = str(Path(credential_dir).expanduser().resolve())
        self.cursor_state = str(Path(cursor_state).expanduser().resolve())
        self.poll_interval = poll_interval
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._process_tree_factory = _process_tree_factory
        self._process_tree: _ProcessTree | None = None

    @staticmethod
    def _env() -> dict[str, str]:
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
            if key in os.environ
        }
        env["HEY_NO_KEYRING"] = "1"
        return env

    async def _discard_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        while await self.process.stderr.read(4096):
            pass

    def _terminate_process_group(self, *, force: bool = False) -> None:
        if not self.process:
            return
        if self._process_tree:
            self._process_tree.terminate()
        pid = getattr(self.process, "pid", None)
        if os.name == "posix" and pid is not None:
            try:
                os.killpg(
                    pid,
                    signal.SIGKILL if force else signal.SIGTERM,
                )
            except ProcessLookupError:
                pass
        elif self.process.returncode is None:
            if force:
                self.process.kill()
            else:
                self.process.terminate()

    def _close_process_tree(self) -> None:
        if self._process_tree:
            self._process_tree.terminate()
            self._process_tree.close()
            self._process_tree = None

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        command = (
            self.binary,
            "watch",
            "--own-email",
            self.own_email,
            "--cursor-state",
            self.cursor_state,
            "--poll-interval",
            self.poll_interval,
            "--account",
            self.account,
            "--config-dir",
            self.credential_dir,
        )
        try:
            self._process_tree = self._process_tree_factory()
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                limit=_MAX_WATCH_LINE,
                start_new_session=os.name == "posix",
                creationflags=self._process_tree.creation_flags,
            )
            self._process_tree.attach(self.process)
        except OSError:
            if self.process is not None and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            if self._process_tree:
                self._process_tree.close()
                self._process_tree = None
            raise RuntimeError("HEY SDK watch could not start") from None
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        assert self.process.stdout is not None

        async def wait_for_direct_exit() -> int:
            while self.process and self.process.returncode is None:
                await asyncio.sleep(0.01)
            assert self.process is not None
            assert self.process.returncode is not None
            return self.process.returncode

        process_wait = asyncio.create_task(wait_for_direct_exit())
        try:
            while True:
                read_line = asyncio.create_task(self.process.stdout.readline())
                done, _pending = await asyncio.wait(
                    (read_line, process_wait),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if process_wait in done and read_line not in done:
                    self._terminate_process_group()
                    try:
                        line = await asyncio.wait_for(
                            asyncio.shield(read_line), timeout=0.5
                        )
                    except TimeoutError:
                        self._terminate_process_group(force=True)
                        try:
                            line = await asyncio.wait_for(read_line, timeout=5)
                        except TimeoutError:
                            read_line.cancel()
                            await asyncio.gather(read_line, return_exceptions=True)
                            line = b""
                else:
                    line = await read_line
                if not line:
                    break
                try:
                    value = _decode_sidecar_json(line)
                except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKey):
                    self._close_process_tree()
                    raise RuntimeError("HEY SDK watch returned invalid JSON") from None
                if not isinstance(value, dict):
                    self._close_process_tree()
                    raise TypeError("HEY SDK watch returned invalid JSON")
                yield value
        except ValueError:
            self._close_process_tree()
            raise RuntimeError("HEY SDK watch returned oversized JSON") from None
        code = await process_wait
        if self._stderr_task and not self._stderr_task.done():
            self._terminate_process_group()
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.5)
            except TimeoutError:
                self._terminate_process_group(force=True)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stderr_task), timeout=5
                    )
                except TimeoutError:
                    self._stderr_task.cancel()
                    await asyncio.gather(self._stderr_task, return_exceptions=True)
        if code != 0:
            self._close_process_tree()
            raise RuntimeError(f"HEY SDK watch failed (exit {code})")
        self._close_process_tree()
        raise RuntimeError("HEY SDK watch stopped unexpectedly")

    async def ack(self, event_id: str) -> None:
        if not re.fullmatch(r"thread:[1-9][0-9]*:entry:[1-9][0-9]*", event_id):
            raise RuntimeError("HEY SDK watch event identity is invalid")
        if not self.process or not self.process.stdin:
            raise RuntimeError("HEY SDK watch is not running")
        frame = json.dumps({"ack": event_id}, separators=(",", ":")) + "\n"
        self.process.stdin.write(frame.encode("utf-8"))
        await self.process.stdin.drain()

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self._terminate_process_group()

            async def wait_for_direct_exit() -> None:
                while self.process and self.process.returncode is None:
                    await asyncio.sleep(0.01)

            try:
                await asyncio.wait_for(wait_for_direct_exit(), timeout=0.5)
            except TimeoutError:
                self._terminate_process_group(force=True)
                try:
                    await asyncio.wait_for(wait_for_direct_exit(), timeout=5)
                except TimeoutError:
                    pass
        if self._stderr_task and not self._stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.5)
            except TimeoutError:
                self._terminate_process_group(force=True)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stderr_task), timeout=5
                    )
                except TimeoutError:
                    self._stderr_task.cancel()
                    await asyncio.gather(self._stderr_task, return_exceptions=True)
        self._close_process_tree()


class HeySDKClient:
    """Synchronous command client for the official-SDK sidecar."""

    PROTOCOL_VERSION = 2

    def __init__(self, runner: PayloadRunner, *, account: str, own_email: str):
        self.runner = runner
        self.account = canonical_account(account)
        self.account_id = int(self.account)
        self.own_email = own_email.strip().lower()

    def verify(self) -> bool:
        result = self.runner(["verify", "--own-email", self.own_email], None)
        expected_keys = {
            "ok",
            "protocol_version",
            "sdk_version",
            "account",
            "email",
        }
        sdk_version = result.get("sdk_version")
        account = result.get("account")
        email = result.get("email")
        if (
            set(result) != expected_keys
            or result.get("ok") is not True
            or type(result.get("protocol_version")) is not int
            or result["protocol_version"] != self.PROTOCOL_VERSION
            or sdk_version != SDK_VERSION
            or type(account) is not int
            or account != self.account_id
            or type(email) is not str
            or email != self.own_email
        ):
            raise RuntimeError("HEY SDK sidecar verification failed")
        return True

    def reply(self, thread_id: int, text: str) -> dict[str, Any]:
        result = self.runner(
            ["reply", "--thread-id", str(thread_id)], {"content": text}
        )
        if set(result) != {"ok"} or result.get("ok") is not True:
            raise RuntimeError("HEY SDK reply failed")
        return result

    def comment(self, thread_id: int, text: str) -> dict[str, Any]:
        result = self.runner(
            ["comment", "--thread-id", str(thread_id)], {"content": text}
        )
        if set(result) != {"ok"} or result.get("ok") is not True:
            raise RuntimeError("HEY SDK comment failed")
        return result
