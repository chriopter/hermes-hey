from __future__ import annotations

import tomllib
from pathlib import Path

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


def test_interactive_setup_uses_real_hey_cli_flag(capsys) -> None:
    plugin_module.interactive_setup()
    output = capsys.readouterr().out
    assert "hey auth login --no-browser" in output
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
