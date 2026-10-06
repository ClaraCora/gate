from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from gate.config import GateSettings
from gate.database import Database, utc_now
from gate.http_usage import usage_recorder
from gate.probes import EgressProbe, ProbeError, probe_socks_exit
from gate.worker_protocol import Request, TrafficRequest


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
        self.on_noise: Callable[[str], Awaitable[None]] | None = None

    @asynccontextmanager
    async def measure(self, purpose: str, region_id: str | None = None) -> AsyncIterator[None]:
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
                return await probe_socks_exit(host, port, **kwargs)
        finally:
            async with self._condition:
                self._running_probes -= 1
                self._condition.notify_all()

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
            await self.database.set_runtime_state(
                key,
                {
                    "identity": counter["identity"],
                    "timestamp": now,
                    "noise_bytes": counter["noise_bytes"],
                    "bytes_per_second": rate,
                    "windows": windows,
                },
            )
            if windows >= policy.noise_confirmation_windows and self.on_noise is not None:
                await self.on_noise(region_id)

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

        previous = await read()
        high_windows = 0
        for _ in range(policy.noise_confirmation_windows):
            started = asyncio.get_running_loop().time()
            await asyncio.sleep(policy.noise_observation_seconds)
            current = await read()
            elapsed = asyncio.get_running_loop().time() - started
            if previous and current and previous["identity"] == current["identity"]:
                delta = current["noise_bytes"] - previous["noise_bytes"]
                high_windows = (
                    high_windows + 1 if delta / elapsed >= policy.noise_bytes_per_second else 0
                )
            else:
                high_windows = 0
            previous = current
        if high_windows >= policy.noise_confirmation_windows:
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
