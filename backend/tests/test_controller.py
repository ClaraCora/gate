from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gate.config import DatabaseConfig, SelectionConfig, load_settings
from gate.controller import AutomationController
from gate.database import Database
from gate.discovery import DiscoveryService
from gate.domain import FeedParseResult, RegionMode, RegionStatus, VpnGateNode
from gate.probes import EgressProbe, ProbeError
from gate.profiles import sanitize_openvpn_profile


class FakeCoordinator:
    def __init__(self) -> None:
        self.switches: list[tuple[str, int]] = []

    async def switch(self, region_id: str, node_id: int) -> object:
        self.switches.append((region_id, node_id))
        return object()

    async def probe_candidate(self, region_id: str, node_id: int) -> object:
        del region_id, node_id
        return object()


@pytest.mark.asyncio
async def test_discovery_cycle_attempts_current_candidate_for_unavailable_region(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = load_settings().model_copy(
        update={"database": DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'auto.db'}")}
    )
    database = Database(settings.database.url)
    await database.initialize(settings.regions)
    node = VpnGateNode(
        hostname="vpn-jp-auto",
        ip="128.211.249.131",
        country_long="Japan",
        country_code="JP",
        score=1000,
        ping_ms=12,
        speed_bps=100_000_000,
        sessions=2,
        uptime_ms=86_400_000,
        total_users=1,
        total_traffic_bytes=1,
        log_type="2weeks",
        operator="Test",
        message="",
        openvpn_config_base64=encoded_profile,
    )

    async def fetcher() -> FeedParseResult:
        return FeedParseResult(nodes=(node,), rejected_rows=0, warnings=())

    discovery = DiscoveryService(database, feed_url="unused", fetcher=fetcher)
    coordinator = FakeCoordinator()
    controller = AutomationController(settings, database, discovery, coordinator)

    await controller.run_discovery_cycle()

    candidate = (await database.list_candidates("jp"))[0]
    assert coordinator.switches == [("jp", candidate.id)]
    await database.close()


@pytest.mark.asyncio
async def test_optimization_requires_two_measured_improvement_rounds(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = load_settings().model_copy(
        update={
            "database": DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'optimize.db'}"),
            "selection": SelectionConfig(
                improvement_ratio=1.15,
                confirmation_rounds=2,
                switch_cooldown_minutes=0,
                active_failure_threshold=3,
            ),
        }
    )
    database = Database(settings.database.url)
    await database.initialize(settings.regions)
    second_profile_b64 = base64.b64encode(
        base64.b64decode(encoded_profile).replace(b" 1195", b" 1196")
    ).decode()
    active_profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    candidate_profile = sanitize_openvpn_profile(second_profile_b64, expected_ip="128.211.249.131")

    def node(name: str, score: int, speed: int, profile: str) -> VpnGateNode:
        return VpnGateNode(
            hostname=name,
            ip="128.211.249.131",
            country_long="Japan",
            country_code="JP",
            score=score,
            ping_ms=20,
            speed_bps=speed,
            sessions=5,
            uptime_ms=2_592_000_000,
            total_users=1,
            total_traffic_bytes=1,
            log_type="2weeks",
            operator="Test",
            message="",
            openvpn_config_base64=profile,
        )

    observed_at = datetime.now(UTC)
    await database.ingest_nodes(
        [
            (node("active", 100, 10_000_000, encoded_profile), active_profile),
            (node("candidate", 200, 300_000_000, second_profile_b64), candidate_profile),
        ],
        observed_at,
    )
    candidates = await database.list_candidates("jp")
    active = next(item for item in candidates if item.fingerprint == active_profile.fingerprint)
    candidate = next(
        item for item in candidates if item.fingerprint == candidate_profile.fingerprint
    )
    await database.complete_switch("jp", "a", active.id)
    for _ in range(3):
        await database.record_probe(
            region_id="jp",
            node_id=active.id,
            probe_type="active_health",
            result="succeeded",
            latency_ms=900.0,
        )
        await database.record_probe(
            region_id="jp",
            node_id=candidate.id,
            probe_type="candidate",
            result="succeeded",
            latency_ms=30.0,
        )
    discovery = DiscoveryService(database, feed_url="unused")
    discovery.profiles[active_profile.fingerprint] = active_profile
    discovery.profiles[candidate_profile.fingerprint] = candidate_profile
    coordinator = FakeCoordinator()
    controller = AutomationController(settings, database, discovery, coordinator)

    await controller.run_optimization_cycle()
    assert coordinator.switches == []
    await controller.run_optimization_cycle()
    assert coordinator.switches == [("jp", candidate.id)]
    await database.close()


@pytest.mark.asyncio
async def test_locked_region_stays_unavailable_after_health_threshold(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = load_settings().model_copy(
        update={
            "database": DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'locked.db'}"),
            "selection": SelectionConfig(
                improvement_ratio=1.15,
                confirmation_rounds=2,
                switch_cooldown_minutes=30,
                active_failure_threshold=1,
            ),
        }
    )
    database = Database(settings.database.url)
    await database.initialize(settings.regions)
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    node = VpnGateNode(
        hostname="vpn-jp-locked",
        ip="128.211.249.131",
        country_long="Japan",
        country_code="JP",
        score=1000,
        ping_ms=12,
        speed_bps=100_000_000,
        sessions=2,
        uptime_ms=86_400_000,
        total_users=1,
        total_traffic_bytes=1,
        log_type="2weeks",
        operator="Test",
        message="",
        openvpn_config_base64=encoded_profile,
    )
    await database.ingest_nodes([(node, profile)], datetime.now(UTC))
    candidate = (await database.list_candidates("jp"))[0]
    await database.complete_switch("jp", "a", candidate.id)
    await database.set_region_mode("jp", RegionMode.LOCKED)
    discovery = DiscoveryService(database, feed_url="unused")
    discovery.profiles[profile.fingerprint] = profile
    coordinator = FakeCoordinator()

    async def failed_probe(*args: object, **kwargs: object) -> EgressProbe:
        del args, kwargs
        raise ProbeError("locked route failed")

    controller = AutomationController(
        settings,
        database,
        discovery,
        coordinator,
        probe=failed_probe,
    )

    await controller.run_health_cycle()

    region = await database.get_region("jp")
    assert region is not None and region.status == RegionStatus.UNAVAILABLE
    assert coordinator.switches == []
    events = await database.list_events()
    assert any(event.code == "LOCKED_REGION_UNAVAILABLE" for event in events)
    await database.close()


@pytest.mark.asyncio
async def test_disabled_automation_skips_periodic_work_until_reenabled(tmp_path: Path) -> None:
    settings = load_settings().model_copy(
        update={"database": DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'toggle.db'}")}
    )
    database = Database(settings.database.url)
    controller = AutomationController(
        settings,
        database,
        DiscoveryService(database, feed_url="unused"),
        FakeCoordinator(),
    )
    controller.set_enabled(False)
    called = asyncio.Event()

    async def operation() -> None:
        called.set()

    task = asyncio.create_task(
        controller._repeat(operation, 0.01, immediate=True, requires_enabled=True)
    )
    await asyncio.sleep(0.03)
    assert called.is_set() is False

    controller.set_enabled(True)
    await asyncio.wait_for(called.wait(), timeout=0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await database.close()
