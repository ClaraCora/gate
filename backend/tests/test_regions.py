from __future__ import annotations

from gate.domain import VpnGateNode
from gate.regions import DEFAULT_REGIONS, nodes_for_region, region_for_port


def _node(country_code: str) -> VpnGateNode:
    return VpnGateNode(
        hostname=f"vpn-{country_code.lower()}",
        ip="128.211.249.131",
        country_long=country_code,
        country_code=country_code,
        score=1,
        ping_ms=10,
        speed_bps=1,
        sessions=1,
        uptime_ms=1,
        total_users=1,
        total_traffic_bytes=1,
        log_type="",
        operator="",
        message="",
        openvpn_config_base64="unused",
    )


def test_region_country_matching_is_strict() -> None:
    europe = next(region for region in DEFAULT_REGIONS if region.id == "eu")
    matches = nodes_for_region([_node("DE"), _node("US"), _node("RO")], europe)

    assert [node.country_code for node in matches] == ["DE", "RO"]


def test_finds_region_by_stable_port() -> None:
    assert region_for_port(11081).id == "jp"  # type: ignore[union-attr]
    assert region_for_port(9999) is None
