from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import UTC
from functools import partial
from typing import Protocol

from gate.config import GateSettings
from gate.coordinator import SwitchBusyError, SwitchCoordinator
from gate.database import Database, JobStatus, RegionRecord, utc_now
from gate.discovery import DiscoveryService
from gate.domain import RegionMode, RegionStatus
from gate.errors import GateError
from gate.monitoring import MonitoringService
from gate.probes import EgressProbe, probe_socks_exit
from gate.selection import endpoint, failure_category


class SwitchGateway(Protocol):
    async def switch(self, region_id: str, node_id: int) -> object: ...

    async def probe_candidate(self, region_id: str, node_id: int) -> object: ...


class NotificationGateway(Protocol):
    async def send(self, message: str) -> bool: ...


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
        notifier: NotificationGateway | None = None,
        monitoring: MonitoringService | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.discovery = discovery
        self.coordinator = coordinator or SwitchCoordinator(database, discovery)
        self.probe = probe
        self.notifier = notifier
        self.monitoring = monitoring
        self._attempt_locks: dict[str, asyncio.Lock] = {}
        self._noise_pending: set[str] = set()
        self._recovery_jobs: dict[str, asyncio.Task[None]] = {}
        self._noise_jobs: dict[str, asyncio.Task[None]] = {}
        self._standby_task: asyncio.Task[None] | None = None
        self._standby_lock = asyncio.Lock()
        self._standby_scan_after = 0.0
        if monitoring is not None:
            monitoring.on_noise = self.queue_noise
        self.failure_counts: dict[str, int] = {}
        self._enabled = settings.automation.enabled
        self._enabled_event = asyncio.Event()
        if self._enabled:
            self._enabled_event.set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled:
            self._enabled_event.set()
        else:
            self._enabled_event.clear()

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
            async with (
                self.monitoring.measure(kind, region_id) if self.monitoring else nullcontext()
            ):
                result = await operation()
        except asyncio.CancelledError:
            await self.database.update_job(
                job.id,
                status=JobStatus.CANCELLED,
                progress=1.0,
                detail={"message": "自动任务已取消", "node_id": node_id},
            )
            raise
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

    async def _record_automatic_switch_failure(
        self,
        region: RegionRecord,
        node_id: int,
        exc: Exception,
        *,
        automatic: bool = True,
        event_code: str = "AUTO_CANDIDATE_FAILED",
        event_message: str | None = None,
    ) -> None:
        category = failure_category(exc)
        if category != "node":
            await self.database.add_event(
                code="AUTO_CANDIDATE_NOT_PENALIZED",
                level="warning",
                message=f"{region.name} 的候选操作未归因于节点故障, 不计入失败冷却或人工干预次数",
                region_id=region.id,
                node_id=node_id,
                details={"category": category, "error_code": getattr(exc, "code", "INTERNAL")},
            )
            return
        await self.database.history.fail(
            node_id,
            getattr(exc, "selection_incident", None)
            or f"switch:{region.id}:{node_id}:{utc_now().timestamp()}",
        )
        await self.database.record_switch_failure(region.id, node_id)
        failure_streak, intervention_notified = await self.database.record_switch_failure_attempt(
            region.id
        )
        error_code = exc.code if isinstance(exc, GateError) else "AUTOMATION_INTERNAL_ERROR"
        await self.database.add_event(
            code=event_code,
            level="warning",
            message=event_message or f"{region.name} 的自动候选节点切换失败",
            region_id=region.id,
            node_id=node_id,
            details={"error_code": error_code, "message": str(exc)},
        )
        if failure_streak < 5 or intervention_notified or self.notifier is None:
            return
        message = (
            "Gate 需要人工干预\n"
            f"入口: {region.name} ({region.id})\n"
            f"连续{'自动' if automatic else ''}切换失败: {failure_streak} 次\n"
            f"当前节点 ID: {region.active_node_id or '--'}\n"
            f"当前实际出口: {region.active_egress_ip or '--'}\n"
            "请检查 VPN 隧道、出口探测和候选线路。"
        )
        try:
            sent = await self.notifier.send(message)
        except Exception as notify_exc:
            await self.database.add_event(
                code="TELEGRAM_NOTIFICATION_FAILED",
                level="error",
                message=f"{region.name} 的 Telegram 人工干预通知发送失败",
                region_id=region.id,
                node_id=node_id,
                details={"error": str(notify_exc)},
            )
        else:
            if sent:
                await self.database.mark_switch_intervention_notified(region.id)
                await self.database.add_event(
                    code="TELEGRAM_INTERVENTION_NOTIFIED",
                    message=f"{region.name} 连续自动切换失败, 已推送人工干预通知",
                    region_id=region.id,
                    node_id=node_id,
                    details={"failure_streak": failure_streak},
                )

    async def _attempt_region(self, region: RegionRecord, *, automatic: bool = True) -> bool:
        lock = self._attempt_locks.setdefault(region.id, asyncio.Lock())
        if lock.locked():
            return False
        async with lock:
            return await self._attempt_region_unlocked(region, automatic=automatic)

    async def _attempt_region_unlocked(self, region: RegionRecord, *, automatic: bool) -> bool:
        entries = await self.database.history.explain(
            region.id,
            self.settings.selection_policy,
            set(self.discovery.profiles),
            ignore_round=True,
        )
        compliant = [entry for entry in entries if not entry.excluded]
        round_state = await self.database.get_runtime_state(f"selection_round:{region.id}")
        failed_endpoints: set[str] = set(round_state.get("failed_endpoints", []))
        universe = {endpoint(entry.node) for entry in compliant}
        if universe and universe <= failed_endpoints:
            # All candidates have had their turn. Begin a new batch while endpoint
            # cooldowns continue to exclude the recently failed ones.
            failed_endpoints.clear()
            round_state = {"failed_endpoints": [], "batch": round_state.get("batch", 0)}
            await self.database.set_runtime_state(f"selection_round:{region.id}", round_state)
        entries = await self.database.history.explain(
            region.id,
            self.settings.selection_policy,
            set(self.discovery.profiles),
            ignore_round=True,
        )
        noisy = await self.database.get_runtime_state(f"noise_exclusions:{region.id}")
        now = utc_now().timestamp()
        eligible = [
            e
            for e in entries
            if not e.excluded
            and noisy.get(endpoint(e.node), 0) <= now
            and noisy.get(str(e.node.id), 0) <= now
        ]
        if not eligible:
            cooling = [
                e.evidence.cooldown_until
                for e in entries
                if e.evidence.cooldown_until > now and set(e.excluded).issubset({"cooldown"})
            ]
            retry_at = min(cooling) if cooling else now + 300
            await self.database.set_runtime_state(
                f"recovery:{region.id}", {"next_retry": retry_at, "reason": "no_eligible_candidate"}
            )
            await self.database.add_event(
                code="AUTO_WAITING_FOR_CANDIDATE" if cooling else "AUTO_REGION_UNAVAILABLE",
                level="warning" if cooling else "error",
                message=(
                    f"{region.name} 的候选节点仍在冷却, 等待冷却结束"
                    if cooling
                    else f"{region.name} 没有符合排除规则的候选节点"
                ),
                region_id=region.id,
                details={"attempted": 0, "next_retry": retry_at},
            )
            return False

        unfailed = [entry for entry in eligible if endpoint(entry.node) not in failed_endpoints]
        if not unfailed:
            await self.database.add_event(
                code="AUTO_WAITING_FOR_COOLDOWN",
                level="warning",
                message=f"{region.name} 本轮候选均已尝试, 等待故障冷却后重试",
                region_id=region.id,
                details={"attempted": 0},
            )
            return False

        selected = unfailed[: self.settings.selection_policy.max_candidates_per_batch]
        attempted = 0
        for candidate in selected:
            node = candidate.node
            attempted += 1
            try:
                operation = partial(self.coordinator.switch, region.id, node.id)
                if automatic:
                    await self._run_automatic_job(
                        kind="auto_switch",
                        region_id=region.id,
                        node_id=node.id,
                        operation=operation,
                    )
                else:
                    await operation()
            except Exception as exc:
                if isinstance(exc, GateError) and exc.code == "SWITCH_BUSY":
                    raise
                if isinstance(exc, GateError) and exc.code == "NOISY_CANDIDATE":
                    noisy[endpoint(node)] = utc_now().timestamp() + 86_400
                    noisy[str(node.id)] = utc_now().timestamp() + 86_400
                    await self.database.set_runtime_state(f"noise_exclusions:{region.id}", noisy)
                    await self.database.add_event(
                        code="SWITCH_CANDIDATE_NOISY",
                        level="warning",
                        region_id=region.id,
                        node_id=node.id,
                        message="候选线路广播开销过高, 已加入临时排除并继续尝试",
                    )
                    continue
                category = failure_category(exc)
                if category == "policy":
                    error_code = getattr(exc, "code", "POLICY")
                    await self.database.add_event(
                        code=f"SWITCH_CANDIDATE_EXCLUDED_{error_code}",
                        level="warning",
                        region_id=region.id,
                        node_id=node.id,
                        message="候选线路因占用或出口冲突被排除, 继续尝试其余线路",
                    )
                    continue
                if category != "node":
                    if category == "detector":
                        # Keep failures/alerts untouched, but do not retry the
                        # same inconclusive endpoint first on every sibling port.
                        await self.database.history.defer_candidate(region.group_id, node)
                    await self.database.set_runtime_state(
                        f"recovery:{region.id}",
                        {
                            "next_retry": utc_now().timestamp() + 60,
                            "reason": f"{category}_unavailable",
                        },
                    )
                    await self.database.add_event(
                        code=f"SWITCH_NOT_PENALIZED_{category.upper()}",
                        level="warning",
                        region_id=region.id,
                        node_id=node.id,
                        message="候选操作未证明线路故障, 暂停本轮并保留当前路由",
                    )
                    return False
                failed_endpoints.add(endpoint(node))
                round_state.update(failed_endpoints=sorted(failed_endpoints))
                await self.database.set_runtime_state(f"selection_round:{region.id}", round_state)
                await self._record_automatic_switch_failure(
                    region,
                    node.id,
                    exc,
                    automatic=automatic,
                    event_code="AUTO_CANDIDATE_FAILED" if automatic else "MANUAL_CANDIDATE_FAILED",
                    event_message=f"{region.name} 的候选节点切换失败",
                )
                continue
            if automatic:
                self.failure_counts[region.id] = 0
            await self.database.reset_switch_failure_streak(region.id)
            await self.database.set_runtime_state(f"selection_round:{region.id}", round_state)
            await self.database.set_runtime_state(
                f"switch_reason:{region.id}",
                {
                    "reason": "confirmed_failure_recovery" if automatic else "manual",
                    "at": utc_now().isoformat(),
                },
            )
            await self.database.set_runtime_state(f"recovery:{region.id}", {})
            await self.database.add_event(
                code="STABLE_SELECTION_SWITCH_COMPLETED",
                region_id=region.id,
                node_id=node.id,
                message=f"{region.name} 已按稳定策略完成出口切换",
                details={
                    "reason": "confirmed_failure_recovery" if automatic else "manual",
                    "attempted": attempted,
                },
            )
            return True

        round_state["failed_endpoints"] = sorted(failed_endpoints)
        batch = round_state.get("batch", 0) + 1
        round_state["batch"] = batch
        delays = (60, 120, 300, 900, 1800)
        await self.database.set_runtime_state(f"selection_round:{region.id}", round_state)
        await self.database.set_runtime_state(
            f"recovery:{region.id}",
            {
                "attempt": batch,
                "next_retry": utc_now().timestamp() + delays[min(batch - 1, 4)],
                "reason": "candidate_batch_exhausted",
            },
        )
        await self.database.add_event(
            code="AUTO_CANDIDATE_BATCH_FAILED",
            level="warning",
            message=f"{region.name} 本轮 {attempted} 个候选均未通过, 按退避间隔稍后继续",
            region_id=region.id,
            details={
                "attempted": attempted,
                "batch": batch,
                "next_retry": (await self.database.get_runtime_state(f"recovery:{region.id}")).get(
                    "next_retry"
                ),
            },
        )
        return False

    async def attempt_region(self, region_id: str, *, automatic: bool = False) -> bool:
        """Run one on-demand failover cycle using the same exclusion rules as automation."""
        region = await self.database.get_region(region_id)
        if region is None:
            return False
        return await self._attempt_region(region, automatic=automatic)

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
                await self._recover(region)

    async def run_discovery_tick(self) -> None:
        now = utc_now().timestamp()
        state = await self.database.get_runtime_state("discovery_schedule")
        if state.get("next_refresh", 0) > now:
            return
        if (
            self.monitoring
            and (await self.monitoring.budget_status())["optional_work_paused"]
            and all(
                r.status == RegionStatus.HEALTHY
                for r, _ in await self.database.list_regions()
                if r.enabled
            )
        ):
            return
        await self.database.set_runtime_state(
            "discovery_schedule",
            {
                "next_refresh": now + self.settings.monitoring.discovery_interval_minutes * 60,
            },
        )
        await self.run_discovery_cycle()

    async def run_health_cycle(self, *, due_only: bool = False) -> None:
        regions = [r for r, _ in await self.database.list_regions() if r.enabled]
        semaphore = asyncio.Semaphore(self.settings.monitoring.max_concurrent_probes)

        async def guarded(region: RegionRecord, index: int) -> None:
            async with semaphore:
                try:
                    await self._check_health(
                        region, due_only=due_only, index=index, total=len(regions)
                    )
                except Exception as exc:
                    # An unexpected error at one entry must not abort the cycle,
                    # leave sibling probes running, or prevent overdue recovery.
                    state = await self.database.get_runtime_state(f"health:{region.id}")
                    state["next_check"] = utc_now().timestamp() + min(
                        60, self.settings.monitoring.health_interval_seconds
                    )
                    await self.database.set_runtime_state(f"health:{region.id}", state)
                    active = await self.database.get_active_slot(region.id)
                    if active is not None and active.node_id is not None:
                        await self.database.record_probe(
                            region_id=region.id,
                            node_id=active.node_id,
                            probe_type="active_health",
                            result="unknown",
                            error_code="HEALTH_CHECK_INTERNAL_ERROR",
                        )
                    await self.database.add_event(
                        code="ACTIVE_HEALTH_INTERNAL_ERROR",
                        level="error",
                        region_id=region.id,
                        message=f"{region.name} 的检测任务异常, 其他入口继续检查和恢复",
                        details={"error": type(exc).__name__},
                    )

        await asyncio.gather(*(guarded(region, index) for index, region in enumerate(regions)))

    async def _check_health(
        self, region: RegionRecord, *, due_only: bool, index: int, total: int
    ) -> None:
        if region.status == RegionStatus.SWITCHING:
            return
        active = await self.database.get_active_slot(region.id)
        if active is None or active.node_id is None:
            return
        key = f"health:{region.id}"
        state = await self.database.get_runtime_state(key)
        policy = self.settings.monitoring
        started_at = utc_now()
        now = started_at.timestamp()
        if not state or state.get("node_id") != active.node_id:
            verified = active.last_verified_at
            verified_at = verified.replace(tzinfo=UTC).timestamp() if verified else now
            state = {
                "node_id": active.node_id,
                "failures": 0,
                "next_check": now + index * policy.health_interval_seconds / max(1, total),
                "next_full": verified_at + policy.full_verification_hours * 3600,
            }
            await self.database.set_runtime_state(key, state)
        if due_only and state.get("next_check", 0) > now:
            return
        if now - state.get("failure_started_at", now) > 300:
            state.update(failures=0, failure_started_at=now)
        full = state.get("next_full", 0) <= now
        credentials: dict[str, str] = {}
        if self.settings.socks_auth.enabled:
            credentials = {
                "username": self.settings.socks_auth.username,
                "password": self.settings.socks_auth.password,
            }
        try:
            probe_call = self.monitoring.probe if self.monitoring else self.probe
            probe = await probe_call(
                "127.0.0.1",
                region.socks_port,
                expected_countries=set(region.countries),
                full=full,
                previous_ip=region.active_egress_ip,
                secondary_first=state.get("failures", 0) > 0,
                provider_offset=state.get("failures", 0),
                **credentials,
            )
        except GateError as exc:
            # A switch may finish while any kind of probe is in flight.
            current = await self.database.get_active_slot(region.id)
            if current is None or current.node_id != active.node_id or current.slot != active.slot:
                return
            completed_at = utc_now().timestamp()
            if failure_category(exc) != "node":
                # No success/failure sample: this is unknown, not a failed exit.
                await self.database.record_probe(
                    region_id=region.id,
                    node_id=active.node_id,
                    probe_type="active_health",
                    result="unknown",
                    error_code=exc.code,
                    started_at=started_at,
                )
                state["next_check"] = completed_at + min(60, policy.health_interval_seconds)
                state["detector_error"] = str(exc)
                await self.database.set_runtime_state(key, state)
                await self.database.add_event(
                    code="ACTIVE_HEALTH_UNKNOWN",
                    level="warning",
                    message=f"{region.name} 的检测服务暂不可用, 保留当前线路",
                    region_id=region.id,
                    details={"error": str(exc)},
                )
                return
            await self.database.record_probe(
                region_id=region.id,
                node_id=active.node_id,
                probe_type="active_health",
                result="failed",
                error_code=exc.code,
                started_at=started_at,
            )
            failures = state.get("failures", 0) + 1
            self.failure_counts[region.id] = failures
            state.update(
                failures=failures,
                failure_started_at=state.get("failure_started_at", now),
                next_check=completed_at + policy.failure_confirm_seconds,
            )
            await self.database.add_event(
                code="ACTIVE_HEALTH_CHECK_FAILED",
                level="warning",
                message=f"{region.name} 的活动出口检查失败, 正在复核",
                region_id=region.id,
                details={"failure_count": failures, "error_code": exc.code},
            )
            if failures >= self.settings.selection.active_failure_threshold:
                state["next_check"] = completed_at + policy.health_interval_seconds
                if not state.get("fault_incident_recorded"):
                    await self.database.history.fail(
                        active.node_id,
                        f"health:{region.id}:{active.node_id}:"
                        f"{state.get('failure_started_at', now)}",
                        now=now,
                    )
                    state["fault_incident_recorded"] = True
                    await self.database.set_runtime_state(key, state)
                await self.database.set_region_status(region.id, RegionStatus.UNAVAILABLE)
                if region.status != RegionStatus.UNAVAILABLE:
                    await self.database.add_event(
                        code="ACTIVE_OUTAGE_CONFIRMED",
                        level="error",
                        region_id=region.id,
                        node_id=active.node_id,
                        message=f"{region.name} 连续复核确认出口不可用",
                        details={"reason": "route_failure"},
                    )
                if region.mode == RegionMode.AUTO:
                    if due_only:
                        self._launch_recovery(region)
                    else:
                        await self._recover(region)
                elif failures == self.settings.selection.active_failure_threshold:
                    await self.database.add_event(
                        code="LOCKED_REGION_UNAVAILABLE",
                        level="error",
                        message=f"{region.name} 已锁定的出口连续检查失败, 未自动切换",
                        region_id=region.id,
                        details={"failure_count": failures},
                    )
        else:
            # A manual switch could complete while an old health request was in flight.
            current = await self.database.get_active_slot(region.id)
            if current is None or current.node_id != active.node_id or current.slot != active.slot:
                return
            completed_at = utc_now().timestamp()
            await self.database.record_probe(
                region_id=region.id,
                node_id=active.node_id,
                probe_type="active_health",
                result="succeeded",
                egress_ip=probe.egress_ip,
                country_code=probe.country_code or None,
                latency_ms=probe.latency_ms,
                started_at=started_at,
            )
            try:
                await self.database.set_active_egress_ip(region.id, probe.egress_ip)
            except ValueError as exc:
                await self.database.set_active_egress_ip(
                    region.id, probe.egress_ip, allow_duplicate=True
                )
                await self.database.add_event(
                    code="ACTIVE_HEALTH_DUPLICATE_EXIT",
                    level="error",
                    message=f"{region.name} 检测到与同组入口重复的实际出口 IP, 已停止使用当前状态",
                    region_id=region.id,
                    details={"egress_ip": probe.egress_ip, "error": str(exc)},
                )
                await self.database.set_region_status(region.id, RegionStatus.UNAVAILABLE)
                if region.status != RegionStatus.UNAVAILABLE:
                    await self.database.add_event(
                        code="ACTIVE_OUTAGE_CONFIRMED",
                        level="error",
                        region_id=region.id,
                        node_id=active.node_id,
                        message=f"{region.name} 因重复出口暂不可用",
                        details={"reason": "duplicate_exit"},
                    )
                state.update(
                    failures=0,
                    failure_started_at=now,
                    fault_incident_recorded=False,
                    next_check=completed_at + policy.health_interval_seconds,
                    detector_error="duplicate_exit",
                )
                await self.database.set_runtime_state(key, state)
                if region.mode == RegionMode.AUTO:
                    if due_only:
                        self._launch_recovery(region)
                    else:
                        await self._recover(region)
                return
            await self.database.set_region_status(region.id, RegionStatus.HEALTHY)
            if region.status == RegionStatus.UNAVAILABLE:
                await self.database.add_event(
                    code="ACTIVE_OUTAGE_RECOVERED",
                    region_id=region.id,
                    node_id=active.node_id,
                    message=f"{region.name} 当前线路通过检查并恢复可用",
                )
            self.failure_counts[region.id] = 0
            state.update(
                failures=0,
                failure_started_at=now,
                fault_incident_recorded=False,
                next_check=completed_at + policy.health_interval_seconds,
                detector_error=None,
            )
            if full or probe.egress_ip != region.active_egress_ip:
                state["next_full"] = now + policy.full_verification_hours * 3600
            await self.database.set_runtime_state(f"recovery:{region.id}", {})
        await self.database.set_runtime_state(key, state)

    async def _recover(self, region: RegionRecord) -> None:
        current = await self.database.get_region(region.id)
        if (
            current is None
            or not current.enabled
            or current.mode != RegionMode.AUTO
            or current.status != RegionStatus.UNAVAILABLE
        ):
            return
        region = current
        key = f"recovery:{region.id}"
        state = await self.database.get_runtime_state(key)
        now = utc_now().timestamp()
        if (
            state.get("next_retry", 0) > now
            or state.get("in_progress_until", 0) > now
            or self._attempt_locks.get(region.id, asyncio.Lock()).locked()
        ):
            return
        # Persist a short lease before network work. The backoff is written only
        # after this batch ends, so the delay is measured from actual completion.
        await self.database.set_runtime_state(key, {**state, "in_progress_until": now + 360})
        refresh = await self.database.get_runtime_state("recovery_discovery")
        if refresh.get("next_refresh", 0) <= now:
            await self.database.set_runtime_state("recovery_discovery", {"next_refresh": now + 300})
            try:
                await self.discovery.refresh(minimum_interval=300)
            except GateError:
                if not self.discovery.profiles:
                    return
        try:
            await self._attempt_region(region)
        except SwitchBusyError:
            await self.database.set_runtime_state(key, {"next_retry": now + 60})

    def _launch_recovery(self, region: RegionRecord) -> None:
        task = self._recovery_jobs.get(region.id)
        if task is not None and not task.done():
            return
        standby_task = self._standby_task
        if standby_task is not None and not standby_task.done():
            standby_task.cancel()
        noise_tasks = [task for task in self._noise_jobs.values() if not task.done()]
        for noise_task in noise_tasks:
            noise_task.cancel()

        async def recover() -> None:
            try:
                if standby_task is not None:
                    await asyncio.gather(standby_task, return_exceptions=True)
                if noise_tasks:
                    await asyncio.gather(*noise_tasks, return_exceptions=True)
                await self._recover(region)
            except Exception as exc:
                await self.database.add_event(
                    code="RECOVERY_INTERNAL_ERROR",
                    level="error",
                    region_id=region.id,
                    message="出口恢复任务异常",
                    details={"error": type(exc).__name__},
                )

        self._recovery_jobs[region.id] = asyncio.create_task(recover())

    async def close(self) -> None:
        tasks = [*self._recovery_jobs.values(), *self._noise_jobs.values()]
        if self._standby_task is not None:
            tasks.append(self._standby_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_scheduler_tick(self) -> None:
        await self.run_health_cycle(due_only=True)
        for region, _ in await self.database.list_regions():
            if (
                region.enabled
                and region.mode == RegionMode.AUTO
                and region.status == RegionStatus.UNAVAILABLE
            ):
                self._launch_recovery(region)
        pending, self._noise_pending = self._noise_pending, set()
        for region_id in pending:
            self._launch_noise_replacement(region_id)
        self._schedule_standby_validation()

    def _schedule_standby_validation(self) -> None:
        now = utc_now().timestamp()
        if self._standby_scan_after > now:
            return
        if not self.settings.selection_policy.standby_enabled:
            return
        if self._standby_task is not None and not self._standby_task.done():
            return
        if any(not task.done() for task in self._recovery_jobs.values()):
            return
        if any(not task.done() for task in self._noise_jobs.values()):
            return
        self._standby_scan_after = now + 60

        async def validate_one() -> None:
            async with self._standby_lock:
                regions = [r for r, _ in await self.database.list_regions() if r.enabled]
                if any(r.status == RegionStatus.UNAVAILABLE for r in regions):
                    return
                for region in regions:
                    health_state = await self.database.get_runtime_state(f"health:{region.id}")
                    if health_state.get("failures", 0) > 0:
                        return
                if (
                    self.monitoring
                    and (await self.monitoring.budget_status())["optional_work_paused"]
                ):
                    return
                now = utc_now().timestamp()
                groups: dict[str, list[RegionRecord]] = {}
                for region in regions:
                    groups.setdefault(region.group_id, []).append(region)
                for group_id, entries_by_group in sorted(groups.items()):
                    schedule = await self.database.get_runtime_state(f"standby_schedule:{group_id}")
                    if schedule.get("next_probe", 0) > now or schedule.get("lease_until", 0) > now:
                        continue
                    region = next(
                        (r for r in entries_by_group if r.active_node_id is not None),
                        entries_by_group[0],
                    )
                    pool = await self.database.history.standby(group_id)
                    candidates = await self.database.history.explain(
                        region.id,
                        self.settings.selection_policy,
                        set(self.discovery.profiles),
                        ignore_round=True,
                    )
                    eligible = [entry for entry in candidates if not entry.excluded]
                    eligible_endpoints = {endpoint(entry.node) for entry in eligible}
                    for record in pool:
                        if record.endpoint not in eligible_endpoints:
                            await self.database.history.remove_standby(group_id, record.endpoint)
                    pool = [record for record in pool if record.endpoint in eligible_endpoints]
                    pool_by_endpoint = {record.endpoint: record for record in pool}
                    if len(pool) < self.settings.selection_policy.standby_pool_size:
                        candidate = next(
                            (
                                entry
                                for entry in eligible
                                if endpoint(entry.node) not in pool_by_endpoint
                            ),
                            None,
                        )
                    else:
                        pool_candidates = [
                            entry for entry in eligible if endpoint(entry.node) in pool_by_endpoint
                        ]
                        candidate = min(
                            pool_candidates,
                            key=lambda entry: pool_by_endpoint[endpoint(entry.node)].validated_at,
                            default=None,
                        )
                    if candidate is None:
                        await self.database.set_runtime_state(
                            f"standby_schedule:{group_id}", {"next_probe": now + 3600}
                        )
                        continue
                    await self.database.set_runtime_state(
                        f"standby_schedule:{group_id}",
                        {**schedule, "lease_until": now + 240},
                    )
                    try:
                        async with (
                            self.monitoring.measure("standby_validation", region.id)
                            if self.monitoring
                            else nullcontext()
                        ):
                            result = await asyncio.wait_for(
                                self.coordinator.probe_candidate(region.id, candidate.node.id),
                                timeout=self.settings.selection_policy.standby_timeout_seconds,
                            )
                    except asyncio.CancelledError:
                        await self.database.set_runtime_state(
                            f"standby_schedule:{group_id}",
                            {"next_probe": utc_now().timestamp(), "lease_until": 0},
                        )
                        raise
                    except Exception as exc:
                        category = failure_category(exc)
                        if category == "node" or category == "policy":
                            await self.database.history.remove_standby(
                                group_id, endpoint(candidate.node)
                            )
                        await self.database.add_event(
                            code="STANDBY_VALIDATION_FAILED",
                            level="warning",
                            region_id=region.id,
                            node_id=candidate.node.id,
                            message=f"{region.name} 的共享备用线路验证未完成",
                            details={"category": category},
                        )
                    else:
                        await self.database.history.save_standby(
                            group_id, candidate.node, getattr(result, "egress_ip", None)
                        )
                        await self.database.add_event(
                            code="STANDBY_VALIDATION_COMPLETED",
                            message=f"{region.name} 的共享备用线路已验证并释放临时隧道",
                            region_id=region.id,
                            node_id=candidate.node.id,
                            details={
                                "group_id": group_id,
                                "pool_size": min(
                                    len(pool) + 1,
                                    self.settings.selection_policy.standby_pool_size,
                                ),
                            },
                        )
                    await self.database.set_runtime_state(
                        f"standby_schedule:{group_id}",
                        {
                            "next_probe": utc_now().timestamp()
                            + self.settings.selection_policy.standby_interval_hours * 3600,
                            "lease_until": 0,
                        },
                    )
                    return

        self._standby_task = asyncio.create_task(validate_one())
        self._standby_task.add_done_callback(
            lambda task: (
                setattr(self, "_standby_task", None) if self._standby_task is task else None
            )
        )

    async def queue_noise(self, region_id: str) -> None:
        self._noise_pending.add(region_id)

    def _launch_noise_replacement(self, region_id: str) -> None:
        task = self._noise_jobs.get(region_id)
        if task is not None and not task.done():
            return
        if any(not recovery.done() for recovery in self._recovery_jobs.values()):
            return

        async def replace() -> None:
            try:
                async with (
                    self._standby_lock,
                    (
                        self.monitoring.measure("noise_validation", region_id)
                        if self.monitoring
                        else nullcontext()
                    ),
                ):
                    await self._replace_noisy_region(region_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.database.add_event(
                    code="NOISE_REPLACEMENT_FAILED",
                    level="error",
                    region_id=region_id,
                    message="广播开销替换任务异常, 已保留当前调度",
                    details={"error": type(exc).__name__},
                )

        task = asyncio.create_task(replace())
        self._noise_jobs[region_id] = task
        task.add_done_callback(lambda completed: self._noise_jobs.pop(region_id, None))

    async def _replace_noisy_region(self, region_id: str) -> None:
        region = await self.database.get_region(region_id)
        if (
            region is None
            or not region.enabled
            or region.status != RegionStatus.HEALTHY
            or not self.settings.monitoring.noise_guard_enabled
        ):
            return
        state = await self.database.get_runtime_state(f"noise_action:{region_id}")
        now = utc_now().timestamp()
        if state.get("next_action", 0) > now:
            return
        cooldown = self.settings.selection_policy.noise_replacement_hours * 3600
        await self.database.set_runtime_state(
            f"noise_action:{region_id}",
            {
                "next_action": now + cooldown,
                "node_id": region.active_node_id,
            },
        )
        await self.database.add_event(
            code="SUSTAINED_TUNNEL_NOISE",
            level="warning",
            region_id=region_id,
            message=f"{region.name} 持续广播开销超标",
            details={"mode": region.mode, "cooldown_seconds": cooldown},
        )
        if self.notifier:
            try:
                await self.notifier.send(
                    "Gate 广播开销提醒\n"
                    f"{region.name} 连续 "
                    f"{self.settings.selection_policy.noise_sustained_minutes} 分钟超标, "
                    "当前线路保持运行。"
                )
            except Exception as exc:
                await self.database.add_event(
                    code="TELEGRAM_NOTIFICATION_FAILED",
                    level="warning",
                    region_id=region.id,
                    message=f"{region.name} 的广播开销提醒发送失败",
                    details={"error": type(exc).__name__},
                )
        if region.mode == RegionMode.LOCKED:
            return
        if self.monitoring and (await self.monitoring.budget_status())["optional_work_paused"]:
            return
        if region.active_node_id is None:
            return
        current = await self.database.history.snapshot(self.settings.selection_policy)
        current_rate = current.get(region.active_node_id)
        if current_rate is None or current_rate.noise_bps is None:
            return
        entries = await self.database.history.explain(
            region.id,
            self.settings.selection_policy,
            set(self.discovery.profiles),
            ignore_round=True,
        )
        pool = await self.database.history.standby(region.group_id)
        pool_endpoints = {item.endpoint for item in pool}
        for entry in entries:
            if (
                entry.excluded
                or entry.evidence.tier != "stable"
                or endpoint(entry.node) not in pool_endpoints
            ):
                continue
            await self.database.set_runtime_state(f"candidate_noise:{region.id}", {})
            try:
                await asyncio.wait_for(
                    self.coordinator.probe_candidate(region.id, entry.node.id), timeout=180
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                category = failure_category(exc)
                if category == "node" or category == "policy":
                    await self.database.history.remove_standby(
                        region.group_id, endpoint(entry.node)
                    )
                continue
            measured = await self.database.get_runtime_state(f"candidate_noise:{region.id}")
            if (
                measured.get("node_id") != entry.node.id
                or not measured.get("below_threshold")
                or measured.get("average_bps") is None
                or measured["average_bps"] > current_rate.noise_bps * 0.5
            ):
                continue
            current_region = await self.database.get_region(region.id)
            if (
                current_region is None
                or current_region.active_node_id != region.active_node_id
                or current_region.status != RegionStatus.HEALTHY
                or current_region.mode != RegionMode.AUTO
                or any(not task.done() for task in self._recovery_jobs.values())
            ):
                return
            await self.database.set_runtime_state(
                f"noise_requirement:{region.id}",
                {"node_id": entry.node.id, "max_bps": current_rate.noise_bps * 0.5},
            )
            try:
                await self.coordinator.switch(region.id, entry.node.id)
            except Exception as exc:
                category = failure_category(exc)
                if category == "node":
                    await self.database.history.fail(
                        entry.node.id,
                        getattr(
                            exc,
                            "selection_incident",
                            None,
                        )
                        or f"noise-switch:{region.id}:{endpoint(entry.node)}:"
                        f"{utc_now().timestamp()}",
                    )
                    await self.database.history.remove_standby(
                        region.group_id, endpoint(entry.node)
                    )
                    continue
                if category == "policy":
                    await self.database.history.remove_standby(
                        region.group_id, endpoint(entry.node)
                    )
                    continue
                await self.database.add_event(
                    code="NOISE_REPLACEMENT_FAILED",
                    level="warning",
                    region_id=region.id,
                    node_id=entry.node.id,
                    message=f"{region.name} 噪声替代线路切换失败, 保留当前出口",
                    details={"category": category},
                )
                return
            finally:
                await self.database.set_runtime_state(f"noise_requirement:{region.id}", {})
            await self.database.reset_switch_failure_streak(region.id)
            await self.database.set_runtime_state(
                f"switch_reason:{region.id}",
                {"reason": "sustained_noise_reduction", "at": utc_now().isoformat()},
            )
            await self.database.add_event(
                code="NOISE_REPLACEMENT_COMPLETED",
                message=f"{region.name} 已切换到广播开销低至少 50% 的稳定出口",
                region_id=region.id,
                node_id=entry.node.id,
                details={
                    "previous_noise_bps": current_rate.noise_bps,
                    "candidate_noise_bps": measured["average_bps"],
                },
            )
            return
        await self.database.add_event(
            code="NO_RELIABLE_LOW_NOISE_CANDIDATE",
            level="warning",
            region_id=region.id,
            message=f"{region.name} 暂无满足稳定性和噪声降幅要求的备用线路, 继续使用当前出口",
            details={"current_noise_bps": current_rate.noise_bps},
        )

    async def run_optimization_cycle(self) -> None:
        if (
            self.settings.monitoring.optimization_enabled
            and not await self.database.get_runtime_state("legacy_optimization_notice")
        ):
            await self.database.add_event(
                code="LEGACY_OPTIMIZATION_IGNORED",
                level="warning",
                message="旧质量优化开关已停用, 线路选择由稳定性策略接管",
            )
            await self.database.set_runtime_state("legacy_optimization_notice", {"sent": True})

    async def run_maintenance_cycle(self) -> None:
        await self.database.cleanup_retention(**self.settings.retention.model_dump())
        await self.database.history.prune()
        await self.database.prune_traffic()

    async def _repeat(
        self,
        operation: Callable[[], Awaitable[None]],
        interval_seconds: float,
        *,
        immediate: bool,
        requires_enabled: bool = False,
    ) -> None:
        if not immediate:
            await asyncio.sleep(interval_seconds)
        while True:
            if requires_enabled:
                await self._enabled_event.wait()
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.database.add_event(
                    code="AUTOMATION_INTERNAL_ERROR",
                    level="error",
                    message="自动维护周期发生意外错误",
                    details={"operation": operation.__name__, "error": type(exc).__name__},
                )
            await asyncio.sleep(interval_seconds)

    async def run_forever(self) -> None:
        optimization_interval = self.settings.automation.optimization_interval_minutes * 60
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                self._repeat(
                    self.run_discovery_tick,
                    30,
                    immediate=True,
                    requires_enabled=True,
                )
            )
            tasks.create_task(
                self._repeat(
                    self.run_scheduler_tick,
                    10,
                    immediate=False,
                    requires_enabled=True,
                )
            )
            tasks.create_task(
                self._repeat(
                    self.run_optimization_cycle,
                    optimization_interval,
                    immediate=False,
                    requires_enabled=True,
                )
            )
            tasks.create_task(self._repeat(self.run_maintenance_cycle, 86_400, immediate=False))
