from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok", "not_ready"]


class MetaResponse(BaseModel):
    name: str = "Gate"
    version: str
    api_version: str = "v1"


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    authenticated: bool
    csrf_token: str | None = None
    expires_at: datetime | None = None


class RegionResponse(BaseModel):
    id: str
    name: str
    countries: list[str]
    socks_port: int
    enabled: bool
    mode: str
    status: str
    active_node_id: int | None
    candidate_count: int
    updated_at: datetime


class RegionModeRequest(BaseModel):
    mode: Literal["auto", "locked", "disabled"]


class SlotRuntimeResponse(BaseModel):
    region_id: str
    slot: Literal["a", "b"]
    namespace: str
    namespace_ip: str
    exists: bool
    tunnel_up: bool
    openvpn_active: bool
    socks_active: bool


class CandidateResponse(BaseModel):
    id: int
    hostname: str
    ip: str
    country_code: str
    country_long: str
    transport: str
    port: int
    api_score: int
    api_ping_ms: int | None
    api_speed_bps: int
    sessions: int
    uptime_ms: int
    log_type: str
    operator: str
    last_seen_at: datetime
    availability_24h: float | None = None
    measured_latency_ms: float | None = None
    measured_throughput_mbps: float | None = None
    quality_score: float | None = None


class DiscoveryResponse(BaseModel):
    discovered: int
    accepted: int
    rejected_feed_rows: int
    rejected_profiles: int
    warnings: list[str]
    observed_at: datetime
    source_url: str


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    level: str
    message: str
    details: dict[str, Any]
    created_at: datetime


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    status: str
    region_id: str | None
    progress: float
    error_code: str | None
    detail: dict[str, Any]
    created_at: datetime
    updated_at: datetime
