from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx

from gate.config import GateSettings
from gate.database import Database, utc_now
from gate.errors import GateError
from gate.http_usage import bounded_get, usage_recorder
from gate.probes import (
    DetectorError,
    EgressProbe,
    ProbeError,
    UnconfirmedRouteError,
    parse_cloudflare_trace,
    probe_socks_exit,
)
from gate.worker_protocol import InspectRequest, Request, TrafficRequest


class TrafficWorker(Protocol):
    async def request(self, request: Request) -> dict[str, object]: ...


class NoisyCandidateError(ProbeError):
    code = "NOISY_CANDIDATE"


class MonitoringService:
    def __init__(self, settings: GateSettings, database: Database, worker: TrafficWorker) -> None:
        self.settings = settings
        self.database = database
        self.worker = worker
        self._condition = asyncio.Condition()
        self._running_probes = 0
        self._collect_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._controls_verified_at = 0.0
        self.on_noise: Callable[[str], Awaitable[None]] | None = None

    @asynccontextmanager
    async def measure(self, purpose: str, region_id: str | None = None) -> AsyncIterator[None]:
        if usage_recorder.get() is not None:
            yield
            return

        async def record(size: int) -> None:
            await self.database.record_traffic(
                scope="http",
                source=purpose,
                region_id=region_id,
                rx_bytes=size,
                requests=1,
            )

        token = usage_recorder.set(record)
        try:
            yield
        finally:
            usage_recorder.reset(token)

    async def probe(
        self,
        host: str,
        port: int,
        *,
        purpose: str = "verification",
        region_id: str | None = None,
        **kwargs: Any,
    ) -> EgressProbe:
        if region_id is None:
            region_id = next((r.id for r in self.settings.regions if r.socks_port == port), None)
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._running_probes < self.settings.monitoring.max_concurrent_probes
            )
            self._running_probes += 1
        try:
            kwargs.setdefault("timeout_seconds", self.settings.monitoring.probe_timeout_seconds)
            async with self.measure(purpose, region_id):
                try:
                    return await probe_socks_exit(host, port, **kwargs)
                except ProbeError as exc:
                    if isinstance(exc, DetectorError) and not isinstance(
                        exc, UnconfirmedRouteError
                    ):
                        raise
                    if exc.code != "PROBE_FAILED" and not isinstance(exc, UnconfirmedRouteError):
                        raise
                    await self._confirm_route_failure(host, port, region_id, kwargs, exc)
                    raise
        finally:
            async with self._condition:
                self._running_probes -= 1
                self._condition.notify_all()

    async def validate_connection_failure(self, exc: Exception) -> Exception:
        if not isinstance(exc, GateError) or exc.code != "TUNNEL_CONNECT_FAILED":
            return exc
        async with self.measure("failure_control"):
            if not await self._host_controls_available():
                return DetectorError("host network cannot confirm a candidate connection failure")
        return exc

    async def _host_controls_available(self) -> bool:
        async with self._control_lock:
            if self._controls_verified_at and time.monotonic() - self._controls_verified_at < 30:
                return True
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                try:
                    async with asyncio.timeout(6):
                        trace_body = await bounded_get(
                            client, "https://www.cloudflare.com/cdn-cgi/trace", limit=16_384
                        )
                        ipify_body = await bounded_get(
                            client, "https://api.ipify.org?format=json", limit=16_384
                        )
                    addresses = (
                        parse_cloudflare_trace(trace_body.decode()).get("ip", ""),
                        str(json.loads(ipify_body)["ip"]),
                    )
                    if not all(ipaddress.ip_address(ip).is_global for ip in addresses):
                        return False
                except (httpx.HTTPError, OSError, TimeoutError, ValueError, KeyError, TypeError):
                    return False
            self._controls_verified_at = time.monotonic()
            return True

    async def _confirm_route_failure(
        self,
        host: str,
        port: int,
        region_id: str | None,
        kwargs: dict[str, Any],
        original: ProbeError,
    ) -> None:
        try:
            inventory = await self.worker.request(InspectRequest(action="inspect"))
            active = await self.database.get_active_slot(region_id) if region_id else None
        except (GateError, ValueError) as exc:
            raise DetectorError(
                "worker state is unavailable; route failure is unconfirmed"
            ) from exc
        slots = inventory.get("slots")
        slot = (
            next(
                (
                    row
                    for row in slots
                    if isinstance(row, dict)
                    and (
                        (port == 1080 and row.get("namespace_ip") == host)
                        or (
                            active is not None
                            and row.get("region_id") == region_id
                            and row.get("slot") == active.slot
                        )
                    )
                ),
                None,
            )
            if isinstance(slots, list)
            else None
        )
        if not slot or not slot.get("exists") or not slot.get("socks_active"):
            raise DetectorError(
                "local SOCKS process is unavailable; keep node evidence"
            ) from original
        if port != 1080:
            address = slot.get("namespace_ip")
            if not isinstance(address, str):
                raise DetectorError("worker slot address is unavailable") from original
            try:
                await probe_socks_exit(address, 1080, **kwargs)
            except ProbeError as direct_error:
                if (
                    isinstance(direct_error, DetectorError)
                    and not isinstance(direct_error, UnconfirmedRouteError)
                ) or direct_error.code not in {"PROBE_FAILED", "DETECTOR_UNAVAILABLE"}:
                    raise DetectorError("direct slot evidence is incomplete") from direct_error
            else:
                raise DetectorError("direct slot works; the fixed entry needs repair") from original
        if not await self._host_controls_available():
            raise DetectorError("host network or detector control is unavailable") from original
        raise ProbeError(
            "independent destinations failed through the tunnel while host controls succeeded"
        ) from original

    async def budget_status(self) -> dict[str, Any]:
        now = utc_now()
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = await self.database.traffic_summary(since)
        body_bytes = sum(row["rx_bytes"] for row in rows if row["scope"] == "http")
        requests = sum(row["requests"] for row in rows if row["scope"] == "http")
        # Display this explicitly as an estimate, never as measured wire bytes.
        estimated = body_bytes + requests * 8192
        limit = self.settings.monitoring.daily_budget_mib * 1024 * 1024
        return {
            "since": since.isoformat(),
            "timezone": "UTC",
            "measured_body_bytes": body_bytes,
            "requests": requests,
            "estimated_diagnostic_bytes": estimated,
            "estimated_overhead_per_request": 8192,
            "budget_bytes": limit,
            "optional_work_paused": estimated >= limit,
        }

    async def collect(self) -> None:
        async with self._collect_lock:
            result = await self.worker.request(TrafficRequest(action="traffic"))
            raw = result.get("counters")
            counters = (
                [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
            )
            await self.database.record_traffic_counters(counters)
            await self.database.set_runtime_state(
                "traffic_status",
                {
                    "last_sample_at": utc_now().isoformat(),
                    "errors": result.get("errors", []),
                    "sources": len(counters),
                },
            )
            await self.database.set_runtime_state("traffic_error", {})
        if self.settings.monitoring.noise_guard_enabled:
            await self._check_noise(counters)

    async def _check_noise(self, counters: list[dict[str, Any]]) -> None:
        now = utc_now().timestamp()
        policy = self.settings.monitoring
        for counter in counters:
            if counter["scope"] != "tunnel":
                continue
            region_id = counter["region_id"]
            active = await self.database.get_active_slot(region_id)
            if active is None or active.namespace_name != counter["source"]:
                continue
            key = f"noise:{region_id}"
            previous = await self.database.get_runtime_state(key)
            elapsed = now - previous.get("timestamp", now)
            continuous = (
                previous.get("identity") == counter["identity"]
                and 30 <= elapsed <= 180
                and counter["noise_bytes"] >= previous.get("noise_bytes", 0)
            )
            rate = (
                (counter["noise_bytes"] - previous.get("noise_bytes", 0)) / elapsed
                if continuous
                else None
            )
            high = rate is not None and rate >= policy.noise_bytes_per_second
            windows = previous.get("windows", 0) + 1 if high else 0
            high_since = previous.get("high_since") if high else None
            if high and high_since is None:
                high_since = now
            sustained = bool(
                high_since
                and now - high_since >= self.settings.selection_policy.noise_sustained_minutes * 60
                and not previous.get("notified", False)
            )
            if active.node_id is not None and rate is not None:
                await self.database.history.set_noise(active.node_id, rate)
            await self.database.set_runtime_state(
                key,
                {
                    "identity": counter["identity"],
                    "timestamp": now,
                    "noise_bytes": counter["noise_bytes"],
                    "bytes_per_second": rate,
                    "windows": windows,
                    "high_since": high_since,
                    "notified": previous.get("notified", False) if high else False,
                },
            )
            if sustained and self.on_noise is not None:
                await self.on_noise(region_id)
                state = await self.database.get_runtime_state(key)
                state["notified"] = True
                await self.database.set_runtime_state(key, state)

    async def observe_candidate(self, region_id: str, slot: str) -> None:
        policy = self.settings.monitoring
        if not policy.noise_guard_enabled:
            return

        async def read() -> dict[str, Any] | None:
            result = await self.worker.request(TrafficRequest(action="traffic"))
            rows = result.get("counters", [])
            if not isinstance(rows, list):
                return None
            return next(
                (
                    r
                    for r in rows
                    if isinstance(r, dict)
                    and r.get("scope") == "tunnel"
                    and r.get("source") == f"gate-{region_id}-{slot}"
                ),
                None,
            )

        await self.database.set_runtime_state(f"candidate_noise:{region_id}", {})
        previous = await read()
        rates: list[float] = []
        window_count = max(3, policy.noise_confirmation_windows)
        for _ in range(window_count):
            started = asyncio.get_running_loop().time()
            await asyncio.sleep(max(20, policy.noise_observation_seconds))
            current = await read()
            elapsed = asyncio.get_running_loop().time() - started
            if previous and current and previous["identity"] == current["identity"]:
                delta = current["noise_bytes"] - previous["noise_bytes"]
                if delta >= 0:
                    rate = delta / elapsed
                    rates.append(rate)
            previous = current
        slot_record = await self.database.get_slot(region_id, slot)
        if (
            slot_record is not None
            and slot_record.node_id is not None
            and len(rates) == window_count
        ):
            await self.database.history.set_noise(slot_record.node_id, sum(rates) / len(rates))
        await self.database.set_runtime_state(
            f"candidate_noise:{region_id}",
            {
                "node_id": slot_record.node_id if slot_record else None,
                "valid_windows": len(rates),
                "required_windows": window_count,
                "below_threshold": len(rates) == window_count
                and all(rate < policy.noise_bytes_per_second for rate in rates),
                "average_bps": sum(rates) / len(rates) if len(rates) == window_count else None,
                "at": utc_now().timestamp(),
            },
        )
        replacement = await self.database.get_runtime_state(f"noise_requirement:{region_id}")
        if (
            replacement
            and slot_record
            and replacement.get("node_id") == slot_record.node_id
            and (
                len(rates) != window_count
                or any(rate >= policy.noise_bytes_per_second for rate in rates)
                or sum(rates) / len(rates) > replacement["max_bps"]
            )
        ):
            raise NoisyCandidateError("stable replacement lacks the required noise reduction")
        if len(rates) == window_count and any(
            rate >= policy.noise_bytes_per_second for rate in rates
        ):
            raise NoisyCandidateError("candidate has sustained DHCP/broadcast overhead")
        if previous is None:
            await self.database.add_event(
                code="NOISE_MEASUREMENT_UNAVAILABLE",
                level="warning",
                region_id=region_id,
                message="候选广播计数暂不可用, 开销未知; 已保留 HTTPS 可用性校验",
            )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.collect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.database.set_runtime_state(
                    "traffic_error",
                    {
                        "at": utc_now().isoformat(),
                        "error": type(exc).__name__,
                    },
                )
            await asyncio.sleep(60)
