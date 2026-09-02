from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ApiConfig(BaseModel):
    listen: str = "127.0.0.1"
    port: int = Field(default=18080, ge=1, le=65535)


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///./gate.db"


class DiscoveryConfig(BaseModel):
    url: str = "https://www.vpngate.net/api/iphone/"
    fallback_urls: tuple[str, ...] = ("https://r.jina.ai/http://www.vpngate.net/api/iphone/",)
    interval_minutes: int = Field(default=10, ge=1, le=1440)
    top_k_per_region: int = Field(default=5, ge=1, le=20)


class SelectionConfig(BaseModel):
    improvement_ratio: float = Field(default=1.15, ge=1.0, le=3.0)
    confirmation_rounds: int = Field(default=2, ge=1, le=10)
    switch_cooldown_minutes: int = Field(default=30, ge=0, le=1440)
    active_failure_threshold: int = Field(default=3, ge=1, le=20)


class AutomationConfig(BaseModel):
    enabled: bool = True
    health_interval_seconds: int = Field(default=120, ge=30, le=3600)
    optimization_interval_minutes: int = Field(default=30, ge=5, le=1440)
    max_candidates_per_cycle: int = Field(default=3, ge=1, le=10)


class SecurityConfig(BaseModel):
    enabled: bool = True
    session_hours: int = Field(default=12, ge=1, le=168)
    cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:18080",
        "http://localhost:18080",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for origin in origins:
            value = origin.rstrip("/")
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed origins must be absolute HTTP(S) origins")
            normalized.append(value)
        if not normalized:
            raise ValueError("at least one allowed origin is required")
        return tuple(dict.fromkeys(normalized))


class SocksAuthConfig(BaseModel):
    enabled: bool = False
    username: str = ""
    password: str = ""

    @model_validator(mode="after")
    def validate_credentials(self) -> SocksAuthConfig:
        if not self.enabled:
            if self.username or self.password:
                raise ValueError("disabled SOCKS authentication must not retain credentials")
            return self
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", self.username):
            raise ValueError(
                "SOCKS username must be 3-32 ASCII letters, numbers, dots, underscores, or hyphens"
            )
        if not 12 <= len(self.password) <= 128:
            raise ValueError("SOCKS password must be 12-128 characters")
        if any(ord(character) < 33 or ord(character) > 126 for character in self.password):
            raise ValueError("SOCKS password must contain visible ASCII characters only")
        return self


class RetentionConfig(BaseModel):
    observations_days: int = Field(default=7, ge=1, le=365)
    probes_days: int = Field(default=7, ge=1, le=365)
    completed_jobs_days: int = Field(default=30, ge=1, le=365)
    events_days: int = Field(default=180, ge=1, le=3650)


class RegionConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,15}$")
    group_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,15}$")
    name: str = Field(min_length=1, max_length=80)
    countries: tuple[str, ...]
    socks_port: int = Field(ge=1, le=65535)
    network_index: int = Field(ge=1, le=8192)
    enabled: bool = True

    @field_validator("countries")
    @classmethod
    def normalize_countries(cls, countries: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(country.upper() for country in countries))
        if not normalized or any(
            len(country) != 2 or not country.isalpha() for country in normalized
        ):
            raise ValueError("countries must contain two-letter country codes")
        return normalized

    @model_validator(mode="after")
    def default_group_id(self) -> RegionConfig:
        if self.group_id is None:
            self.group_id = self.id
        return self


class GateSettings(BaseModel):
    api: ApiConfig = ApiConfig()
    database: DatabaseConfig = DatabaseConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    selection: SelectionConfig = SelectionConfig()
    automation: AutomationConfig = AutomationConfig()
    security: SecurityConfig = SecurityConfig()
    socks_auth: SocksAuthConfig = SocksAuthConfig()
    retention: RetentionConfig = RetentionConfig()
    regions: tuple[RegionConfig, ...]

    @model_validator(mode="after")
    def reject_duplicate_regions_and_ports(self) -> GateSettings:
        ids = [region.id for region in self.regions]
        ports = [region.socks_port for region in self.regions]
        network_indexes = [region.network_index for region in self.regions]
        if len(ids) != len(set(ids)):
            raise ValueError("region IDs must be unique")
        if len(ports) != len(set(ports)):
            raise ValueError("SOCKS ports must be unique")
        if len(network_indexes) != len(set(network_indexes)):
            raise ValueError("region network indexes must be unique")
        group_countries: dict[str, tuple[str, ...]] = {}
        for region in self.regions:
            assert region.group_id is not None
            existing = group_countries.setdefault(region.group_id, region.countries)
            if existing != region.countries:
                raise ValueError("entries in the same region group must use identical countries")
        return self


def default_config_path() -> Path:
    configured = os.environ.get("GATE_CONFIG")
    if configured:
        return Path(configured)
    local = Path("config/gate.yaml")
    return local if local.exists() else Path("config/gate.example.yaml")


def default_socks_auth_path() -> Path:
    return Path(os.environ.get("GATE_SOCKS_AUTH_FILE", "/etc/gate/socks-auth.json"))


def load_settings(
    path: Path | None = None,
    *,
    socks_auth_path: Path | None = None,
) -> GateSettings:
    config_path = path or default_config_path()
    with config_path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError(f"Gate config must be a mapping: {config_path}")
    auth_path = socks_auth_path or default_socks_auth_path()
    raw.pop("socks_auth", None)
    if auth_path.exists():
        try:
            auth_raw = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid SOCKS authentication file: {auth_path}") from exc
        if not isinstance(auth_raw, dict):
            raise ValueError(f"SOCKS authentication file must be a mapping: {auth_path}")
        raw["socks_auth"] = auth_raw
    else:
        raw["socks_auth"] = {}
    auth_override = os.environ.get("GATE_AUTH_ENABLED")
    if auth_override is not None:
        normalized = auth_override.strip().lower()
        if normalized not in {"0", "1", "false", "true"}:
            raise ValueError("GATE_AUTH_ENABLED must be true or false")
        security = raw.setdefault("security", {})
        if not isinstance(security, dict):
            raise ValueError("security config must be a mapping")
        security["enabled"] = normalized in {"1", "true"}
    return GateSettings.model_validate(raw)
