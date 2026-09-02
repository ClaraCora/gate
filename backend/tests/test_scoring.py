from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gate.domain import ProbeMetrics
from gate.scoring import calculate_quality, decide_switch


def test_quality_rewards_measured_reliability_and_speed() -> None:
    strong = calculate_quality(
        ProbeMetrics(
            availability_24h=0.99,
            latency_ms=45,
            throughput_mbps=80,
            api_speed_bps=150_000_000,
            uptime_ms=10 * 86_400_000,
            sessions=10,
        )
    )
    weak = calculate_quality(
        ProbeMetrics(
            availability_24h=0.60,
            latency_ms=700,
            throughput_mbps=2,
            api_speed_bps=10_000_000,
            uptime_ms=60_000,
            sessions=100,
        )
    )

    assert strong.total > weak.total
    assert 0 <= weak.total <= 100
    assert 0 <= strong.total <= 100


def test_switch_requires_hysteresis_confirmation_and_cooldown() -> None:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    below_threshold = decide_switch(
        current_score=80,
        candidate_score=90,
        confirmation_rounds=2,
        last_switch_at=None,
        now=now,
    )
    unconfirmed = decide_switch(
        current_score=80,
        candidate_score=93,
        confirmation_rounds=1,
        last_switch_at=None,
        now=now,
    )
    cooling_down = decide_switch(
        current_score=80,
        candidate_score=93,
        confirmation_rounds=2,
        last_switch_at=now - timedelta(minutes=5),
        now=now,
    )
    accepted = decide_switch(
        current_score=80,
        candidate_score=93,
        confirmation_rounds=2,
        last_switch_at=now - timedelta(hours=1),
        now=now,
    )

    assert not below_threshold.should_switch
    assert not unconfirmed.should_switch
    assert not cooling_down.should_switch
    assert accepted.should_switch
