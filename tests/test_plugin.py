from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

from hey_platform import plugin as plugin_module
from hey_platform.plugin import apply_yaml_config, register


class FakeContext:
    def __init__(self):
        self.registration = None

    def register_platform(self, **kwargs):
        self.registration = kwargs


def test_register_exposes_fail_closed_hey_platform() -> None:
    context = FakeContext()
    register(context)

    registration = context.registration
    assert registration is not None
    assert registration["name"] == "hey"
    assert registration["label"] == "HEY"
    assert "allowed_users_env" not in registration
    assert "allow_all_env" not in registration
    assert registration["allow_update_command"] is False
    assert registration["max_message_length"] > 0


def test_requirements_check_is_passive_and_config_independent(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin_module.shutil,
        "which",
        lambda _name: pytest.fail("passive requirements check must not inspect PATH"),
    )

    assert plugin_module.check_requirements() is True


def test_register_seeds_quiet_email_display_defaults(monkeypatch) -> None:
    from gateway import display_config

    monkeypatch.delitem(display_config._PLATFORM_DEFAULTS, "hey", raising=False)
    register(FakeContext())

    assert display_config._PLATFORM_DEFAULTS["hey"] == {
        "tool_progress": "off",
        "thinking_progress": False,
        "interim_assistant_messages": False,
        "long_running_notifications": False,
        "busy_ack_detail": False,
    }


def test_yaml_config_keeps_sender_allowlist_profile_scoped() -> None:
    extras = apply_yaml_config(
        {},
        {
            "allow_from": ["Christopher@Example.com"],
            "allow_all_users": "false",
            "account": "12345",
            "own_email": "agent@example.com",
        },
    )

    assert extras == {
        "allow_from": ["Christopher@Example.com"],
        "allow_all_users": False,
        "account": "12345",
        "own_email": "agent@example.com",
    }


def test_connected_check_requires_account_identity_and_cli_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_module, "check_requirements", lambda: True)
    config_dir = tmp_path / "config"
    credentials = config_dir / "hey-cli" / "credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}")
    valid = type(
        "Config",
        (),
        {
            "enabled": True,
            "extra": {
                "account": "12345",
                "own_email": "agent@example.com",
                "config_dir": str(config_dir),
                "allow_from": ["authorized@example.com"],
            },
        },
    )()
    missing_identity = type(
        "Config",
        (),
        {"enabled": True, "extra": {"account": "12345", "config_dir": str(config_dir)}},
    )()

    assert plugin_module.is_connected(valid) is True
    assert plugin_module.is_connected(missing_identity) is False


def test_platform_registry_creates_adapter_with_configured_executable_outside_path(
    tmp_path, monkeypatch
) -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.run import GatewayRunner

    sidecar = tmp_path / "private" / "hermes-hey-sidecar"
    sidecar.parent.mkdir()
    sidecar.write_text("#!/bin/sh\n")
    sidecar.chmod(0o700)
    monkeypatch.setattr(plugin_module.shutil, "which", lambda _name: None)
    config = type(
        "Config",
        (),
        {
            "enabled": True,
            "extra": {
                "account": "12345",
                "own_email": "agent@example.com",
                "allow_from": ["authorized@example.com"],
                "sidecar_binary": str(sidecar),
            },
        },
    )()
    context = FakeContext()
    register(context)
    registration = context.registration
    assert registration is not None
    previous_registration = platform_registry.get("hey")
    platform_registry.register(PlatformEntry(source="builtin", **registration))
    try:
        created = platform_registry.create_adapter("hey", config)
        assert created is not None
        assert created.sidecar_binary == str(sidecar)
        assert created.platform.value == "hey"
        assert created.enforces_own_access_policy is True
        assert created._dm_policy == "allowlist"
        assert created._is_dm_allowed("AUTHORIZED@EXAMPLE.COM") is True
        assert created._is_dm_allowed("outsider@example.com") is False

        runner = cast(Any, object.__new__(GatewayRunner))
        runner.adapters = {created.platform: created}
        runner._profile_adapters = {}
        runner.pairing_store = None
        runner.pairing_stores = {}
        for key in (
            "EMAIL_ALLOWED_USERS",
            "GATEWAY_ALLOWED_USERS",
            "EMAIL_ALLOW_ALL_USERS",
            "GATEWAY_ALLOW_ALL_USERS",
        ):
            monkeypatch.delenv(key, raising=False)
        allowed = created.build_source(
            chat_id="thread:456",
            chat_type="dm",
            user_id="authorized@example.com",
        )
        denied = created.build_source(
            chat_id="thread:789",
            chat_type="dm",
            user_id="outsider@example.com",
        )

        assert runner._is_user_authorized(allowed) is True
        assert runner._is_user_authorized(denied) is False

        restored: list[object] = []

        async def capture_restored(message) -> None:
            restored.append(message)

        created.handle_message = capture_restored
        queued = type("QueuedEvent", (), {"source": allowed})()
        runner._startup_restore_queue = [queued]
        drained = asyncio.run(GatewayRunner._drain_startup_restore_queue(runner))

        assert drained == 1
        assert restored == [queued]
        assert runner._startup_restore_queue == []
    finally:
        platform_registry.unregister("hey", scope=None)
        if previous_registration is not None:
            platform_registry.register(previous_registration)


@pytest.mark.parametrize(
    "override",
    [
        {"account": "01"},
        {"account": "9223372036854775808"},
        {"account": "9" * 10_000},
        {"account": 0},
        {"account": 12345},
        {"own_email": "not-an-email"},
        {"allow_from": "authorized@example.com"},
        {"allow_from": ["authorized@example.com", 7]},
        {"allow_from": "authorized@example.com", "allow_all_users": True},
        {"poll_interval": "0s"},
        {"poll_interval": "eventually"},
        {"watch_failure_threshold": 0},
        {"watch_failure_threshold": 101},
    ],
)
def test_validation_rejects_malformed_values_without_throwing(monkeypatch, override) -> None:
    monkeypatch.setattr(plugin_module, "check_requirements", lambda: True)
    extra = {
        "account": "12345",
        "own_email": "agent@example.com",
        "allow_from": ["authorized@example.com"],
        "poll_interval": "1s",
        "watch_failure_threshold": 5,
        **override,
    }
    config = type("Config", (), {"enabled": True, "extra": extra})()

    assert plugin_module.validate_config(config) is False


def test_validation_accepts_exact_go_int64_maximum(monkeypatch) -> None:
    monkeypatch.setattr(plugin_module, "check_requirements", lambda: True)
    config = type(
        "Config",
        (),
        {
            "enabled": True,
            "extra": {
                "account": "9223372036854775807",
                "own_email": "agent@example.com",
                "allow_from": ["authorized@example.com"],
            },
        },
    )()

    assert plugin_module.validate_config(config) is True


def test_validation_returns_false_for_non_mapping_extra(monkeypatch) -> None:
    monkeypatch.setattr(plugin_module, "check_requirements", lambda: True)
    config = type("Config", (), {"enabled": True, "extra": ["invalid"]})()

    assert plugin_module.validate_config(config) is False


def test_interactive_setup_uses_real_hey_cli_flag(capsys) -> None:
    plugin_module.interactive_setup()
    output = capsys.readouterr().out
    assert "hey auth login --no-browser" in output
    assert "HEY CLI v1.1.0" in output
    assert "curl -fsSL https://hey.com/install-cli | bash" not in output
    assert "--remote" not in output


def test_root_filesystem_plugin_shim_exists() -> None:
    root_init = Path(__file__).parents[1] / "__init__.py"
    assert root_init.is_file()
    assert "register" in root_init.read_text()


def test_wheel_entrypoint_is_lazy_package_module() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert (
        project["project"]["entry-points"]["hermes_agent.plugins"]["hey-platform"]
        == "hey_platform"
    )


def test_ci_builds_and_tests_pinned_go_sidecar() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/tests.yml").read_text()

    assert "actions/setup-go@924ae3a1cded613372ab5595356fb5720e22ba16 # v6" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert 'go-version: "1.26.7"' in workflow
    assert "go test -race ./..." in workflow
    assert "go vet ./..." in workflow
    assert "go build -o /tmp/hermes-hey-sidecar ." in workflow
    assert 'echo /tmp >> "$GITHUB_PATH"' in workflow
    assert "uv sync --frozen --extra dev" in workflow
    assert "Build wheel again from the source distribution" in workflow
    assert "Test and build Go sidecar from the source distribution" in workflow
    assert "Verify wheel and source-distribution parity" in workflow
    assert "direct_payload == rebuilt_payload" in workflow
    assert "Install and import both distribution formats" in workflow


def test_readme_uses_exact_checksum_entry() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text()

    assert 'sha256sum -c <(grep -Fx "$HEY_CLI_SHA256  $HEY_CLI_ARCHIVE" checksums.txt)' in readme
    assert "hermes config set --force platforms.hey" in readme
    assert '"account":"12345"' in readme
    assert "hermes config set platforms.hey.account" not in readme
