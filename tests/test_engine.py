from __future__ import annotations

import inspect
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hey_platform import engine as engine_module
from hey_platform.core import HeyEvent
from hey_platform.engine import DurableQueue


def event(entry_id: int, sender: str = "authorized@example.com", content: str = "work") -> HeyEvent:
    return HeyEvent(
        event_id=f"thread:456:entry:{entry_id}",
        posting_id=123,
        thread_id=456,
        entry_id=entry_id,
        account_id=12345,
        sender_id=77,
        sender_name="Christopher",
        sender_email=sender,
        subject="Request",
        content=content,
        app_url="https://app.hey.com/topics/456",
        created_at="2026-08-25T16:10:00Z",
        box_kind="imbox",
    )


def write_state(path: Path, value: object) -> bytes:
    encoded = json.dumps(value).encode()
    path.write_bytes(encoded)
    path.chmod(0o600)
    return encoded


def test_unauthorized_content_is_never_persisted(tmp_path: Path) -> None:
    queue = DurableQueue(tmp_path / "state.json")
    item = event(900, sender="outsider@example.com", content="private outsider text")

    assert queue.ingest(item, lambda e: e.sender_email == "authorized@example.com") is False

    assert not (tmp_path / "state.json").exists()


def test_authorized_event_survives_restart_until_exact_delivery_completion(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = DurableQueue(path)
    item = event(900)

    assert first.ingest(item, lambda _e: True) is True
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_suffix(".json.tmp").exists()
    restarted = DurableQueue(path)
    assert restarted.pending() == [item]

    restarted.complete(item.identity)
    assert DurableQueue(path).pending() == []


def test_duplicate_event_is_dispatched_only_once(tmp_path: Path) -> None:
    queue = DurableQueue(tmp_path / "state.json")
    item = event(900)

    assert queue.ingest(item, lambda _e: True) is True
    assert queue.ingest(item, lambda _e: True) is False
    assert queue.pending() == [item]


def test_completing_old_event_preserves_newly_ingested_event(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    queue = DurableQueue(path)
    old = event(900)
    new = event(901)
    queue.ingest(old, lambda _e: True)
    queue.ingest(new, lambda _e: True)

    queue.complete(old.identity)

    assert queue.pending() == [new]


def test_pending_identity_is_never_evicted_from_seen_window(tmp_path: Path) -> None:
    queue = DurableQueue(tmp_path / "state.json", max_seen=100)
    first = event(900)
    assert queue.ingest(first, lambda _e: True) is True
    for entry_id in range(901, 1001):
        assert queue.ingest(event(entry_id), lambda _e: True) is True

    assert queue.ingest(first, lambda _e: True) is False
    assert [item.identity for item in queue.pending()].count(first.identity) == 1


def test_multiple_queue_instances_serialize_state_updates(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    queues = [DurableQueue(path) for _ in range(4)]

    def ingest(entry_id: int) -> bool:
        return queues[entry_id % len(queues)].ingest(event(entry_id), lambda _e: True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(ingest, range(900, 1000)))

    assert all(accepted)
    assert len(DurableQueue(path).pending()) == 100


def test_corrupt_state_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{broken")
    path.chmod(0o600)
    queue = DurableQueue(path)

    with pytest.raises(RuntimeError, match="HEY state is unreadable"):
        queue.ingest(event(900), lambda _e: True)

    assert path.read_text() == "{broken"


def test_existing_queue_parent_with_group_access_fails_closed(tmp_path: Path) -> None:
    parent = tmp_path / "queue"
    parent.mkdir(mode=0o750)
    path = parent / "state.json"

    with pytest.raises(RuntimeError, match="queue directory is insecure"):
        DurableQueue(path).ingest(event(900), lambda _e: True)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o750
    assert not path.exists()


def test_symlink_queue_parent_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    parent = tmp_path / "queue"
    parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="queue directory is insecure"):
        DurableQueue(parent / "state.json").ingest(event(900), lambda _e: True)

    assert not (target / "state.json").exists()


def test_existing_state_with_group_access_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"pending": [], "seen": []}')
    path.chmod(0o640)

    with pytest.raises(RuntimeError, match="state file is insecure"):
        DurableQueue(path).pending()

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_symlink_state_is_rejected_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"pending": [], "seen": []}')
    target.chmod(0o600)
    path = tmp_path / "state.json"
    path.symlink_to(target)

    with pytest.raises(RuntimeError, match="state file is insecure"):
        DurableQueue(path).pending()

    assert path.is_symlink()


@pytest.mark.skipif(engine_module.os.name == "nt", reason="POSIX temp behavior")
def test_abandoned_temp_does_not_block_write_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    target = tmp_path / "target.txt"
    target.write_text("do not overwrite")
    target.chmod(0o600)
    abandoned = tmp_path / "state.json.tmp"
    abandoned.symlink_to(target)
    item = event(900)

    assert DurableQueue(path).ingest(item, lambda _event: True)

    assert DurableQueue(path).pending() == [item]
    assert target.read_text() == "do not overwrite"
    assert abandoned.is_symlink()


@pytest.mark.skipif(engine_module.os.name == "nt", reason="POSIX dirfd behavior")
def test_parent_swap_during_state_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "queue"
    parent.mkdir(mode=0o700)
    path = parent / "state.json"
    original = write_state(path, {"seen": [], "pending": []})
    detached = tmp_path / "detached"
    attacker = json.dumps(
        {"seen": [event(902).identity], "pending": [event(902).to_dict()]}
    ).encode()
    real_open = engine_module.os.open
    swapped = False

    def swapping_open(
        file: str | Path, flags: int, mode: int = 0o777, **kwargs
    ) -> int:
        nonlocal swapped
        if not swapped and (
            Path(file) == path
            or (file == path.name and kwargs.get("dir_fd") is not None)
        ):
            swapped = True
            parent.rename(detached)
            parent.mkdir(mode=0o700)
            path.write_bytes(attacker)
            path.chmod(0o600)
        return real_open(file, flags, mode, **kwargs)

    monkeypatch.setattr(engine_module.os, "open", swapping_open)

    with pytest.raises(RuntimeError, match="queue directory is insecure"):
        DurableQueue(path).ingest(event(901), lambda _event: True)

    assert (detached / "state.json").read_bytes() == original
    assert path.read_bytes() == attacker


@pytest.mark.skipif(engine_module.os.name == "nt", reason="POSIX dirfd behavior")
def test_parent_swap_between_load_and_save_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "queue"
    parent.mkdir(mode=0o700)
    path = parent / "state.json"
    original = write_state(path, {"seen": [], "pending": []})
    detached = tmp_path / "detached"
    attacker = write_state(tmp_path / "attacker.json", {"seen": [], "pending": []})
    (tmp_path / "attacker.json").unlink()
    queue = DurableQueue(path)
    real_save = queue._save

    def swapping_save(
        *, seen: list[str], pending: list[dict[str, Any]], **kwargs: Any
    ) -> None:
        parent.rename(detached)
        parent.mkdir(mode=0o700)
        path.write_bytes(attacker)
        path.chmod(0o600)
        real_save(seen=seen, pending=pending, **kwargs)

    monkeypatch.setattr(queue, "_save", swapping_save)

    with pytest.raises(RuntimeError, match="queue directory is insecure"):
        queue.ingest(event(901), lambda _event: True)

    assert (detached / "state.json").read_bytes() == original
    assert path.read_bytes() == attacker


def test_windows_acl_contract_rejects_foreign_owner_or_broad_allow() -> None:
    private = {"S-1-5-18", "S-1-5-32-544", "S-1-5-21-current"}

    assert engine_module._windows_acl_is_private(
        owner_sid="S-1-5-21-current",
        current_sid="S-1-5-21-current",
        allowed_sids=private,
    )
    assert not engine_module._windows_acl_is_private(
        owner_sid="S-1-5-21-other",
        current_sid="S-1-5-21-current",
        allowed_sids=private,
    )
    assert not engine_module._windows_acl_is_private(
        owner_sid="S-1-5-21-current",
        current_sid="S-1-5-21-current",
        allowed_sids=private | {"S-1-1-0"},
    )


def test_windows_metadata_contract_rejects_reparse_points_and_wrong_types() -> None:
    directory = stat.S_IFDIR | 0o700
    regular = stat.S_IFREG | 0o600

    assert engine_module._windows_metadata_is_secure(
        SimpleNamespace(st_mode=directory, st_file_attributes=0), directory=True
    )
    assert not engine_module._windows_metadata_is_secure(
        SimpleNamespace(st_mode=directory, st_file_attributes=0x400), directory=True
    )
    assert not engine_module._windows_metadata_is_secure(
        SimpleNamespace(st_mode=regular, st_file_attributes=0), directory=True
    )


def test_windows_path_validator_fails_closed_on_acl_probe_errors() -> None:
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0)

    def broken_acl_probe(_path: Path) -> tuple[str, str, set[str]]:
        raise OSError("security descriptor unavailable")

    assert not engine_module._windows_path_is_secure(
        Path("state.json"),
        directory=False,
        metadata_reader=lambda _path: metadata,
        acl_reader=broken_acl_probe,
    )
    assert engine_module._windows_path_is_secure(
        Path("state.json"),
        directory=False,
        metadata_reader=lambda _path: metadata,
        acl_reader=lambda _path: (
            "S-1-5-21-current",
            "S-1-5-21-current",
            {"S-1-5-21-current", "S-1-5-18"},
        ),
    )


def test_windows_native_security_contract_uses_owner_dacl_and_protected_acl() -> None:
    acl_source = inspect.getsource(engine_module._read_windows_acl)
    lockdown_source = inspect.getsource(engine_module._lock_down_windows_path)

    assert "GetNamedSecurityInfoW" in acl_source
    assert "GetAclInformation" in acl_source
    assert "GetAce" in acl_source
    assert "SetFileSecurityW" in lockdown_source
    assert "0x80000000" in lockdown_source


def test_windows_ingest_holds_one_parent_transaction_across_read_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    queue = DurableQueue(path)
    calls: list[object] = []

    class FakeTransaction:
        def read_state(self, limit: int) -> bytes | None:
            calls.append(("read", limit))
            return None

        def replace_state(self, encoded: bytes) -> None:
            calls.append(("replace", json.loads(encoded)))

    @contextmanager
    def fake_factory(requested_path: Path, *, create: bool):
        calls.append(("enter", requested_path, create))
        try:
            yield FakeTransaction()
        finally:
            calls.append("exit")

    monkeypatch.setattr(engine_module.os, "name", "nt")
    monkeypatch.setattr(engine_module, "_windows_state_transaction", fake_factory)

    assert queue.ingest(event(900), lambda _event: True)

    assert calls[0] == ("enter", path, True)
    assert calls[1] == ("read", queue.max_state_bytes + 1)
    assert calls[2] == (
        "replace",
        {"pending": [event(900).to_dict()], "seen": [event(900).identity]},
    )
    assert calls[3] == "exit"


def test_windows_native_transaction_contract_blocks_parent_rename_and_renames_temp_by_handle() -> None:
    from hey_platform import windows_state

    source = inspect.getsource(windows_state._NativeWindowsApi)

    assert "FILE_FLAG_OPEN_REPARSE_POINT" in source
    assert "FILE_FLAG_BACKUP_SEMANTICS" in source
    assert "FILE_SHARE_DELETE" not in source
    assert "GetFinalPathNameByHandleW" in source
    assert "GetSecurityInfo" in source
    assert "SetFileInformationByHandle" in source
    assert "FlushFileBuffers" in source


def test_windows_parent_handle_grants_acl_lockdown_access() -> None:
    from hey_platform.windows_state import _NativeWindowsApi

    source = inspect.getsource(_NativeWindowsApi.open_parent)

    assert "self.WRITE_DAC" in source


def test_windows_native_api_declares_64_bit_safe_handle_signatures() -> None:
    from hey_platform.windows_state import _NativeWindowsApi

    source = inspect.getsource(_NativeWindowsApi.__init__)

    for function in (
        "CreateFileW",
        "GetFinalPathNameByHandleW",
        "GetFileInformationByHandleEx",
        "ReadFile",
        "WriteFile",
        "FlushFileBuffers",
        "SetFileInformationByHandle",
        "CloseHandle",
        "GetSecurityInfo",
        "SetKernelObjectSecurity",
    ):
        assert f"{function}.argtypes" in source

    assert "self.advapi32.GetSecurityInfo(" in inspect.getsource(
        _NativeWindowsApi.assert_private
    )
    assert "wintypes.HANDLE" in source.split(
        "self.advapi32.GetSecurityInfo.argtypes", 1
    )[1].split("]", 1)[0]


def test_windows_acl_probe_calls_the_prototyped_get_security_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    from ctypes import wintypes

    from hey_platform.windows_state import _NativeWindowsApi

    calls: list[tuple[str, int]] = []

    class InjectedFunction:
        def __init__(self, name: str, result: int) -> None:
            self.argtypes: list[object] = [ctypes.c_void_p]
            self.restype: object = ctypes.c_ulong
            self.name = name
            self.result = result

        def __call__(self, handle: int, *_args: object) -> int:
            calls.append((self.name, handle))
            return self.result

    prototyped = InjectedFunction("prototyped", 5)
    separately_loaded = InjectedFunction("separately-loaded", 6)
    api: Any = _NativeWindowsApi.__new__(_NativeWindowsApi)
    api.ctypes = ctypes
    api.wintypes = wintypes
    api.advapi32 = SimpleNamespace(GetSecurityInfo=prototyped)
    monkeypatch.setattr(api, "_attributes", lambda _handle: 0)
    monkeypatch.setattr(
        engine_module,
        "_windows_apis",
        lambda _ctypes: (
            SimpleNamespace(GetSecurityInfo=separately_loaded),
            SimpleNamespace(),
        ),
    )

    with pytest.raises(OSError) as raised:
        api.assert_private(0x1_0000_0001, directory=False)

    assert raised.value.errno == 5
    assert calls == [("prototyped", 0x1_0000_0001)]


@pytest.mark.parametrize("ace_type", [2, 3, 4, 5, 9, 11, 255])
def test_windows_dacl_parser_rejects_every_unparsed_ace(ace_type: int) -> None:
    with pytest.raises(OSError, match="unsupported Windows ACE type"):
        engine_module._windows_ace_kind(ace_type)

    assert engine_module._windows_ace_kind(0) == "allow"
    assert engine_module._windows_ace_kind(1) == "deny"


def test_state_save_fsyncs_file_and_parent_directory(tmp_path: Path, monkeypatch) -> None:
    synced_modes: list[int] = []
    real_fsync = engine_module.os.fsync

    def recording_fsync(fd: int) -> None:
        synced_modes.append(engine_module.os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(engine_module.os, "fsync", recording_fsync)

    assert DurableQueue(tmp_path / "state.json").ingest(event(900), lambda _e: True)

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


def test_oversized_existing_state_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    oversized = b"{" + (b" " * 128) + b"}"
    path.write_bytes(oversized)
    path.chmod(0o600)
    queue = DurableQueue(path, max_state_bytes=64)

    with pytest.raises(RuntimeError, match="HEY state exceeds capacity"):
        queue.ingest(event(900), lambda _e: True)

    assert path.read_bytes() == oversized


def test_pending_capacity_applies_backpressure_without_losing_queue(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    queue = DurableQueue(path, max_pending=1)
    first = event(900)

    assert queue.ingest(first, lambda _e: True)
    with pytest.raises(RuntimeError, match="HEY pending queue is full"):
        queue.ingest(event(901), lambda _e: True)

    assert DurableQueue(path, max_pending=1).pending() == [first]


def test_state_capacity_rejects_new_event_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    queue = DurableQueue(path, max_state_bytes=64)

    with pytest.raises(RuntimeError, match="HEY state exceeds capacity"):
        queue.ingest(event(900, content="x" * 128), lambda _e: True)

    assert not path.exists()


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"seen": []},
        {"pending": []},
        {"seen": [], "pending": [], "extra": True},
        {"seen": [900], "pending": []},
        {"seen": ["thread:0:entry:1"], "pending": []},
        {"seen": ["thread:01:entry:1"], "pending": []},
        {"seen": ["thread:1:entry:0"], "pending": []},
        {"seen": [], "pending": [{**event(900).to_dict(), "extra": True}]},
        {"seen": [], "pending": [{**event(900).to_dict(), "content": 123}]},
        {
            "seen": [],
            "pending": [
                {**event(900).to_dict(), "sender_email": "UPPER@EXAMPLE.COM"}
            ],
        },
    ],
)
def test_malformed_persisted_schema_fails_closed_without_overwrite(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "state.json"
    original = write_state(path, value)

    with pytest.raises(RuntimeError, match="HEY state is unreadable"):
        DurableQueue(path).ingest(event(901), lambda _event: True)

    assert path.read_bytes() == original


def test_pending_identity_missing_from_seen_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    original = write_state(
        path,
        {"seen": [], "pending": [event(900).to_dict()]},
    )

    with pytest.raises(RuntimeError, match="HEY state is unreadable"):
        DurableQueue(path).ingest(event(901), lambda _event: True)

    assert path.read_bytes() == original


def test_duplicate_pending_identity_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    item = event(900)
    original = write_state(
        path,
        {
            "seen": [item.identity],
            "pending": [item.to_dict(), item.to_dict()],
        },
    )

    with pytest.raises(RuntimeError, match="HEY state is unreadable"):
        DurableQueue(path).ingest(event(901), lambda _event: True)

    assert path.read_bytes() == original


def test_duplicate_seen_identity_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    identity = event(900).identity
    original = write_state(
        path,
        {"seen": [identity, identity], "pending": []},
    )

    with pytest.raises(RuntimeError, match="HEY state is unreadable"):
        DurableQueue(path).ingest(event(901), lambda _event: True)

    assert path.read_bytes() == original
