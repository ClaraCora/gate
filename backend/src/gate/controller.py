from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
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
from gate.scoring import calculate_quality, decide_switch


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

    async def _record_automatic_switch_failure(
        self,
        region: RegionRecord,
        node_id: int,
        exc: Exception,
        *,
        event_code: str = "AUTO_CANDIDATE_FAILED",
        event_message: str | None = None,
    ) -> None:
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
            f"连续自动切换失败: {failure_streak} 次\n"
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
        active = await self.database.get_active_slot(region.id)
        candidates = await self.database.list_candidates(region.id)
        failed_nodes = await self.database.list_switch_failure_nodes(region.id)
        noisy = await self.database.get_runtime_state(f"noise_exclusions:{region.id}")
        now = utc_now().timestamp()
        eligible = [
            candidate
            for candidate in candidates
            if (active is None or candidate.id != active.node_id)
            and candidate.fingerprint in self.discovery.profiles
            and noisy.get(str(candidate.id), 0) <= now
        ]
        if not eligible:
            await self.database.add_event(
                code="AUTO_REGION_UNAVAILABLE",
                level="error",
                message=f"{region.name} 没有可用且不重复的自动候选节点",
                region_id=region.id,
                details={"attempted": 0},
            )
            return False

        unfailed = [candidate for candidate in eligible if candidate.id not in failed_nodes]
        if not unfailed:
            # Every currently eligible candidate failed in the previous round. Start
            # a new round, but leave this cycle untouched so failures are not retried.
            await self.database.reset_switch_failures(region.id)
            await self.database.add_event(
                code="AUTO_REGION_UNAVAILABLE",
                level="error",
                message=f"{region.name} 的所有自动候选节点均已失败, 将在下一周期重试",
                region_id=region.id,
                details={"attempted": 0, "reset_failures": True},
            )
            return False

        attempt_limit = self.settings.automation.max_candidates_per_cycle
        # Shuffle once per round. Failed candidates remain excluded until every
        # eligible candidate has failed; no candidate is retried in this cycle.
        selected = random.sample(unfailed, k=min(attempt_limit, len(unfailed)))
        attempted = 0
        for candidate in selected:
            attempted += 1
            try:
                operation = partial(self.coordinator.switch, region.id, candidate.id)
                if automatic:
                    await self._run_automatic_job(
                        kind="auto_switch",
                        region_id=region.id,
                        node_id=candidate.id,
                        operation=operation,
                    )
                else:
                    await operation()
            except Exception as exc:
                if isinstance(exc, GateError) and exc.code == "SWITCH_BUSY":
                    raise
                if isinstance(exc, GateError) and exc.code == "DETECTOR_UNAVAILABLE":
                    await self.database.add_event(
                        code="SWITCH_DETECTOR_UNAVAILABLE",
                        level="warning",
                        region_id=region.id,
                        message="候选检测服务不可用, 暂停本轮切换并保留原线路",
                    )
                    return False
                if isinstance(exc, GateError) and exc.code == "NOISY_CANDIDATE":
                    noisy[str(candidate.id)] = utc_now().timestamp() + 86_400
                    await self.database.set_runtime_state(f"noise_exclusions:{region.id}", noisy)
                failed_nodes.add(candidate.id)
                if automatic:
                    await self._record_automatic_switch_failure(region, candidate.id, exc)
                else:
                    await self.database.record_switch_failure(region.id, candidate.id)
                continue
            if automatic:
                self.failure_counts[region.id] = 0
            await self.database.reset_switch_failure_streak(region.id)
            return True

        if all(candidate.id in failed_nodes for candidate in eligible):
            await self.database.reset_switch_failures(region.id)
        await self.database.add_event(
            code="AUTO_REGION_UNAVAILABLE",
            level="error",
            message=f"{region.name} 没有可用且不重复的自动候选节点",
            region_id=region.id,
            details={
                "attempted": attempted,
                "reset_failures": all(candidate.id in failed_nodes for candidate in eligible),
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
                await self._check_health(region, due_only=due_only, index=index, total=len(regions))

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
                **credentials,
            )
        except GateError as exc:
            if exc.code == "DETECTOR_UNAVAILABLE":
                # No success/failure sample: this is unknown, not a failed exit.
                state["next_check"] = now + min(60, policy.health_interval_seconds)
                state["detector_error"] = str(exc)
                await self.database.set_runtime_state(key, state)
                await self.database.add_event(
                    code="ACTIVE_HEALTH_DETECTOR_FAILED",
                    level="warning",
                    message=f"{region.name} 的检测服务暂不可用, 保留当前线路",
                    region_id=region.id,
                    details={"error": str(exc)},
                )
                return
            # A manual switch may have completed while this probe was in flight.
            # Never attribute the old route's failure to the newly active slot.
            current = await self.database.get_active_slot(region.id)
            if current is None or current.node_id != active.node_id or current.slot != active.slot:
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
            state.update(failures=failures, next_check=now + policy.failure_confirm_seconds)
            await self.database.add_event(
                code="ACTIVE_HEALTH_CHECK_FAILED",
                level="warning",
                message=f"{region.name} 的活动出口检查失败, 正在复核",
                region_id=region.id,
                details={"failure_count": failures, "error_code": exc.code},
            )
            if failures >= self.settings.selection.active_failure_threshold:
                state["next_check"] = now + policy.health_interval_seconds
                await self.database.set_region_status(region.id, RegionStatus.UNAVAILABLE)
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
            try:
                await self.database.set_active_egress_ip(region.id, probe.egress_ip)
            except ValueError as exc:
                await self.database.add_event(
                    code="ACTIVE_HEALTH_DUPLICATE_EXIT",
                    level="error",
                    message=f"{region.name} 检测到与同组入口重复的实际出口 IP, 已停止使用当前状态",
                    region_id=region.id,
                    details={"egress_ip": probe.egress_ip, "error": str(exc)},
                )
                await self.database.set_region_status(region.id, RegionStatus.UNAVAILABLE)
                return
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
            await self.database.set_region_status(region.id, RegionStatus.HEALTHY)
            self.failure_counts[region.id] = 0
            state.update(
                failures=0, next_check=now + policy.health_interval_seconds, detector_error=None
            )
            if full or probe.egress_ip != region.active_egress_ip:
                state["next_full"] = now + policy.full_verification_hours * 3600
            await self.database.set_runtime_state(f"recovery:{region.id}", {})
        await self.database.set_runtime_state(key, state)

    async def _recover(self, region: RegionRecord) -> None:
        key = f"recovery:{region.id}"
        state = await self.database.get_runtime_state(key)
        now = utc_now().timestamp()
        if (
            state.get("next_retry", 0) > now
            or self._attempt_locks.get(region.id, asyncio.Lock()).locked()
        ):
            return
        delays = (60, 120, 300, 900, 1800)
        attempt = state.get("attempt", 0)
        # Reserve the next retry before network work; restart cannot create a tight loop.
        await self.database.set_runtime_state(
            key,
            {
                "attempt": attempt + 1,
                "next_retry": now + delays[min(attempt, 4)],
            },
        )
        refresh = await self.database.get_runtime_state("recovery_discovery")
        if refresh.get("next_refresh", 0) <= now:
            await self.database.set_runtime_state("recovery_discovery", {"next_refresh": now + 300})
            try:
                await self.discovery.refresh(minimum_interval=300)
            except GateError:
                if not self.discovery.profiles:
                    return
        try:
            if await self._attempt_region(region):
                await self.database.set_runtime_state(key, {})
        except SwitchBusyError:
            await self.database.set_runtime_state(key, {"attempt": attempt, "next_retry": now + 60})

    def _launch_recovery(self, region: RegionRecord) -> None:
        task = self._recovery_jobs.get(region.id)
        if task is not None and not task.done():
            return

        async def recover() -> None:
            try:
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

    async def queue_noise(self, region_id: str) -> None:
        self._noise_pending.add(region_id)

    def _launch_noise_replacement(self, region_id: str) -> None:
        task = self._noise_jobs.get(region_id)
        if task is not None and not task.done():
            return

        async def replace() -> None:
            try:
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
        if region is None or not region.enabled or region.status != RegionStatus.HEALTHY:
            return
        state = await self.database.get_runtime_state(f"noise_action:{region_id}")
        now = utc_now().timestamp()
        if state.get("next_action", 0) > now:
            return
        cooldown = self.settings.monitoring.noise_switch_cooldown_minutes * 60
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
        if region.mode == RegionMode.LOCKED:
            if self.notifier:
                await self.notifier.send(
                    f"Gate 广播开销提醒\n{region.name} 已锁定, 请人工检查隧道流量。"
                )
            return
        noisy = await self.database.get_runtime_state(f"noise_exclusions:{region_id}")
        noisy[str(region.active_node_id)] = now + 86_400
        await self.database.set_runtime_state(f"noise_exclusions:{region_id}", noisy)
        await self._attempt_region(region)

    async def _optimize_region(self, region: RegionRecord) -> None:
        active = await self.database.get_active_slot(region.id)
        if (
            active is None
            or active.node_id is None
            or not self.settings.monitoring.optimization_enabled
        ):
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
        failed = await self.database.list_switch_failure_nodes(region.id)
        noisy = await self.database.get_runtime_state(f"noise_exclusions:{region.id}")
        now = utc_now().timestamp()
        for candidate in candidates:
            if (
                candidate.id == active.node_id
                or candidate.id in failed
                or noisy.get(str(candidate.id), 0) > now
            ):
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
        except Exception as exc:
            await self._record_automatic_switch_failure(
                region,
                best_node_id,
                exc,
                event_code="AUTO_OPTIMIZATION_FAILED",
                event_message=f"{region.name} 的线路质量优化失败",
            )
            return
        await self.database.reset_switch_failure_streak(region.id)
        await self.database.add_event(
            code="AUTO_QUALITY_SWITCH",
            message=f"{region.name} 已自动切换到实测质量更高的出口",
            region_id=region.id,
            node_id=best_node_id,
            details={"previous_score": current_score, "candidate_score": best_score},
        )

    async def run_optimization_cycle(self) -> None:
        if not self.settings.monitoring.optimization_enabled:
            return
        if self.monitoring and (await self.monitoring.budget_status())["optional_work_paused"]:
            return
        for region, _candidate_count in await self.database.list_regions():
            if (
                region.enabled
                and region.mode == RegionMode.AUTO
                and region.status == RegionStatus.HEALTHY
            ):
                await self._optimize_region(region)

    async def run_maintenance_cycle(self) -> None:
        await self.database.cleanup_retention(**self.settings.retention.model_dump())
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
            except Exception:
                await self.database.add_event(
                    code="AUTOMATION_INTERNAL_ERROR",
                    level="error",
                    message="自动维护周期发生意外错误",
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
