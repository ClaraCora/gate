from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gate.config import DatabaseConfig, GateSettings, load_settings
from gate.controller import AutomationController
from gate.database import Database, NodeRecord
from gate.discovery import DiscoveryService
from gate.domain import VpnGateNode
from gate.monitoring import MonitoringService, NoisyCandidateError
from gate.network import TunnelConnectError
from gate.probes import DetectorError, EgressProbe, ProbeError
from gate.profiles import sanitize_openvpn_profile
from gate.selection import (
    EndpointHistory,
    HealthEvidence,
    ProfileEvidence,
    SelectionStore,
    endpoint,
    wilson_lower,
)
from gate.worker_protocol import Request
from sqlalchemy import select


@pytest.mark.asyncio
async def test_health_samples_are_throttled_and_candidate_checks_add_no_uptime(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = load_settings().model_copy(
        update={"database": DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")}
    )
    database = Database(settings.database.url)
    await database.initialize(settings.regions)
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    node = VpnGateNode(
        hostname="vpn-jp-history",
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
    async with database.sessions() as session:
        stored = await session.scalar(
            select(NodeRecord).where(NodeRecord.fingerprint == profile.fingerprint)
        )
    assert stored is not None

    now = datetime.now(UTC).timestamp()
    await database.history.observe(stored.id, region_id="jp", success=True, now=now)
    await database.history.observe(stored.id, region_id="jp", success=True, now=now + 60)
    await database.history.observe(stored.id, region_id="jp", success=None, now=now + 120)
    await database.history.observe(stored.id, region_id="jp", success=True, now=now + 300)
    await database.history.observe(
        stored.id, region_id="jp", success=True, now=now + 600, active=False
    )
    await database.history.verify_profile(stored.id, "203.0.113.77")

    async with database.sessions() as session:
        evidence = list(await session.scalars(select(HealthEvidence)))
        history = await session.get(EndpointHistory, "JP:128.211.249.131:udp:1195")
        profile_evidence = await session.get(ProfileEvidence, stored.fingerprint)
    assert len(evidence) == 3
    assert sum(row.observed_seconds for row in evidence) == pytest.approx(300)
    assert history is not None and history.continuous_seconds == pytest.approx(300)
    assert profile_evidence is not None and profile_evidence.egress_ip == "203.0.113.77"

    incident_at = now + 900
    assert await database.history.fail(stored.id, "incident-1", now=incident_at)
    assert not await database.history.fail(stored.id, "incident-1", now=incident_at + 1)
    async with database.sessions() as session:
        history = await session.get(EndpointHistory, "JP:128.211.249.131:udp:1195")
    assert history is not None
    assert history.failure_streak == 1
    assert history.cooldown_until == pytest.approx(incident_at + 1800)
    await database.close()


def test_wilson_lower_bound_keeps_single_success_below_reliable_history() -> None:
    assert wilson_lower(1, 1) < wilson_lower(99, 100)


async def setup_nodes(
    path: Path, encoded_profile: str, count: int = 3
) -> tuple[GateSettings, Database, DiscoveryService, list[NodeRecord]]:
    settings = load_settings().model_copy(
        update={"database": DatabaseConfig(url=f"sqlite+aiosqlite:///{path}")}
    )
    database = Database(settings.database.url)
    await database.initialize(settings.regions)
    discovery = DiscoveryService(database, feed_url="unused")
    nodes = []
    for index in range(count):
        encoded = base64.b64encode(
            base64.b64decode(encoded_profile).replace(b" 1195", f" {1195 + index}".encode())
        ).decode()
        profile = sanitize_openvpn_profile(encoded, expected_ip="128.211.249.131")
        node = VpnGateNode(
            hostname=f"vpn-jp-{index}",
            ip="128.211.249.131",
            country_long="Japan",
            country_code="JP",
            score=1000 + index,
            ping_ms=12,
            speed_bps=100_000_000,
            sessions=2,
            uptime_ms=86_400_000,
            total_users=1,
            total_traffic_bytes=1,
            log_type="2weeks",
            operator="Test",
            message="",
            openvpn_config_base64=encoded,
        )
        nodes.append((node, profile))
        discovery.profiles[profile.fingerprint] = profile
    await database.ingest_nodes(nodes, datetime.now(UTC))
    async with database.sessions() as session:
        stored = list(await session.scalars(select(NodeRecord).order_by(NodeRecord.id)))
    return settings, database, discovery, stored


@pytest.mark.asyncio
async def test_stable_history_outranks_one_success_and_unknown(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings, database, discovery, nodes = await setup_nodes(
        tmp_path / "ranking.db", encoded_profile
    )
    now = datetime.now(UTC).timestamp()
    stable, fresh, unknown = nodes
    async with database.sessions() as session, session.begin():
        session.add(
            EndpointHistory(
                endpoint=endpoint(stable),
                last_success=now,
                continuous_seconds=86400,
                last_observation=now,
                observation_session=database.history.session_id,
            )
        )
        session.add(ProfileEvidence(fingerprint=stable.fingerprint, verified_at=now))
        for index in range(289):
            session.add(
                HealthEvidence(
                    endpoint=endpoint(stable),
                    sampled_at=int(now - (289 - index) * 300),
                    success=True,
                    active=True,
                    observed_seconds=300 if index else 0,
                )
            )
    await database.history.observe(fresh.id, region_id="jp", success=True, active=False)
    await database.history.observe(unknown.id, region_id="jp", success=None)
    entries = await database.history.explain(
        "jp", settings.selection_policy, set(discovery.profiles)
    )
    assert [entry.node.id for entry in entries] == [stable.id, fresh.id, unknown.id]
    assert entries[0].evidence.tier == "stable"
    assert entries[0].evidence.observed_seconds == 86400
    assert entries[1].evidence.tier == "verified"
    assert entries[1].evidence.observed_seconds == 0
    assert entries[2].evidence.samples == 0
    assert entries[2].evidence.tier == "unverified"

    await database.history.fail(stable.id, "new-fault")
    await database.history.verify_profile(stable.id, "8.8.8.8")
    evidence = await database.history.snapshot(settings.selection_policy)
    assert evidence[stable.id].tier == "verified"
    assert evidence[stable.id].cooldown_until > now
    await database.close()


@pytest.mark.asyncio
async def test_cooldown_and_round_survive_switch_restart_and_profile_change(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings, database, discovery, nodes = await setup_nodes(
        tmp_path / "restart.db", encoded_profile
    )
    first, second, _ = nodes
    now = datetime.now(UTC).timestamp()
    await database.history.fail(first.id, "failure", now=now)
    await database.set_runtime_state("selection_round:jp", {"failed_endpoints": [endpoint(first)]})
    await database.complete_switch("jp", "a", second.id, "8.8.4.4")
    changed_encoded = base64.b64encode(
        base64.b64decode(encoded_profile).replace(b"AES-128-CBC", b"AES-256-CBC")
    ).decode()
    changed = sanitize_openvpn_profile(changed_encoded, expected_ip=first.ip)
    assert changed.fingerprint != first.fingerprint
    async with database.sessions() as session, session.begin():
        stored = await session.get(NodeRecord, first.id)
        assert stored is not None
        stored.fingerprint = changed.fingerprint
    await database.close()

    database = Database(settings.database.url)
    await database.initialize(settings.regions)
    entries = await database.history.explain(
        "jp", settings.selection_policy, {*discovery.profiles, changed.fingerprint}
    )
    changed_entry = next(entry for entry in entries if entry.node.id == first.id)
    assert "cooldown" in changed_entry.excluded
    assert "round_failed" in changed_entry.excluded
    assert not changed_entry.evidence.profile_verified
    assert changed_entry.evidence.cooldown_until == pytest.approx(now + 1800)
    assert (await database.get_runtime_state("selection_round:jp"))["failed_endpoints"] == [
        endpoint(first)
    ]
    async with database.sessions() as session:
        history = await session.get(EndpointHistory, endpoint(first))
    assert history is not None and history.failure_streak == 1
    await database.close()


@pytest.mark.asyncio
async def test_restart_does_not_infer_unobserved_uptime(
    tmp_path: Path, encoded_profile: str
) -> None:
    _, database, _, nodes = await setup_nodes(tmp_path / "gap.db", encoded_profile)
    now = datetime.now(UTC).timestamp()
    await database.history.observe(nodes[0].id, region_id="jp", success=True, now=now)
    database.history = SelectionStore(database)
    await database.history.observe(nodes[0].id, region_id="jp", success=True, now=now + 300)
    async with database.sessions() as session:
        samples = list(await session.scalars(select(HealthEvidence)))
    assert sum(row.observed_seconds for row in samples) == 0
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [True, False])
async def test_recovery_attempts_five_distinct_candidates_then_keeps_cooldowns(
    tmp_path: Path,
    encoded_profile: str,
    automatic: bool,
) -> None:
    settings, database, discovery, nodes = await setup_nodes(
        tmp_path / "batches.db", encoded_profile, 7
    )
    attempts: list[int] = []

    class FailedGateway:
        async def switch(self, region_id: str, node_id: int) -> object:
            attempts.append(node_id)
            raise TunnelConnectError("confirmed connection failure")

        async def probe_candidate(self, region_id: str, node_id: int) -> object:
            raise AssertionError("recovery must not start separate background validation")

    class Notifier:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, message: str) -> bool:
            self.messages.append(message)
            return True

    notifier = Notifier()
    controller = AutomationController(
        settings, database, discovery, FailedGateway(), notifier=notifier
    )
    region = await database.get_region("jp")
    assert region is not None
    before = datetime.now(UTC).timestamp()
    assert not await controller._attempt_region(region, automatic=automatic)
    assert attempts == [node.id for node in nodes[:5]]
    retry = await database.get_runtime_state("recovery:jp")
    assert retry["next_retry"] >= before + 60
    assert len(notifier.messages) == 1
    assert "切换失败: 5 次" in notifier.messages[0]
    assert not await controller._attempt_region(region, automatic=automatic)
    assert attempts == [node.id for node in nodes]
    assert not await controller._attempt_region(region, automatic=automatic)
    assert len(attempts) == 7
    assert len(notifier.messages) == 1
    assert len((await database.get_runtime_state("selection_round:jp"))["failed_endpoints"]) == 7
    await database.close()


@pytest.mark.asyncio
async def test_fixed_entry_failure_does_not_penalize_working_slot(
    tmp_path: Path,
    encoded_profile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gate import monitoring as monitoring_module

    settings, database, _, nodes = await setup_nodes(tmp_path / "fixed.db", encoded_profile)
    await database.complete_switch("jp", "a", nodes[0].id, "8.8.8.8")
    calls: list[int] = []

    class Worker:
        async def request(self, request: Request) -> dict[str, object]:
            return {
                "slots": [
                    {
                        "region_id": "jp",
                        "slot": "a",
                        "exists": True,
                        "socks_active": True,
                        "namespace_ip": "10.253.0.2",
                    }
                ]
            }

    async def probe(host: str, port: int, **kwargs: object) -> EgressProbe:
        calls.append(port)
        if port == 1080:
            return EgressProbe("8.8.8.8", "JP", 10)
        raise ProbeError("fixed path broken")

    monkeypatch.setattr(monitoring_module, "probe_socks_exit", probe)
    monitor = MonitoringService(settings, database, Worker())
    with pytest.raises(DetectorError, match="fixed entry needs repair"):
        await monitor.probe("127.0.0.1", 11081, expected_countries={"JP"})
    assert calls == [11081, 1080]
    assert (await database.history.snapshot(settings.selection_policy))[nodes[0].id].samples == 0
    await database.close()


@pytest.mark.asyncio
async def test_health_unknown_and_ip_change_keep_current_route(
    tmp_path: Path,
    encoded_profile: str,
) -> None:
    from gate.domain import RegionStatus

    settings, database, discovery, nodes = await setup_nodes(
        tmp_path / "health.db", encoded_profile
    )
    await database.complete_switch("jp", "a", nodes[0].id, "8.8.8.8")
    results: list[EgressProbe | ProbeError] = [
        ProbeError("one failed check"),
        DetectorError("source down"),
        EgressProbe("8.8.4.4", "JP", 10),
    ]

    class Gateway:
        async def switch(self, region_id: str, node_id: int) -> object:
            raise AssertionError("healthy or unconfirmed route must not switch")

        async def probe_candidate(self, region_id: str, node_id: int) -> object:
            raise AssertionError("no standby work requested")

    async def probe(*args: object, **kwargs: object) -> EgressProbe:
        result = results.pop(0)
        if isinstance(result, ProbeError):
            raise result
        return result

    controller = AutomationController(settings, database, discovery, Gateway(), probe=probe)
    await controller.run_health_cycle()
    await controller.run_health_cycle()
    assert (await database.get_runtime_state("health:jp"))["failures"] == 1
    await controller.run_health_cycle()
    region = await database.get_region("jp")
    slot = await database.get_active_slot("jp")
    assert region is not None and region.status == RegionStatus.HEALTHY
    assert region.active_egress_ip == "8.8.4.4"
    assert slot is not None and slot.egress_ip == "8.8.4.4"
    assert (await database.get_runtime_state("health:jp"))["failures"] == 0
    evidence = (await database.history.snapshot(settings.selection_policy))[nodes[0].id]
    assert evidence.cooldown_until == 0
    await database.close()


@pytest.mark.asyncio
async def test_one_health_exception_keeps_sibling_checks_and_recovery_running(
    tmp_path: Path,
    encoded_profile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gate.domain import RegionStatus

    settings, database, discovery, nodes = await setup_nodes(
        tmp_path / "isolated.db", encoded_profile
    )
    await database.complete_switch("jp", "a", nodes[0].id, "8.8.8.8")
    await database.complete_switch("kr", "a", nodes[1].id, "8.8.4.4")
    await database.set_region_status("jp", RegionStatus.UNAVAILABLE)
    for region_id, node in (("jp", nodes[0]), ("kr", nodes[1])):
        await database.set_runtime_state(
            f"health:{region_id}",
            {"node_id": node.id, "failures": 0, "next_check": 0, "next_full": 0},
        )
    calls: list[int] = []
    recoveries: list[str] = []

    async def probe(host: str, port: int, **kwargs: object) -> EgressProbe:
        calls.append(port)
        if port == 11081:
            raise RuntimeError("unexpected low-level transport error")
        return EgressProbe("8.8.4.4", "KR", 10)

    controller = AutomationController(settings, database, discovery, probe=probe)
    monkeypatch.setattr(controller, "_launch_recovery", lambda r: recoveries.append(r.id))
    monkeypatch.setattr(controller, "_schedule_standby_validation", lambda: None)
    await controller.run_scheduler_tick()
    assert calls == [11081, 11082]
    assert "jp" in recoveries
    assert "kr" not in recoveries
    state = await database.get_runtime_state("health:jp")
    assert state["next_check"] > datetime.now(UTC).timestamp() + 50
    assert state["failures"] == 0
    checks = await database.list_active_health_probes(
        since=datetime.now(UTC).replace(hour=0), until=datetime.now(UTC)
    )
    assert {(p.region_id, p.result) for p in checks} == {("jp", "unknown"), ("kr", "succeeded")}
    evidence = (await database.history.snapshot(settings.selection_policy))[nodes[0].id]
    assert evidence.samples == 0 and evidence.cooldown_until == 0
    events = await database.list_events()
    errors = [e for e in events if e.code == "ACTIVE_HEALTH_INTERNAL_ERROR"]
    assert len(errors) == 1 and errors[0].details == {"error": "RuntimeError"}
    # A subsequent scheduler tick must obey the delay instead of hammering the
    # broken entry every ten seconds or leaving previous probes in flight.
    await controller.run_scheduler_tick()
    assert calls == [11081, 11082]
    await database.close()


@pytest.mark.asyncio
async def test_three_failures_confirm_one_incident_until_route_recovers(
    tmp_path: Path,
    encoded_profile: str,
) -> None:
    from gate.domain import RegionMode, RegionStatus

    settings, database, discovery, nodes = await setup_nodes(
        tmp_path / "confirm.db", encoded_profile
    )
    await database.complete_switch("jp", "a", nodes[0].id, "8.8.8.8")
    await database.set_region_mode("jp", RegionMode.LOCKED)

    async def probe(*args: object, **kwargs: object) -> EgressProbe:
        raise ProbeError("confirmed with independent controls")

    controller = AutomationController(settings, database, discovery, probe=probe)
    for index in range(4):
        await controller.run_health_cycle()
        region = await database.get_region("jp")
        assert region is not None
        assert region.status == (RegionStatus.HEALTHY if index < 2 else RegionStatus.UNAVAILABLE)
    async with database.sessions() as session:
        history = await session.get(EndpointHistory, endpoint(nodes[0]))
    assert history is not None and history.failure_streak == 1
    events = await database.list_events(limit=100)
    assert sum(event.code == "ACTIVE_OUTAGE_CONFIRMED" for event in events) == 1
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_windows", [False, True])
async def test_noise_replacement_requires_three_fresh_windows(
    tmp_path: Path,
    encoded_profile: str,
    monkeypatch: pytest.MonkeyPatch,
    valid_windows: bool,
) -> None:
    settings, database, _, nodes = await setup_nodes(tmp_path / "noise.db", encoded_profile)
    await database.reserve_slot("jp", "b", nodes[0].id)
    await database.history.verify_profile(nodes[0].id, "8.8.8.8")
    await database.history.set_noise(nodes[0].id, 10)
    await database.set_runtime_state(
        "candidate_noise:jp",
        {
            "node_id": nodes[0].id,
            "below_threshold": True,
            "average_bps": 10,
        },
    )
    await database.set_runtime_state(
        "noise_requirement:jp",
        {
            "node_id": nodes[0].id,
            "max_bps": 1024,
        },
    )

    class Worker:
        async def request(self, request: Request) -> dict[str, object]:
            return {
                "counters": [
                    {
                        "scope": "tunnel",
                        "source": "gate-jp-b",
                        "identity": "boot",
                        "noise_bytes": 100,
                    }
                ]
                if valid_windows
                else []
            }

    async def no_wait(seconds: float) -> None:
        assert seconds >= 20

    monkeypatch.setattr("gate.monitoring.asyncio.sleep", no_wait)
    monitor = MonitoringService(settings, database, Worker())
    if valid_windows:
        await monitor.observe_candidate("jp", "b")
    else:
        with pytest.raises(NoisyCandidateError):
            await monitor.observe_candidate("jp", "b")
    measured = await database.get_runtime_state("candidate_noise:jp")
    assert measured["valid_windows"] == (3 if valid_windows else 0)
    assert measured["below_threshold"] is valid_windows
    assert measured["average_bps"] == (0 if valid_windows else None)
    await database.close()
