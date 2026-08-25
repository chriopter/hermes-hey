from __future__ import annotations

import sys
from pathlib import Path

import pytest

root = Path("/usr/local/lib/hermes-agent")
if root.exists():
    sys.path.insert(0, str(root))


@pytest.fixture(autouse=True)
def isolate_gateway_runtime_status(monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter

    monkeypatch.setattr(
        BasePlatformAdapter,
        "_write_runtime_status_safe",
        lambda *_args, **_kwargs: None,
    )
