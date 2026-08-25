"""Official HEY CLI wrapper for the Hermes platform adapter."""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core import HeyEvent, event_from_watch

Runner = Callable[[list[str], str | None], dict[str, Any]]


def _command_shape(args: list[str]) -> str:
    verb = next((part for part in args if part and not part.startswith("-")), "")
    return f"hey {verb}".strip()


def make_subprocess_runner(
    *, account: str | None = None, config_dir: str | None = None
) -> Runner:
    base_env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
        if key in os.environ
    }
    if config_dir:
        root = Path(config_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(root, 0o700)
        base_env["XDG_CONFIG_HOME"] = str(root)

    def run(args: list[str], stdin: str | None = None) -> dict[str, Any]:
        command = ["hey", *args, "--json"]
        if account:
            command += ["--account", str(account)]
        try:
            process = subprocess.run(
                command,
                check=False,
                text=True,
                input=stdin,
                capture_output=True,
                timeout=120,
                env=base_env,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{_command_shape(args)} timed out") from None
        except OSError:
            raise RuntimeError(f"{_command_shape(args)} could not start") from None
        if process.returncode != 0:
            raise RuntimeError(
                f"{_command_shape(args)} failed (exit {process.returncode})"
            )
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("HEY CLI returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("HEY CLI returned invalid JSON")  # noqa: TRY004
        return value

    return run


class HeyCLI:
    def __init__(self, runner: Runner, *, account: str, own_email: str):
        self.runner = runner
        self.account = str(account)
        self.own_email = own_email.strip().lower()

    def run(self, *args: str, stdin: str | None = None) -> dict[str, Any]:
        result = self.runner(list(args), stdin)
        if result.get("ok") is not True:
            raise RuntimeError(f"{_command_shape(list(args))} failed")
        return result

    def verify_version(self) -> bool:
        data = self.run("version").get("data")
        version = str(data.get("version") or "") if isinstance(data, dict) else ""
        core = r"(0|[1-9][0-9]*)"
        identifier = r"[0-9A-Za-z-]+"
        build = rf"(?:\+{identifier}(?:\.{identifier})*)?"
        match = re.fullmatch(rf"{core}\.{core}\.{core}{build}", version)
        if not match or tuple(int(part) for part in match.groups()) < (1, 1, 0):
            raise RuntimeError("HEY CLI 1.1.0 or newer is required")
        return True

    def verify_identity(self) -> bool:
        data = self.run("account", "list").get("data")
        accounts = data if isinstance(data, list) else []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if (
                str(account.get("id") or "") == self.account
                and str(account.get("email") or "").strip().lower()
                == self.own_email
            ):
                return True
        raise RuntimeError("HEY authenticated identity does not match configuration")

    def hydrate_event(self, raw: dict[str, Any]) -> HeyEvent | None:
        thread_id = raw.get("thread_id")
        if not thread_id:
            return None
        data = self.run("thread", "read", str(thread_id)).get("data")
        entries = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        return event_from_watch(raw, entries, own_email=self.own_email)

    def reply(self, thread_id: int, text: str) -> dict[str, Any]:
        return self.run("reply", str(thread_id), stdin=text)
