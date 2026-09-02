from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
from functools import partial
from typing import Protocol

from gate.config import GateSettings
from gate.coordinator import SwitchCoordinator
from gate.database import Database, JobStatus, RegionRecord, utc_now
from gate.discovery import DiscoveryService
from gate.domain import RegionMode, RegionStatus
from gate.errors import GateError
from gate.probes import EgressProbe, probe_socks_exit
from gate.scoring import calculate_quality, decide_switch


class SwitchGateway(Protocol):
    async def switch(self, region_id: str, node_id: int) -> object: ...

    async def probe_candidate(self, region_id: str, node_id: int) -> object: ...


ProbeCallable = Callable[..., Awaitable[EgressProbe]]


class AutomationController:
    def __init__(
        self,
        settings: GateSettings,
        database: Database,
        discovery: DiscoveryService,
        coordinator: SwitchGateway | None = None,
        *,
        probe: ProbeCallable = probe_socks_exit,
    ) -> None:
        self.settings = settings
        self.database = database
        self.discovery = discovery
        self.coordinator = coordinator or SwitchCoordinator(database, discovery)
        self.probe = probe
        self.failure_counts: dict[str, int] = {}

    async def _run_automatic_job(
        self,
        *,
        kind: str,
        region_id: str,
        node_id: int,
        operation: Callable[[], Awaitable[object]],
    ) -> object:
        job = await self.database.create_job(kind=kind, region_id=region_id)
        await self.database.update_job(
            job.id,
            status=JobStatus.RUNNING,
            progress=0.1,
            detail={"message": "自动任务已开始", "node_id": node_id},
        )
        try:
            result = await operation()
        except GateError as exc:
            await self.database.update_job(
                job.id,
                status=JobStatus.FAILED,
                progress=1.0,
                error_code=exc.code,
                detail={"message": str(exc), "node_id": node_id},
            )
            raise
        except Exception:
            await self.database.update_job(
                job.id,
                status=JobStatus.FAILED,
                progress=1.0,
                error_code="AUTOMATION_INTERNAL_ERROR",
                detail={"message": "自动任务发生意外错误", "node_id": node_id},
            )
            raise
        await self.database.update_job(
            job.id,
            status=JobStatus.SUCCEEDED,
            progress=1.0,
            detail={"message": "自动任务已完成", "node_id": node_id},
        )
        return result

    async def _attempt_region(self, region: RegionRecord) -> bool:
        active = await self.database.get_active_slot(region.id)
        candidates = await self.database.list_candidates(
            region.id, self.settings.automation.max_candidates_per_cycle
        )
        attempted = 0
        for candidate in candidates:
            if active is not None and candidate.id == active.node_id:
                continue
            if candidate.fingerprint not in self.discovery.profiles:
                continue
            attempted += 1
            try:
                await self._run_automatic_job(
                    kind="auto_switch",
                    region_id=region.id,
                    node_id=candidate.id,
                    operation=partial(self.coordinator.switch, region.id, candidate.id),
                )
            except GateError as exc:
                await self.database.add_event(
                    code="AUTO_CANDIDATE_FAILED",
                    level="warning",
                    message=f"{region.name} 的自动候选节点切换失败",
                    region_id=region.id,
                    node_id=candidate.id,
                    details={"error_code": exc.code, "message": str(exc)},
                )
                continue
            self.failure_counts[region.id] = 0
            return True

        await self.database.add_event(
            code="AUTO_REGION_UNAVAILABLE",
            level="error",
            message=f"{region.name} 没有可用且不重复的自动候选节点",
            region_id=region.id,
            details={"attempted": attempted},
        )
        return False

    async def run_discovery_cycle(self) -> None:
        try:
            await self.discovery.refresh()
        except GateError as exc:
            await self.database.add_event(
                code="AUTOMATION_DISCOVERY_FAILED",
                level="error",
                message="自动刷新 VPN Gate 节点失败",
                details={"error_code": exc.code},
            )
            return

        for region, _candidate_count in await self.database.list_regions():
            if (
                region.enabled
                and region.mode == RegionMode.AUTO
                and region.status != RegionStatus.HEALTHY
            ):
                await self._attempt_region(region)

    async def run_health_cycle(self) -> None:
        for region, _candidate_count in await self.database.list_regions():
            if not region.enabled or region.status != RegionStatus.HEALTHY:
                continue
            active = await self.database.get_active_slot(region.id)
            started_at = utc_now()
            try:
                probe = await self.probe(
                    "127.0.0.1",
                    region.socks_port,
                    expected_countries=set(region.countries),
                )
            except GateError as exc:
                if active is not None and active.node_id is not None:
                    await self.database.record_probe(
                        region_id=region.id,
                        node_id=active.node_id,
                        probe_type="active_health",
                        result="failed",
                        error_code=exc.code,
                        started_at=started_at,
                    )
                failures = self.failure_counts.get(region.id, 0) + 1
                self.failure_counts[region.id] = failures
                await self.database.add_event(
                    code="ACTIVE_HEALTH_CHECK_FAILED",
                    level="warning",
                    message=f"{region.name} 的活动出口健康检查失败",
                    region_id=region.id,
                    details={
                        "failure_count": failures,
                        "error_code": exc.code,
                        "message": str(exc),
                    },
                )
                if failures >= self.settings.selection.active_failure_threshold:
                    await self.database.set_region_status(region.id, RegionStatus.UNAVAILABLE)
                    if region.mode == RegionMode.AUTO:
                        await self._attempt_region(region)
                    else:
                        await self.database.add_event(
                            code="LOCKED_REGION_UNAVAILABLE",
                            level="error",
                            message=f"{region.name} 已锁定的出口连续检查失败, 未自动切换",
                            region_id=region.id,
                            node_id=active.node_id if active is not None else None,
                            details={"failure_count": failures},
                        )
            else:
                if active is not None and active.node_id is not None:
                    await self.database.record_probe(
                        region_id=region.id,
                        node_id=active.node_id,
                        probe_type="active_health",
                        result="succeeded",
                        egress_ip=probe.egress_ip,
                        country_code=probe.country_code,
                        latency_ms=probe.latency_ms,
                        started_at=started_at,
                    )
                    await self.database.set_active_egress_ip(region.id, probe.egress_ip)
                self.failure_counts[region.id] = 0

    async def _optimize_region(self, region: RegionRecord) -> None:
        active = await self.database.get_active_slot(region.id)
        if active is None or active.node_id is None:
            return
        current_metrics = await self.database.get_probe_metrics(region.id, active.node_id)
        if current_metrics is None:
            return
        current_score = calculate_quality(current_metrics).total
        candidates = await self.database.list_candidates(
            region.id, self.settings.discovery.top_k_per_region
        )
        best_node_id: int | None = None
        best_score = -1.0
        attempted_probe = False
        for candidate in candidates:
            if candidate.id == active.node_id:
                continue
            metrics = await self.database.get_probe_metrics(region.id, candidate.id)
            if (
                metrics is None
                and not attempted_probe
                and candidate.fingerprint in self.discovery.profiles
            ):
                attempted_probe = True
                try:
                    await self._run_automatic_job(
                        kind="auto_candidate_probe",
                        region_id=region.id,
                        node_id=candidate.id,
                        operation=partial(
                            self.coordinator.probe_candidate, region.id, candidate.id
                        ),
                    )
                except GateError:
                    continue
                metrics = await self.database.get_probe_metrics(region.id, candidate.id)
            if metrics is None:
                continue
            score = calculate_quality(metrics).total
            if score > best_score:
                best_node_id = candidate.id
                best_score = score

        if best_node_id is None:
            await self.database.reset_selection_candidate(region.id)
            return
        if best_score < current_score * self.settings.selection.improvement_ratio:
            await self.database.reset_selection_candidate(region.id)
            return

        confirmations = await self.database.confirm_selection_candidate(region.id, best_node_id)
        selection = await self.database.get_selection_state(region.id)
        last_switch_at = selection.last_switch_at if selection is not None else None
        if last_switch_at is not None and last_switch_at.tzinfo is None:
            last_switch_at = last_switch_at.replace(tzinfo=UTC)
        decision = decide_switch(
            current_score=current_score,
            candidate_score=best_score,
            confirmation_rounds=confirmations,
            last_switch_at=last_switch_at,
            improvement_ratio=self.settings.selection.improvement_ratio,
            required_confirmation_rounds=self.settings.selection.confirmation_rounds,
            cooldown=timedelta(minutes=self.settings.selection.switch_cooldown_minutes),
        )
        if not decision.should_switch:
            await self.database.add_event(
                code="AUTO_OPTIMIZATION_PENDING",
                message=f"{region.name} 的优选节点正在等待确认轮次或冷却时间",
                region_id=region.id,
                node_id=best_node_id,
                details={
                    "reason": decision.reason,
                    "current_score": current_score,
                    "candidate_score": best_score,
                    "confirmation_rounds": confirmations,
                },
            )
            return
        try:
            await self._run_automatic_job(
                kind="auto_quality_switch",
                region_id=region.id,
                node_id=best_node_id,
                operation=lambda: self.coordinator.switch(region.id, best_node_id),
            )
        except GateError as exc:
            await self.database.add_event(
                code="AUTO_OPTIMIZATION_FAILED",
                level="warning",
                message=f"{region.name} 的线路质量优化失败",
                region_id=region.id,
                node_id=best_node_id,
                details={"error_code": exc.code, "message": str(exc)},
            )
            return
        await self.database.add_event(
            code="AUTO_QUALITY_SWITCH",
            message=f"{region.name} 已自动切换到实测质量更高的出口",
            region_id=region.id,
            node_id=best_node_id,
            details={"previous_score": current_score, "candidate_score": best_score},
        )

    async def run_optimization_cycle(self) -> None:
        for region, _candidate_count in await self.database.list_regions():
            if (
                region.enabled
                and region.mode == RegionMode.AUTO
                and region.status == RegionStatus.HEALTHY
            ):
                await self._optimize_region(region)

    async def run_maintenance_cycle(self) -> None:
        await self.database.cleanup_retention(**self.settings.retention.model_dump())

    async def _repeat(
        self,
        operation: Callable[[], Awaitable[None]],
        interval_seconds: float,
        *,
        immediate: bool,
    ) -> None:
        if not immediate:
            await asyncio.sleep(interval_seconds)
        while True:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.database.add_event(
                    code="AUTOMATION_INTERNAL_ERROR",
                    level="error",
                    message="自动维护周期发生意外错误",
                )
            await asyncio.sleep(interval_seconds)

    async def run_forever(self) -> None:
        discovery_interval = self.settings.discovery.interval_minutes * 60
        optimization_interval = self.settings.automation.optimization_interval_minutes * 60
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                self._repeat(self.run_discovery_cycle, discovery_interval, immediate=True)
            )
            tasks.create_task(
                self._repeat(
                    self.run_health_cycle,
                    self.settings.automation.health_interval_seconds,
                    immediate=False,
                )
            )
            tasks.create_task(
                self._repeat(
                    self.run_optimization_cycle,
                    optimization_interval,
                    immediate=False,
                )
            )
            tasks.create_task(self._repeat(self.run_maintenance_cycle, 86_400, immediate=False))
