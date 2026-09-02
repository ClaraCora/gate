from __future__ import annotations

from collections.abc import Iterable

from gate.domain import RegionDefinition, VpnGateNode

DEFAULT_REGIONS = (
    RegionDefinition("jp", "Japan", frozenset({"JP"}), 11081),
    RegionDefinition("kr", "Korea", frozenset({"KR"}), 11082),
    RegionDefinition("na", "North America", frozenset({"US", "CA"}), 11083),
    RegionDefinition(
        "eu",
        "Europe",
        frozenset({"DE", "NL", "FR", "GB", "RO", "PL", "ES", "IT", "SE", "FI", "CH", "AT"}),
        11084,
    ),
    RegionDefinition(
        "sea",
        "Southeast Asia",
        frozenset({"SG", "TH", "VN", "ID", "MY", "PH"}),
        11085,
    ),
)


def nodes_for_region(
    nodes: Iterable[VpnGateNode], region: RegionDefinition
) -> tuple[VpnGateNode, ...]:
    return tuple(node for node in nodes if node.country_code in region.countries)


def region_for_port(port: int) -> RegionDefinition | None:
    return next((region for region in DEFAULT_REGIONS if region.socks_port == port), None)
