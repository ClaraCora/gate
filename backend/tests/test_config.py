from __future__ import annotations

import json
from pathlib import Path

import pytest
from gate.config import GateSettings, SocksAuthConfig, load_settings
from pydantic import ValidationError


def test_loads_project_example_config() -> None:
    settings = load_settings()

    assert settings.api.listen == "127.0.0.1"
    assert settings.discovery.fallback_urls == (
        "https://r.jina.ai/http://www.vpngate.net/api/iphone/",
    )
    assert settings.automation.health_interval_seconds == 120
    assert [region.socks_port for region in settings.regions[:5]] == [
        11081,
        11082,
        11083,
        11084,
        11085,
    ]
    japan_entries = [region for region in settings.regions if region.group_id == "jp"]
    assert len(japan_entries) == 10
    assert [region.enabled for region in japan_entries] == [True] + [False] * 9


def test_rejects_duplicate_socks_ports() -> None:
    raw = load_settings().model_dump()
    raw["regions"][1]["socks_port"] = raw["regions"][0]["socks_port"]

    with pytest.raises(ValidationError, match="SOCKS ports must be unique"):
        GateSettings.model_validate(raw)


def test_loads_socks_auth_from_separate_file_without_using_yaml(tmp_path: Path) -> None:
    auth_path = tmp_path / "socks-auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "username": "gate_user",
                "password": "strong!proxy#password",
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings(socks_auth_path=auth_path)

    assert settings.socks_auth.enabled is True
    assert settings.socks_auth.username == "gate_user"
    assert settings.socks_auth.password == "strong!proxy#password"


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("ab", "strong!proxy#password", "username"),
        ("gate_user", "too-short", "12-128"),
        ("gate_user", "密码密码密码密码密码密码", "visible ASCII"),
    ],
)
def test_rejects_invalid_socks_credentials(username: str, password: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SocksAuthConfig(enabled=True, username=username, password=password)


def test_disabled_socks_auth_cannot_retain_credentials() -> None:
    with pytest.raises(ValidationError, match="must not retain"):
        SocksAuthConfig(enabled=False, username="gate_user", password="strong!proxy#password")
