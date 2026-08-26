"""Durable exactly-once queue for authorized HEY events."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar, Protocol

from .core import HeyEvent

Authorize = Callable[[HeyEvent], bool]


class _WindowsStateTransaction(Protocol):
    def read_state(self, limit: int) -> bytes | None: ...

    def replace_state(self, encoded: bytes) -> None: ...


@contextmanager
def _windows_state_transaction(path: Path, *, create: bool):  # pragma: no cover - Windows
    try:
        from .windows_state import WindowsStateTransaction

        with WindowsStateTransaction(path, create=create) as transaction:
            yield transaction
    except (ImportError, OSError) as exc:
        raise RuntimeError("Windows secure state APIs are unavailable") from exc

_WINDOWS_SYSTEM_SID = "S-1-5-18"
_WINDOWS_ADMINISTRATORS_SID = "S-1-5-32-544"
_EVENT_IDENTITY = re.compile(r"thread:[1-9][0-9]*:entry:[1-9][0-9]*\Z")
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


def _parse_persisted_event(value: object) -> HeyEvent:
    if not isinstance(value, dict) or set(value) != _EVENT_KEYS:
        raise ValueError("invalid persisted HEY event")
    parsed = HeyEvent.from_dict(value)
    if parsed.to_dict() != value:
        raise ValueError("persisted HEY event requires coercion")
    return parsed


def _windows_acl_is_private(
    *, owner_sid: str, current_sid: str, allowed_sids: set[str]
) -> bool:
    private_sids = {current_sid, _WINDOWS_SYSTEM_SID, _WINDOWS_ADMINISTRATORS_SID}
    return owner_sid == current_sid and current_sid in allowed_sids and allowed_sids <= private_sids


def _windows_metadata_is_secure(metadata: Any, *, directory: bool) -> bool:
    reparse_point = 0x400
    attributes = int(getattr(metadata, "st_file_attributes", reparse_point))
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    return not attributes & reparse_point and expected_type(metadata.st_mode)


def _windows_path_is_secure(
    path: Path,
    *,
    directory: bool,
    metadata_reader: Callable[[Path], Any],
    acl_reader: Callable[[Path], tuple[str, str, set[str]]],
) -> bool:
    try:
        metadata = metadata_reader(path)
        owner_sid, current_sid, allowed_sids = acl_reader(path)
    except (OSError, RuntimeError):
        return False
    return _windows_metadata_is_secure(
        metadata, directory=directory
    ) and _windows_acl_is_private(
        owner_sid=owner_sid,
        current_sid=current_sid,
        allowed_sids=allowed_sids,
    )


def _windows_ace_kind(ace_type: int) -> str:
    if ace_type == 0:
        return "allow"
    if ace_type == 1:
        return "deny"
    raise OSError(f"unsupported Windows ACE type: {ace_type}")


def _windows_apis(ctypes: Any) -> tuple[Any, Any]:  # pragma: no cover - Windows only
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Windows security APIs are unavailable")
    advapi32 = win_dll("advapi32", use_last_error=True)
    kernel32 = win_dll("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    advapi32.GetTokenInformation.restype = ctypes.c_int
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_ulong
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = ctypes.c_int
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = ctypes.c_int
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    advapi32.SetFileSecurityW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    advapi32.SetFileSecurityW.restype = ctypes.c_int
    return advapi32, kernel32


def _windows_sid_string(  # pragma: no cover - Windows only
    sid: Any, advapi32: Any, kernel32: Any, ctypes: Any
) -> str:
    value = ctypes.c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        raise OSError(ctypes.get_last_error(), "cannot convert Windows SID")
    try:
        if value.value is None:
            raise OSError("Windows SID is empty")
        return value.value
    finally:
        kernel32.LocalFree(ctypes.cast(value, ctypes.c_void_p))


def _windows_current_sid(  # pragma: no cover - Windows only
    advapi32: Any, kernel32: Any, ctypes: Any
) -> str:
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "cannot open Windows process token")
    try:
        size = ctypes.c_ulong()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(ctypes.get_last_error(), "cannot size Windows process token")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, size, ctypes.byref(size)
        ):
            raise OSError(ctypes.get_last_error(), "cannot read Windows process token")
        sid = ctypes.c_void_p.from_buffer(buffer).value
        return _windows_sid_string(sid, advapi32, kernel32, ctypes)
    finally:
        kernel32.CloseHandle(token)


def _read_windows_acl(  # pragma: no cover - Windows only
    path: Path,
) -> tuple[str, str, set[str]]:
    import ctypes

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", ctypes.c_ulong),
            ("AclBytesInUse", ctypes.c_ulong),
            ("AclBytesFree", ctypes.c_ulong),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", ctypes.c_ushort),
        ]

    advapi32, kernel32 = _windows_apis(ctypes)
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000001 | 0x00000004,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(result, "cannot read Windows security descriptor")
    try:
        if not owner.value or not dacl.value:
            raise OSError("Windows owner or DACL is missing")
        current_sid = _windows_current_sid(advapi32, kernel32, ctypes)
        owner_sid = _windows_sid_string(owner, advapi32, kernel32, ctypes)
        information = AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(information), ctypes.sizeof(information), 2
        ):
            raise OSError(
                getattr(ctypes, "get_last_error", lambda: 0)(),
                "cannot inspect Windows DACL",
            )
        allowed_sids: set[str] = set()
        for index in range(information.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError(
                    getattr(ctypes, "get_last_error", lambda: 0)(),
                    "cannot inspect Windows ACE",
                )
            if ace.value is None:
                raise OSError("Windows ACE is missing")
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            if header.AceSize < 8:
                raise OSError("Windows ACE is malformed")
            if _windows_ace_kind(header.AceType) == "allow":
                sid_address = ace.value + 8
                allowed_sids.add(
                    _windows_sid_string(
                        ctypes.c_void_p(sid_address), advapi32, kernel32, ctypes
                    )
                )
        return owner_sid, current_sid, allowed_sids
    finally:
        kernel32.LocalFree(descriptor)


def _lock_down_windows_path(path: Path) -> None:  # pragma: no cover - Windows only
    import ctypes

    try:
        advapi32, kernel32 = _windows_apis(ctypes)
    except OSError as exc:
        raise RuntimeError("Windows security APIs are unavailable") from exc
    current_sid = _windows_current_sid(advapi32, kernel32, ctypes)
    sddl = f"O:{current_sid}D:P(A;;FA;;;SY)(A;;FA;;;{current_sid})"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise RuntimeError("cannot create private Windows security descriptor")
    try:
        security_information = 0x00000001 | 0x00000004 | 0x80000000
        if not advapi32.SetFileSecurityW(
            str(path), security_information, descriptor
        ):
            raise RuntimeError("cannot apply private Windows ACL")
    finally:
        kernel32.LocalFree(descriptor)


def _assert_windows_path_secure(  # pragma: no cover - Windows only
    path: Path, *, directory: bool
) -> None:
    if not _windows_path_is_secure(
        path,
        directory=directory,
        metadata_reader=lambda value: value.lstat(),
        acl_reader=_read_windows_acl,
    ):
        kind = "queue directory" if directory else "state file"
        raise RuntimeError(f"HEY {kind} is insecure")


class DurableQueue:
    _locks_guard: ClassVar = threading.Lock()
    _locks: ClassVar[dict[str, threading.RLock]] = {}

    def __init__(
        self,
        path: Path,
        max_seen: int = 10_000,
        max_pending: int = 10_000,
        max_state_bytes: int = 16 << 20,
    ):
        self.path = Path(path)
        self.max_seen = max(100, int(max_seen))
        self.max_pending = max(1, int(max_pending))
        self.max_state_bytes = max(1, int(max_state_bytes))
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _ensure_secure_parent(self, *, create: bool) -> None:
        parent = self.path.parent
        if os.name == "nt":  # pragma: no cover - Windows only
            try:
                parent.lstat()
                existed = True
            except FileNotFoundError:
                existed = False
            if create:
                try:
                    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                except OSError:
                    raise RuntimeError("HEY queue directory is insecure") from None
                if not existed:
                    _lock_down_windows_path(parent)
            try:
                parent.lstat()
            except FileNotFoundError:
                return
            _assert_windows_path_secure(parent, directory=True)
            return
        directory_fd = self._open_posix_parent(create=create)
        if directory_fd is not None:
            os.close(directory_fd)

    def _assert_posix_parent_identity(self, directory_fd: int) -> None:
        metadata = os.fstat(directory_fd)
        try:
            path_metadata = os.stat(self.path.parent, follow_symlinks=False)
        except OSError:
            raise RuntimeError("HEY queue directory is insecure") from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError("HEY queue directory is insecure")

    def _open_posix_parent(self, *, create: bool) -> int | None:
        if create:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                raise RuntimeError("HEY queue directory is insecure") from None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            directory_fd = os.open(self.path.parent, flags)
        except FileNotFoundError:
            if not create:
                return None
            raise RuntimeError("HEY queue directory is insecure") from None
        except OSError:
            raise RuntimeError("HEY queue directory is insecure") from None
        try:
            self._assert_posix_parent_identity(directory_fd)
        except BaseException:
            os.close(directory_fd)
            raise
        return directory_fd

    def _read_secure_state(
        self,
        directory_fd: int | None = None,
        windows_transaction: _WindowsStateTransaction | None = None,
    ) -> bytes | None:
        if os.name == "nt":
            if windows_transaction is None:
                raise RuntimeError("Windows secure state transaction is unavailable")
            return windows_transaction.read_state(self.max_state_bytes + 1)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        try:
            if directory_fd is None:
                raise RuntimeError("HEY queue directory is insecure")
            self._assert_posix_parent_identity(directory_fd)
            fd = os.open(self.path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            if directory_fd is not None:
                self._assert_posix_parent_identity(directory_fd)
            return None
        except OSError:
            raise RuntimeError("HEY state file is insecure") from None
        try:
            metadata = os.fstat(fd)
            if os.name == "nt":
                path_metadata = self.path.lstat()
                if (
                    not _windows_metadata_is_secure(path_metadata, directory=False)
                    or (path_metadata.st_dev, path_metadata.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise RuntimeError("HEY state file is insecure")
                _assert_windows_path_secure(self.path, directory=False)
            elif (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise RuntimeError("HEY state file is insecure")
            else:
                if directory_fd is None:
                    raise RuntimeError("HEY queue directory is insecure")
                try:
                    path_metadata = os.stat(
                        self.path.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise RuntimeError("HEY state file is insecure") from None
                if (path_metadata.st_dev, path_metadata.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise RuntimeError("HEY state file is insecure")
                self._assert_posix_parent_identity(directory_fd)
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return handle.read(self.max_state_bytes + 1)
        finally:
            if fd >= 0:
                os.close(fd)

    def _load(
        self,
        directory_fd: int | None = None,
        windows_transaction: _WindowsStateTransaction | None = None,
    ) -> dict[str, Any]:
        owns_directory_fd = False
        if os.name != "nt" and directory_fd is None:
            directory_fd = self._open_posix_parent(create=False)
            if directory_fd is None:
                return {"seen": [], "pending": []}
            owns_directory_fd = True
        elif os.name != "nt":
            if directory_fd is None:  # pragma: no cover - narrowed above
                raise RuntimeError("HEY queue directory is insecure")
            self._assert_posix_parent_identity(directory_fd)
        try:
            encoded = self._read_secure_state(directory_fd, windows_transaction)
        finally:
            if owns_directory_fd and directory_fd is not None:
                os.close(directory_fd)
        if encoded is None:
            return {"seen": [], "pending": []}
        if len(encoded) > self.max_state_bytes:
            raise RuntimeError("HEY state exceeds capacity; refusing overwrite")
        try:
            raw = encoded.decode("utf-8")
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("HEY state is unreadable; refusing overwrite") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"seen", "pending"}
            or not isinstance(value["seen"], list)
            or not isinstance(value["pending"], list)
        ):
            raise RuntimeError("HEY state is unreadable; refusing overwrite")
        if len(value["pending"]) > self.max_pending:
            raise RuntimeError("HEY pending queue exceeds capacity")
        try:
            if any(
                not isinstance(identity, str)
                or _EVENT_IDENTITY.fullmatch(identity) is None
                for identity in value["seen"]
            ):
                raise ValueError("invalid persisted HEY identity")
            seen_identities = set(value["seen"])
            if len(seen_identities) != len(value["seen"]):
                raise ValueError("duplicate seen HEY identity")
            pending_identities = {
                _parse_persisted_event(item).identity for item in value["pending"]
            }
            if len(pending_identities) != len(value["pending"]):
                raise ValueError("duplicate pending HEY identity")
            if not pending_identities <= seen_identities:
                raise ValueError("pending HEY identity is missing from seen")
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("HEY state is unreadable; refusing overwrite") from None
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

    def _save_posix(self, encoded: bytes, directory_fd: int | None = None) -> None:
        owns_directory_fd = directory_fd is None
        if directory_fd is None:
            directory_fd = self._open_posix_parent(create=True)
            if directory_fd is None:  # pragma: no cover - create=True is exhaustive
                raise RuntimeError("HEY queue directory is insecure")
        else:
            self._assert_posix_parent_identity(directory_fd)
        temporary_name = f".{self.path.name}.{secrets.token_hex(16)}.tmp"
        temporary_fd = -1
        temporary_created = False
        replaced = False
        try:
            self._assert_posix_parent_identity(directory_fd)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            try:
                temporary_fd = os.open(
                    temporary_name, flags, 0o600, dir_fd=directory_fd
                )
                temporary_created = True
            except OSError:
                raise RuntimeError("HEY temporary state file is insecure") from None
            metadata = os.fstat(temporary_fd)
            os.fchmod(temporary_fd, 0o600)
            metadata = os.fstat(temporary_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise RuntimeError("HEY temporary state file is insecure")
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(temporary_fd, remaining)
                if written <= 0:
                    raise OSError("cannot write HEY state")
                remaining = remaining[written:]
            os.fsync(temporary_fd)
            temporary_metadata = os.stat(
                temporary_name, dir_fd=directory_fd, follow_symlinks=False
            )
            if (temporary_metadata.st_dev, temporary_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RuntimeError("HEY temporary state file is insecure")
            self._assert_posix_parent_identity(directory_fd)
            os.replace(
                temporary_name,
                self.path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            replaced = True
            state_metadata = os.stat(
                self.path.name, dir_fd=directory_fd, follow_symlinks=False
            )
            if (state_metadata.st_dev, state_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RuntimeError("HEY state file is insecure")
            self._assert_posix_parent_identity(directory_fd)
            os.fsync(directory_fd)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if temporary_created and not replaced:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            if owns_directory_fd:
                os.close(directory_fd)

    def _save(
        self,
        *,
        seen: list[str],
        pending: list[dict[str, Any]],
        directory_fd: int | None = None,
        windows_transaction: _WindowsStateTransaction | None = None,
    ) -> None:
        pending_ids = [HeyEvent.from_dict(item).identity for item in pending]
        pending_set = set(pending_ids)
        completed_seen = [identity for identity in seen if identity not in pending_set]
        durable_seen = self._trim(completed_seen, self.max_seen)
        durable_seen.extend(self._trim(pending_ids, len(pending_ids)))
        serialized = json.dumps(
            {"seen": durable_seen, "pending": pending},
            ensure_ascii=False,
            sort_keys=True,
        )
        encoded = serialized.encode("utf-8")
        if len(encoded) > self.max_state_bytes:
            raise RuntimeError("HEY state exceeds capacity; refusing write")
        if os.name != "nt":
            self._save_posix(encoded, directory_fd)
            return
        if windows_transaction is None:
            raise RuntimeError("Windows secure state transaction is unavailable")
        windows_transaction.replace_state(encoded)

    def ingest(self, event: HeyEvent, authorize: Authorize) -> bool:
        if not authorize(event):
            return False
        with self._lock:
            if os.name == "nt":
                with _windows_state_transaction(self.path, create=True) as transaction:
                    value = self._load(windows_transaction=transaction)
                    seen = [str(item) for item in value["seen"]]
                    if event.identity in seen:
                        return False
                    if len(value["pending"]) >= self.max_pending:
                        raise RuntimeError("HEY pending queue is full")
                    seen.append(event.identity)
                    pending = list(value["pending"])
                    pending.append(event.to_dict())
                    self._save(
                        seen=seen,
                        pending=pending,
                        windows_transaction=transaction,
                    )
                    return True
            directory_fd = self._open_posix_parent(create=True)
            try:
                value = self._load(directory_fd)
                seen = [str(item) for item in value["seen"]]
                if event.identity in seen:
                    return False
                if len(value["pending"]) >= self.max_pending:
                    raise RuntimeError("HEY pending queue is full")
                seen.append(event.identity)
                pending = list(value["pending"])
                pending.append(event.to_dict())
                self._save(
                    seen=seen, pending=pending, directory_fd=directory_fd
                )
                return True
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)

    def pending(self) -> list[HeyEvent]:
        with self._lock:
            if os.name == "nt":
                with _windows_state_transaction(self.path, create=False) as transaction:
                    return [
                        HeyEvent.from_dict(item)
                        for item in self._load(windows_transaction=transaction)["pending"]
                    ]
            directory_fd = self._open_posix_parent(create=False)
            if directory_fd is None:
                return []
            try:
                return [
                    HeyEvent.from_dict(item)
                    for item in self._load(directory_fd)["pending"]
                ]
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)

    def complete(self, identity: str) -> None:
        with self._lock:
            if os.name == "nt":
                with _windows_state_transaction(self.path, create=True) as transaction:
                    value = self._load(windows_transaction=transaction)
                    pending = [
                        item
                        for item in value["pending"]
                        if HeyEvent.from_dict(item).identity != identity
                    ]
                    self._save(
                        seen=[str(item) for item in value["seen"]],
                        pending=pending,
                        windows_transaction=transaction,
                    )
                    return
            directory_fd = self._open_posix_parent(create=True)
            try:
                value = self._load(directory_fd)
                pending = [
                    item
                    for item in value["pending"]
                    if HeyEvent.from_dict(item).identity != identity
                ]
                self._save(
                    seen=[str(item) for item in value["seen"]],
                    pending=pending,
                    directory_fd=directory_fd,
                )
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
