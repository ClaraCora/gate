from __future__ import annotations

import pytest
from gate.config import GateSettings, load_settings
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
