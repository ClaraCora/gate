from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from gate.domain import ProbeMetrics, QualityBreakdown, SwitchDecision


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _log_score(value: float, target: float) -> float:
    return _clamp(math.log1p(max(value, 0.0)) / math.log1p(target))


def calculate_quality(metrics: ProbeMetrics) -> QualityBreakdown:
    availability = _clamp(metrics.availability_24h)
    latency = math.exp(-max(metrics.latency_ms - 20.0, 0.0) / 250.0)
    throughput = _log_score(metrics.throughput_mbps, 100.0)
    api_speed = _log_score(metrics.api_speed_bps / 1_000_000.0, 100.0)
    uptime = _log_score(metrics.uptime_ms / 86_400_000.0, 30.0)
    load = 1.0 / (1.0 + max(metrics.sessions, 0) / 50.0)
    total = 100.0 * (
        0.35 * availability
        + 0.25 * latency
        + 0.20 * throughput
        + 0.10 * api_speed
        + 0.05 * uptime
        + 0.05 * load
    )
    return QualityBreakdown(
        availability=availability,
        latency=latency,
        throughput=throughput,
        api_speed=api_speed,
        uptime=uptime,
        load=load,
        total=round(total, 3),
    )


def decide_switch(
    *,
    current_score: float,
    candidate_score: float,
    confirmation_rounds: int,
    last_switch_at: datetime | None,
    improvement_ratio: float = 1.15,
    required_confirmation_rounds: int = 2,
    cooldown: timedelta = timedelta(minutes=30),
    now: datetime | None = None,
) -> SwitchDecision:
    current_time = now or datetime.now(UTC)
    required_score = current_score * improvement_ratio
    if last_switch_at is not None and current_time - last_switch_at < cooldown:
        return SwitchDecision(False, "switch cooldown is active", required_score)
    if candidate_score < required_score:
        return SwitchDecision(False, "candidate improvement is below threshold", required_score)
    if confirmation_rounds < required_confirmation_rounds:
        return SwitchDecision(False, "candidate needs another confirmation round", required_score)
    return SwitchDecision(True, "candidate passed improvement and stability gates", required_score)
