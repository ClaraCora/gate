from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Transport(StrEnum):
    UDP = "udp"
    TCP = "tcp"


class RegionMode(StrEnum):
    AUTO = "auto"
    LOCKED = "locked"
    DISABLED = "disabled"


class RegionStatus(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    SWITCHING = "switching"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class VpnGateNode:
    hostname: str
    ip: str
    country_long: str
    country_code: str
    score: int
    ping_ms: int | None
    speed_bps: int
    sessions: int
    uptime_ms: int
    total_users: int
    total_traffic_bytes: int
    log_type: str
    operator: str
    message: str
    openvpn_config_base64: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FeedParseResult:
    nodes: tuple[VpnGateNode, ...]
    rejected_rows: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SanitizedProfile:
    remote_ip: str
    remote_port: int
    transport: Transport
    config_text: str = field(repr=False)
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RegionDefinition:
    id: str
    name: str
    countries: frozenset[str]
    socks_port: int


@dataclass(frozen=True, slots=True)
class ProbeMetrics:
    availability_24h: float
    latency_ms: float
    throughput_mbps: float
    api_speed_bps: int
    uptime_ms: int
    sessions: int


@dataclass(frozen=True, slots=True)
class QualityBreakdown:
    availability: float
    latency: float
    throughput: float
    api_speed: float
    uptime: float
    load: float
    total: float


@dataclass(frozen=True, slots=True)
class SwitchDecision:
    should_switch: bool
    reason: str
    required_score: float
