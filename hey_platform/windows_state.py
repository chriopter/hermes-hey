"""Handle-relative, rename-resistant Windows DurableQueue storage."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self


class _WindowsApi(Protocol):
    def open_parent(self, path: Path) -> int: ...
    def open_state(self, path: Path) -> int | None: ...
    def create_temp(self, path: Path) -> int: ...
    def final_path(self, handle: int) -> Path: ...
    def assert_private(self, handle: int, *, directory: bool) -> None: ...
    def set_private(self, handle: int) -> None: ...
    def read(self, handle: int, limit: int) -> bytes: ...
    def write_through(self, handle: int, encoded: bytes) -> None: ...
    def rename_by_handle(self, handle: int, parent: int, name: str) -> None: ...
    def delete_by_handle(self, handle: int) -> None: ...
    def close(self, handle: int) -> None: ...


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


class WindowsStateTransaction:
    """Own a no-share-delete parent handle for one queue transaction."""

    def __init__(self, path: Path, *, create: bool, api: _WindowsApi | None = None):
        self.path = Path(path)
        self.parent_path = self.path.parent
        self.create = create
        self.api = api if api is not None else _NativeWindowsApi()
        self.parent_handle: int | None = None

    def __enter__(self) -> Self:
        existed = self.parent_path.exists()
        if not existed:
            if not self.create:
                return self
            self.parent_path.mkdir(mode=0o700, parents=True, exist_ok=False)
        expected = _canonical(self.parent_path)
        handle = self.api.open_parent(self.parent_path)
        try:
            if not existed:
                self.api.set_private(handle)
            self.api.assert_private(handle, directory=True)
            self._assert_final_path(handle, expected)
        except BaseException:
            self.api.close(handle)
            raise
        self.parent_handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.parent_handle is not None:
            self.api.close(self.parent_handle)
            self.parent_handle = None

    def _assert_final_path(self, handle: int, expected: str) -> None:
        if _canonical(self.api.final_path(handle)) != expected:
            raise RuntimeError("HEY Windows handle resolved outside queue directory")

    def read_state(self, limit: int) -> bytes | None:
        if self.parent_handle is None:
            return None
        handle = self.api.open_state(self.path)
        if handle is None:
            return None
        try:
            self.api.assert_private(handle, directory=False)
            self._assert_final_path(handle, _canonical(self.path))
            return self.api.read(handle, limit)
        finally:
            self.api.close(handle)

    def replace_state(self, encoded: bytes) -> None:
        if self.parent_handle is None:
            raise RuntimeError("HEY queue directory is insecure")
        temporary_path = self.parent_path / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        handle = self.api.create_temp(temporary_path)
        renamed = False
        try:
            self.api.set_private(handle)
            self.api.assert_private(handle, directory=False)
            self._assert_final_path(handle, _canonical(temporary_path))
            self.api.write_through(handle, encoded)
            self.api.rename_by_handle(handle, self.parent_handle, self.path.name)
            renamed = True
            self._assert_final_path(handle, _canonical(self.path))
            self.api.assert_private(handle, directory=False)
        finally:
            if not renamed:
                try:
                    self.api.delete_by_handle(handle)
                except OSError:
                    pass
            self.api.close(handle)


class _NativeWindowsApi:  # pragma: no cover - exercised on Windows CI
    """Small ctypes facade; every filesystem object is validated by handle."""

    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_WRITE_THROUGH = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    CREATE_NEW = 1
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_READ_ATTRIBUTES = 0x80
    FILE_LIST_DIRECTORY = 0x1
    READ_CONTROL = 0x00020000
    WRITE_DAC = 0x00040000
    DELETE = 0x00010000
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("Windows native APIs are unavailable")
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = win_dll("kernel32", use_last_error=True)
        self.advapi32 = win_dll("advapi32", use_last_error=True)
        void_pointer = ctypes.c_void_p
        void_pointer_pointer = ctypes.POINTER(void_pointer)
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            void_pointer,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            void_pointer,
            wintypes.DWORD,
        ]
        self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            void_pointer,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            void_pointer,
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = self.kernel32.ReadFile.argtypes
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self.kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            void_pointer,
            wintypes.DWORD,
        ]
        self.kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            void_pointer_pointer,
            void_pointer_pointer,
            void_pointer_pointer,
            void_pointer_pointer,
            void_pointer_pointer,
        ]
        self.advapi32.GetSecurityInfo.restype = wintypes.DWORD
        self.advapi32.SetKernelObjectSecurity.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            void_pointer,
        ]
        self.advapi32.SetKernelObjectSecurity.restype = wintypes.BOOL

    def _error(self, message: str) -> OSError:
        last_error = getattr(self.ctypes, "get_last_error", lambda: 0)
        return OSError(last_error(), message)

    def _create_file(
        self, path: Path, access: int, disposition: int, flags: int
    ) -> int:
        handle = self.kernel32.CreateFileW(
            str(path),
            access,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE,
            None,
            disposition,
            flags,
            None,
        )
        invalid = self.ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise self._error(f"cannot open secure Windows path: {path}")
        return int(handle)

    def open_parent(self, path: Path) -> int:
        return self._create_file(
            path,
            self.FILE_LIST_DIRECTORY
            | self.FILE_READ_ATTRIBUTES
            | self.READ_CONTROL
            | self.WRITE_DAC,
            self.OPEN_EXISTING,
            self.FILE_FLAG_OPEN_REPARSE_POINT | self.FILE_FLAG_BACKUP_SEMANTICS,
        )

    def open_state(self, path: Path) -> int | None:
        try:
            return self._create_file(
                path,
                self.GENERIC_READ | self.FILE_READ_ATTRIBUTES | self.READ_CONTROL,
                self.OPEN_EXISTING,
                self.FILE_FLAG_OPEN_REPARSE_POINT,
            )
        except OSError as exc:
            if exc.errno in {2, 3}:
                return None
            raise

    def create_temp(self, path: Path) -> int:
        return self._create_file(
            path,
            self.GENERIC_WRITE
            | self.FILE_READ_ATTRIBUTES
            | self.READ_CONTROL
            | self.WRITE_DAC
            | self.DELETE,
            self.CREATE_NEW,
            self.FILE_ATTRIBUTE_NORMAL
            | self.FILE_FLAG_OPEN_REPARSE_POINT
            | self.FILE_FLAG_WRITE_THROUGH,
        )

    def final_path(self, handle: int) -> Path:
        size = self.kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size:
            raise self._error("cannot size final Windows handle path")
        buffer = self.ctypes.create_unicode_buffer(size + 1)
        if not self.kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
            raise self._error("cannot read final Windows handle path")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def _attributes(self, handle: int) -> int:
        class FileAttributeTagInfo(self.ctypes.Structure):
            _fields_ = (
                ("FileAttributes", self.wintypes.DWORD),
                ("ReparseTag", self.wintypes.DWORD),
            )

        info = FileAttributeTagInfo()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle, 9, self.ctypes.byref(info), self.ctypes.sizeof(info)
        ):
            raise self._error("cannot inspect Windows handle attributes")
        return int(info.FileAttributes)

    def assert_private(self, handle: int, *, directory: bool) -> None:
        from .engine import (
            _windows_ace_kind,
            _windows_acl_is_private,
            _windows_apis,
            _windows_current_sid,
            _windows_sid_string,
        )

        attributes = self._attributes(handle)
        if attributes & self.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError("HEY Windows handle is a reparse point")
        if bool(attributes & self.FILE_ATTRIBUTE_DIRECTORY) != directory:
            raise RuntimeError("HEY Windows handle has the wrong type")
        advapi32, kernel32 = _windows_apis(self.ctypes)
        owner = self.ctypes.c_void_p()
        dacl = self.ctypes.c_void_p()
        descriptor = self.ctypes.c_void_p()
        result = self.advapi32.GetSecurityInfo(
            handle,
            1,
            0x00000001 | 0x00000004,
            self.ctypes.byref(owner),
            None,
            self.ctypes.byref(dacl),
            None,
            self.ctypes.byref(descriptor),
        )
        if result or not owner.value or not dacl.value:
            raise OSError(result, "cannot read Windows handle security")

        class AclSizeInformation(self.ctypes.Structure):
            _fields_ = (
                ("AceCount", self.wintypes.DWORD),
                ("AclBytesInUse", self.wintypes.DWORD),
                ("AclBytesFree", self.wintypes.DWORD),
            )

        class AceHeader(self.ctypes.Structure):
            _fields_ = (
                ("AceType", self.ctypes.c_ubyte),
                ("AceFlags", self.ctypes.c_ubyte),
                ("AceSize", self.ctypes.c_ushort),
            )

        try:
            current = _windows_current_sid(advapi32, kernel32, self.ctypes)
            owner_sid = _windows_sid_string(owner, advapi32, kernel32, self.ctypes)
            info = AclSizeInformation()
            if not advapi32.GetAclInformation(
                dacl, self.ctypes.byref(info), self.ctypes.sizeof(info), 2
            ):
                raise self._error("cannot inspect Windows handle DACL")
            allowed: set[str] = set()
            for index in range(info.AceCount):
                ace = self.ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, self.ctypes.byref(ace)) or ace.value is None:
                    raise self._error("cannot inspect Windows handle ACE")
                header = self.ctypes.cast(ace, self.ctypes.POINTER(AceHeader)).contents
                if header.AceSize < 8:
                    raise OSError("malformed Windows handle ACE")
                if _windows_ace_kind(header.AceType) == "allow":
                    allowed.add(
                        _windows_sid_string(
                            self.ctypes.c_void_p(ace.value + 8),
                            advapi32,
                            kernel32,
                            self.ctypes,
                        )
                    )
            if not _windows_acl_is_private(
                owner_sid=owner_sid, current_sid=current, allowed_sids=allowed
            ):
                raise RuntimeError("HEY Windows handle ACL is insecure")
        finally:
            kernel32.LocalFree(descriptor)

    def set_private(self, handle: int) -> None:
        from .engine import _windows_apis, _windows_current_sid

        advapi32, kernel32 = _windows_apis(self.ctypes)
        current = _windows_current_sid(advapi32, kernel32, self.ctypes)
        sddl = f"O:{current}D:P(A;;FA;;;SY)(A;;FA;;;{current})"
        descriptor = self.ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, self.ctypes.byref(descriptor), None
        ):
            raise self._error("cannot create private Windows security descriptor")
        try:
            if not advapi32.SetKernelObjectSecurity(
                handle, 0x00000001 | 0x00000004 | 0x80000000, descriptor
            ):
                raise self._error("cannot apply private Windows handle ACL")
        finally:
            kernel32.LocalFree(descriptor)

    def read(self, handle: int, limit: int) -> bytes:
        chunks: list[bytes] = []
        remaining = limit
        while remaining:
            buffer = self.ctypes.create_string_buffer(min(remaining, 64 << 10))
            read = self.wintypes.DWORD()
            if not self.kernel32.ReadFile(
                handle, buffer, len(buffer), self.ctypes.byref(read), None
            ):
                raise self._error("cannot read Windows state handle")
            if not read.value:
                break
            chunks.append(buffer.raw[: read.value])
            remaining -= read.value
        return b"".join(chunks)

    def write_through(self, handle: int, encoded: bytes) -> None:
        remaining = memoryview(encoded)
        while remaining:
            chunk = bytes(remaining[: 64 << 10])
            written = self.wintypes.DWORD()
            if not self.kernel32.WriteFile(
                handle, chunk, len(chunk), self.ctypes.byref(written), None
            ) or not written.value:
                raise self._error("cannot write Windows state handle")
            remaining = remaining[written.value :]
        if not self.kernel32.FlushFileBuffers(handle):
            raise self._error("cannot flush Windows state handle")

    def rename_by_handle(self, handle: int, parent: int, name: str) -> None:
        class FileRenameInfo(self.ctypes.Structure):
            _fields_ = (
                ("ReplaceIfExists", self.wintypes.BOOL),
                ("RootDirectory", self.wintypes.HANDLE),
                ("FileNameLength", self.wintypes.DWORD),
                ("FileName", self.wintypes.WCHAR * 1),
            )

        encoded_length = len(name.encode("utf-16-le"))
        size = FileRenameInfo.FileName.offset + encoded_length
        buffer = self.ctypes.create_string_buffer(size)
        info = self.ctypes.cast(buffer, self.ctypes.POINTER(FileRenameInfo)).contents
        info.ReplaceIfExists = True
        info.RootDirectory = parent
        info.FileNameLength = encoded_length
        self.ctypes.memmove(
            self.ctypes.addressof(buffer) + FileRenameInfo.FileName.offset,
            self.ctypes.create_unicode_buffer(name),
            encoded_length,
        )
        if not self.kernel32.SetFileInformationByHandle(handle, 3, buffer, size):
            raise self._error("cannot atomically replace Windows state")
        if not self.kernel32.FlushFileBuffers(handle):
            raise self._error("cannot flush replaced Windows state")

    def delete_by_handle(self, handle: int) -> None:
        class FileDispositionInfo(self.ctypes.Structure):
            _fields_ = (("DeleteFile", self.wintypes.BOOL),)

        info = FileDispositionInfo(True)
        if not self.kernel32.SetFileInformationByHandle(
            handle, 4, self.ctypes.byref(info), self.ctypes.sizeof(info)
        ):
            raise self._error("cannot remove Windows temporary state")

    def close(self, handle: int) -> None:
        if not self.kernel32.CloseHandle(handle):
            raise self._error("cannot close Windows handle")
