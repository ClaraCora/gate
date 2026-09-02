from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from gate.database import Database, NodeRecord, RegionRecord, RegionSlotRecord, utc_now
from gate.discovery import DiscoveryService
from gate.domain import RegionMode, RegionStatus
from gate.errors import GateError
from gate.haproxy import HaProxyRuntime
from gate.probes import EgressProbe, probe_socks_exit
from gate.worker_client import WorkerClient
from gate.worker_protocol import DestroySlotRequest, InspectRequest, ProvisionSlotRequest, Request


class SwitchError(GateError):
    code = "SWITCH_FAILED"


class WorkerGateway(Protocol):
    async def request(self, request: Request) -> dict[str, object]: ...


class HaProxyGateway(Protocol):
    async def ready(self, region_id: str, slot: str) -> None: ...

    async def drain(self, region_id: str, slot: str) -> None: ...

    async def disable(self, region_id: str, slot: str) -> None: ...


ProbeCallable = Callable[..., Awaitable[EgressProbe]]
ProgressCallable = Callable[[float, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SwitchResult:
    region_id: str
    node_id: int
    slot: str
    egress_ip: str
    country_code: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class CandidateProbeResult:
    region_id: str
    node_id: int
    slot: str
    egress_ip: str
    country_code: str
    latency_ms: float


class SwitchCoordinator:
    def __init__(
        self,
        database: Database,
        discovery: DiscoveryService,
        *,
        worker: WorkerGateway | None = None,
        haproxy: HaProxyGateway | None = None,
        probe: ProbeCallable = probe_socks_exit,
        drain_seconds: float = 180.0,
    ) -> None:
        self.database = database
        self.discovery = discovery
        self.worker = worker or WorkerClient()
        self.haproxy = haproxy or HaProxyRuntime()
        self.probe = probe
        self.drain_seconds = drain_seconds
        self._region_locks: dict[str, asyncio.Lock] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def _destroy_slot(self, region_id: str, slot: Literal["a", "b"]) -> None:
        await self.haproxy.disable(region_id, slot)
        await self.worker.request(
            DestroySlotRequest(action="destroy_slot", region_id=region_id, slot=slot)
        )
        await self.database.mark_slot_empty(region_id, slot)

    def _schedule_drain_cleanup(self, region_id: str, slot: Literal["a", "b"]) -> None:
        task = asyncio.create_task(self._cleanup_draining_slot(region_id, slot))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _cleanup_draining_slot(self, region_id: str, slot: Literal["a", "b"]) -> None:
        await asyncio.sleep(self.drain_seconds)
        lock = self._region_locks.setdefault(region_id, asyncio.Lock())
        async with lock:
            record = await self.database.get_slot(region_id, slot)
            if record is None or record.state != "draining":
                return
            try:
                await self._destroy_slot(region_id, slot)
            except GateError as exc:
                await self.database.add_event(
                    code="DRAIN_CLEANUP_FAILED",
                    level="error",
                    message=f"Failed to clean drained slot {region_id}/{slot}",
                    region_id=region_id,
                    node_id=record.node_id,
                    details={"slot": slot, "error_code": exc.code},
                )
                return
            await self.database.add_event(
                code="DRAIN_COMPLETED",
                message=f"Drained slot {region_id}/{slot} was cleaned",
                region_id=region_id,
                node_id=record.node_id,
                details={"slot": slot},
            )

    async def reconcile(self) -> None:
        worker_data = await self.worker.request(InspectRequest(action="inspect"))
        raw_slots = worker_data.get("slots")
        if not isinstance(raw_slots, list):
            raise SwitchError("gate-worker returned an invalid slot inventory")
        actual_ready: dict[tuple[str, str], bool] = {}
        actual_exists: dict[tuple[str, str], bool] = {}
        for item in raw_slots:
            if not isinstance(item, dict):
                continue
            region_id = item.get("region_id")
            slot = item.get("slot")
            if not isinstance(region_id, str) or slot not in {"a", "b"}:
                continue
            actual_exists[(region_id, slot)] = item.get("exists") is True
            actual_ready[(region_id, slot)] = all(
                item.get(field) is True
                for field in ("exists", "tunnel_up", "openvpn_active", "socks_active")
            )

        for record in await self.database.list_slots():
            key = (record.region_id, record.slot)
            slot = cast(Literal["a", "b"], record.slot)
            if record.state == "active" and actual_ready.get(key, False):
                region = await self.database.get_region(record.region_id)
                if region is None:
                    await self._destroy_slot(record.region_id, slot)
                    continue
                try:
                    probe = await self.probe(
                        record.backend_address,
                        1080,
                        expected_countries=set(region.countries),
                    )
                except Exception as exc:
                    await self.haproxy.disable(record.region_id, record.slot)
                    with suppress(GateError):
                        await self.worker.request(
                            DestroySlotRequest(
                                action="destroy_slot",
                                region_id=record.region_id,
                                slot=cast(Literal["a", "b"], record.slot),
                            )
                        )
                    await self.database.mark_slot_empty(record.region_id, record.slot)
                    error_code = (
                        exc.code if isinstance(exc, GateError) else "RECONCILE_PROBE_FAILED"
                    )
                    await self.database.add_event(
                        code="RECONCILE_REJECTED_ACTIVE_SLOT",
                        level="error",
                        message=f"{region.name} active slot failed startup verification",
                        region_id=record.region_id,
                        node_id=record.node_id,
                        details={"slot": record.slot, "error_code": error_code},
                    )
                    continue
                await self.haproxy.ready(record.region_id, record.slot)
                await self.database.add_event(
                    code="RECONCILE_VERIFIED_ACTIVE_SLOT",
                    message=f"{region.name} active slot was verified after startup",
                    region_id=record.region_id,
                    node_id=record.node_id,
                    details={
                        "slot": record.slot,
                        "egress_ip": probe.egress_ip,
                        "country_code": probe.country_code,
                    },
                )
            else:
                if record.state != "empty" or actual_exists.get(key, False):
                    await self._destroy_slot(record.region_id, slot)
                else:
                    await self.haproxy.disable(record.region_id, record.slot)

    async def set_region_mode(self, region_id: str, mode: RegionMode) -> None:
        region = await self.database.get_region(region_id)
        if region is None:
            raise SwitchError(f"unknown region: {region_id}")
        if mode == RegionMode.DISABLED:
            for slot in ("a", "b"):
                await self.haproxy.disable(region_id, slot)
                await self.worker.request(
                    DestroySlotRequest(action="destroy_slot", region_id=region_id, slot=slot)
                )
                await self.database.mark_slot_empty(region_id, slot)
        await self.database.set_region_mode(region_id, mode)

    async def _load(self, region_id: str, node_id: int) -> tuple[RegionRecord, NodeRecord]:
        region = await self.database.get_region(region_id)
        node = await self.database.get_node(node_id)
        if region is None:
            raise SwitchError(f"unknown region: {region_id}")
        if node is None or node.country_code not in region.countries:
            raise SwitchError("candidate does not belong to the requested region")
        return region, node

    @staticmethod
    def _target_slot(active: RegionSlotRecord | None) -> Literal["a", "b"]:
        return "b" if active is not None and active.slot == "a" else "a"

    async def switch(
        self,
        region_id: str,
        node_id: int,
        *,
        progress: ProgressCallable | None = None,
    ) -> SwitchResult:
        lock = self._region_locks.setdefault(region_id, asyncio.Lock())
        if lock.locked():
            raise SwitchError(f"a switch is already running for region: {region_id}")
        async with lock:
            return await self._switch(region_id, node_id, progress=progress)

    async def probe_candidate(
        self,
        region_id: str,
        node_id: int,
        *,
        progress: ProgressCallable | None = None,
    ) -> CandidateProbeResult:
        lock = self._region_locks.setdefault(region_id, asyncio.Lock())
        if lock.locked():
            raise SwitchError(f"an operation is already running for region: {region_id}")
        async with lock:
            return await self._probe_candidate(region_id, node_id, progress=progress)

    async def _probe_candidate(
        self,
        region_id: str,
        node_id: int,
        *,
        progress: ProgressCallable | None = None,
    ) -> CandidateProbeResult:
        async def report(value: float, message: str) -> None:
            if progress is not None:
                await progress(value, message)

        region, node = await self._load(region_id, node_id)
        profile = self.discovery.profiles.get(node.fingerprint)
        if profile is None:
            raise SwitchError("candidate profile is not cached; refresh discovery and retry")
        active = await self.database.get_active_slot(region_id)
        target_slot = self._target_slot(active)
        started_at = utc_now()
        await report(0.05, "Preparing an isolated candidate slot")
        try:
            await self._destroy_slot(region_id, target_slot)
            worker_data = await self.worker.request(
                ProvisionSlotRequest(
                    action="provision_slot",
                    region_id=region_id,
                    slot=target_slot,
                    remote_ip=profile.remote_ip,
                    remote_port=profile.remote_port,
                    transport=profile.transport,
                    profile_fingerprint=profile.fingerprint,
                    config_text=profile.config_text,
                )
            )
            namespace_ip = worker_data.get("namespace_ip")
            if not isinstance(namespace_ip, str):
                raise SwitchError("gate-worker did not return the slot address")
            await report(0.55, "Testing HTTPS, DNS, exit IP, and country")
            probe = await self.probe(
                namespace_ip,
                1080,
                expected_countries=set(region.countries),
            )
            await self.database.record_probe(
                region_id=region_id,
                node_id=node_id,
                probe_type="candidate",
                result="succeeded",
                egress_ip=probe.egress_ip,
                country_code=probe.country_code,
                latency_ms=probe.latency_ms,
                started_at=started_at,
            )
            await self.database.add_event(
                code="CANDIDATE_PROBE_COMPLETED",
                message=f"{region.name} candidate {node.ip} verified as {probe.egress_ip}",
                region_id=region_id,
                node_id=node_id,
                details={
                    "slot": target_slot,
                    "egress_ip": probe.egress_ip,
                    "country_code": probe.country_code,
                    "latency_ms": probe.latency_ms,
                },
            )
            await report(0.9, "Candidate verified; cleaning the temporary slot")
            return CandidateProbeResult(
                region_id=region_id,
                node_id=node_id,
                slot=target_slot,
                egress_ip=probe.egress_ip,
                country_code=probe.country_code,
                latency_ms=probe.latency_ms,
            )
        except Exception as exc:
            error_code = exc.code if isinstance(exc, GateError) else "UNEXPECTED_PROBE_ERROR"
            await self.database.record_probe(
                region_id=region_id,
                node_id=node_id,
                probe_type="candidate",
                result="failed",
                error_code=error_code,
                started_at=started_at,
            )
            await self.database.add_event(
                code="CANDIDATE_PROBE_FAILED",
                level="warning",
                message=f"{region.name} candidate {node.ip} failed validation",
                region_id=region_id,
                node_id=node_id,
                details={"slot": target_slot, "error_code": error_code},
            )
            raise
        finally:
            with suppress(GateError):
                await self.worker.request(
                    DestroySlotRequest(
                        action="destroy_slot",
                        region_id=region_id,
                        slot=target_slot,
                    )
                )
            await self.database.mark_slot_empty(region_id, target_slot)

    async def _switch(
        self,
        region_id: str,
        node_id: int,
        *,
        progress: ProgressCallable | None = None,
    ) -> SwitchResult:
        async def report(value: float, message: str) -> None:
            if progress is not None:
                await progress(value, message)

        region, node = await self._load(region_id, node_id)
        profile = self.discovery.profiles.get(node.fingerprint)
        if profile is None:
            raise SwitchError("candidate profile is not cached; refresh discovery and retry")
        active = await self.database.get_active_slot(region_id)
        previous_status = RegionStatus(region.status)
        old_route_usable = active is not None and previous_status in {
            RegionStatus.HEALTHY,
            RegionStatus.DEGRADED,
        }
        target_slot = self._target_slot(active)
        target_enabled = False

        await self.database.set_region_status(region_id, RegionStatus.SWITCHING)
        await report(0.05, "Preparing the inactive slot")
        try:
            await self._destroy_slot(region_id, target_slot)
            worker_data = await self.worker.request(
                ProvisionSlotRequest(
                    action="provision_slot",
                    region_id=region_id,
                    slot=target_slot,
                    remote_ip=profile.remote_ip,
                    remote_port=profile.remote_port,
                    transport=profile.transport,
                    profile_fingerprint=profile.fingerprint,
                    config_text=profile.config_text,
                )
            )
            namespace_ip = worker_data.get("namespace_ip")
            if not isinstance(namespace_ip, str):
                raise SwitchError("gate-worker did not return the slot address")
            await report(0.45, "Testing the candidate through its isolated SOCKS endpoint")
            direct_probe = await self.probe(
                namespace_ip,
                1080,
                expected_countries=set(region.countries),
            )

            await self.haproxy.ready(region_id, target_slot)
            target_enabled = True
            if active is not None:
                await self.haproxy.drain(region_id, active.slot)
            await report(0.75, "Verifying the stable regional port")
            stable_probe = await self.probe(
                "127.0.0.1",
                region.socks_port,
                expected_countries=set(region.countries),
            )
            if stable_probe.egress_ip != direct_probe.egress_ip:
                raise SwitchError("stable SOCKS port reached a different exit than the candidate")

            await self.database.complete_switch(region_id, target_slot, node_id)
            if active is not None:
                self._schedule_drain_cleanup(
                    region_id,
                    cast(Literal["a", "b"], active.slot),
                )
            await self.database.add_event(
                code="SWITCH_COMPLETED",
                message=f"{region.name} switched to {stable_probe.egress_ip}",
                details={
                    "slot": target_slot,
                    "node_id": node_id,
                    "egress_ip": stable_probe.egress_ip,
                    "country_code": stable_probe.country_code,
                    "latency_ms": stable_probe.latency_ms,
                },
            )
            await report(1.0, "Switch completed")
            return SwitchResult(
                region_id=region_id,
                node_id=node_id,
                slot=target_slot,
                egress_ip=stable_probe.egress_ip,
                country_code=stable_probe.country_code,
                latency_ms=stable_probe.latency_ms,
            )
        except Exception as exc:
            if target_enabled:
                await self.haproxy.disable(region_id, target_slot)
            if active is not None:
                if old_route_usable:
                    await self.haproxy.ready(region_id, active.slot)
                else:
                    await self.haproxy.disable(region_id, active.slot)
            with suppress(GateError):
                await self.worker.request(
                    DestroySlotRequest(
                        action="destroy_slot",
                        region_id=region_id,
                        slot=target_slot,
                    )
                )
            await self.database.mark_slot_empty(region_id, target_slot)
            await self.database.set_region_status(region_id, previous_status)
            error_code = exc.code if isinstance(exc, GateError) else "UNEXPECTED_SWITCH_ERROR"
            await self.database.add_event(
                code="SWITCH_ROLLED_BACK",
                level="error",
                message=f"{region.name} switch failed and was rolled back",
                details={"slot": target_slot, "node_id": node_id, "error_code": error_code},
            )
            raise
