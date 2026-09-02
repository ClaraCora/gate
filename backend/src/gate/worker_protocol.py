from __future__ import annotations

import hashlib
import ipaddress
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from gate.config import SocksAuthConfig
from gate.domain import Transport


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthRequest(WorkerRequest):
    action: Literal["health"]


class InspectRequest(WorkerRequest):
    action: Literal["inspect"]


class ProvisionSlotRequest(WorkerRequest):
    action: Literal["provision_slot"]
    region_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,15}$")
    slot: Literal["a", "b"]
    remote_ip: str
    remote_port: int = Field(ge=1, le=65535)
    transport: Transport
    profile_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_text: str = Field(min_length=1, max_length=262_144)

    @field_validator("remote_ip")
    @classmethod
    def require_global_ipv4(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if address.version != 4 or not address.is_global:
            raise ValueError("remote_ip must be a global IPv4 address")
        return str(address)

    @model_validator(mode="after")
    def verify_fingerprint(self) -> ProvisionSlotRequest:
        fingerprint = hashlib.sha256(self.config_text.encode("utf-8")).hexdigest()
        if fingerprint != self.profile_fingerprint:
            raise ValueError("config_text does not match profile_fingerprint")
        return self


class DestroySlotRequest(WorkerRequest):
    action: Literal["destroy_slot"]
    region_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,15}$")
    slot: Literal["a", "b"]


class UpdateSocksAuthRequest(WorkerRequest):
    action: Literal["update_socks_auth"]
    enabled: bool
    username: str = Field(default="", max_length=32)
    password: str = Field(default="", max_length=128)
    listen: Literal["127.0.0.1", "0.0.0.0"] = "127.0.0.1"

    @model_validator(mode="after")
    def validate_credentials(self) -> UpdateSocksAuthRequest:
        SocksAuthConfig(
            enabled=self.enabled,
            username=self.username,
            password=self.password,
            listen=self.listen,
        )
        return self


Request = Annotated[
    HealthRequest
    | InspectRequest
    | ProvisionSlotRequest
    | DestroySlotRequest
    | UpdateSocksAuthRequest,
    Field(discriminator="action"),
]
REQUEST_ADAPTER: TypeAdapter[Request] = TypeAdapter(Request)


class WorkerErrorResponse(BaseModel):
    code: str
    message: str


class WorkerResponse(BaseModel):
    ok: bool
    data: dict[str, object] | None = None
    error: WorkerErrorResponse | None = None
