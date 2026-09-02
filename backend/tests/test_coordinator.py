from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gate.config import load_settings
from gate.coordinator import SwitchCoordinator, SwitchError
from gate.database import Database
from gate.discovery import DiscoveryService
from gate.domain import RegionMode, RegionStatus, VpnGateNode
from gate.probes import EgressProbe
from gate.profiles import sanitize_openvpn_profile
from gate.worker_protocol import DestroySlotRequest, InspectRequest, ProvisionSlotRequest, Request


class FakeWorker:
    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.inventory: list[dict[str, object]] = []

    async def request(self, request: Request) -> dict[str, object]:
        self.requests.append(request)
        if isinstance(request, ProvisionSlotRequest):
            return {"namespace_ip": "10.253.0.2"}
        if isinstance(request, InspectRequest):
            return {"slots": self.inventory}
        return {}


class FakeHaProxy:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str, str]] = []

    async def ready(self, region_id: str, slot: str) -> None:
        self.commands.append(("ready", region_id, slot))

    async def drain(self, region_id: str, slot: str) -> None:
        self.commands.append(("drain", region_id, slot))

    async def disable(self, region_id: str, slot: str) -> None:
        self.commands.append(("disable", region_id, slot))


class ProbeSequence:
    def __init__(self, *results: EgressProbe) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, int]] = []

    async def __call__(
        self,
        host: str,
        port: int,
        *,
        expected_countries: set[str] | frozenset[str],
        timeout_seconds: float = 12.0,
    ) -> EgressProbe:
        del expected_countries, timeout_seconds
        self.calls.append((host, port))
        return self.results.pop(0)


async def _seed(tmp_path: Path, encoded_profile: str) -> tuple[Database, DiscoveryService, int]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'coordinator.db').as_posix()}")
    settings = load_settings()
    await database.initialize(settings.regions)
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    node = VpnGateNode(
        hostname="vpn-jp-test",
        ip="128.211.249.131",
        country_long="Japan",
        country_code="JP",
        score=100,
        ping_ms=10,
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
    discovery = DiscoveryService(database, feed_url="unused")
    discovery.profiles[profile.fingerprint] = profile
    candidate = (await database.list_candidates("jp"))[0]
    return database, discovery, candidate.id


@pytest.mark.asyncio
async def test_switch_provisions_tests_and_commits_active_slot(
    tmp_path: Path, encoded_profile: str
) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    worker = FakeWorker()
    haproxy = FakeHaProxy()
    probe = ProbeSequence(
        EgressProbe("203.0.113.10", "JP", 110.0),
        EgressProbe("203.0.113.10", "JP", 115.0),
    )
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
        probe=probe,
    )

    result = await coordinator.switch("jp", node_id)

    assert result.slot == "a"
    assert probe.calls == [("10.253.0.2", 1080), ("127.0.0.1", 11081)]
    assert haproxy.commands == [("disable", "jp", "a"), ("ready", "jp", "a")]
    assert isinstance(worker.requests[0], DestroySlotRequest)
    assert isinstance(worker.requests[1], ProvisionSlotRequest)
    active = await database.get_active_slot("jp")
    region = await database.get_region("jp")
    assert active is not None and active.slot == "a" and active.node_id == node_id
    assert region is not None and region.status == RegionStatus.HEALTHY
    await database.close()


@pytest.mark.asyncio
async def test_switch_rolls_back_when_stable_port_reaches_wrong_exit(
    tmp_path: Path, encoded_profile: str
) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    worker = FakeWorker()
    haproxy = FakeHaProxy()
    probe = ProbeSequence(
        EgressProbe("203.0.113.10", "JP", 110.0),
        EgressProbe("203.0.113.11", "JP", 115.0),
    )
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
        probe=probe,
    )

    with pytest.raises(SwitchError, match="different exit"):
        await coordinator.switch("jp", node_id)

    assert haproxy.commands == [
        ("disable", "jp", "a"),
        ("ready", "jp", "a"),
        ("disable", "jp", "a"),
    ]
    assert isinstance(worker.requests[-1], DestroySlotRequest)
    active = await database.get_active_slot("jp")
    region = await database.get_region("jp")
    assert active is None
    assert region is not None and region.status == RegionStatus.UNAVAILABLE
    await database.close()


@pytest.mark.asyncio
async def test_failed_failover_keeps_unavailable_old_route_disabled(
    tmp_path: Path, encoded_profile: str
) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    await database.complete_switch("jp", "a", node_id)
    await database.set_region_status("jp", RegionStatus.UNAVAILABLE)
    worker = FakeWorker()
    haproxy = FakeHaProxy()
    probe = ProbeSequence(
        EgressProbe("203.0.113.10", "JP", 110.0),
        EgressProbe("203.0.113.11", "JP", 115.0),
    )
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
        probe=probe,
    )

    with pytest.raises(SwitchError, match="different exit"):
        await coordinator.switch("jp", node_id)

    region = await database.get_region("jp")
    assert region is not None and region.status == RegionStatus.UNAVAILABLE
    assert ("ready", "jp", "a") not in haproxy.commands
    assert haproxy.commands[-2:] == [("disable", "jp", "b"), ("disable", "jp", "a")]
    await database.close()


@pytest.mark.asyncio
async def test_candidate_probe_uses_inactive_slot_without_touching_haproxy(
    tmp_path: Path, encoded_profile: str
) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    worker = FakeWorker()
    haproxy = FakeHaProxy()
    probe = ProbeSequence(EgressProbe("203.0.113.10", "JP", 84.0))
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
        probe=probe,
    )

    result = await coordinator.probe_candidate("jp", node_id)

    assert result.slot == "a"
    assert result.egress_ip == "203.0.113.10"
    assert probe.calls == [("10.253.0.2", 1080)]
    assert haproxy.commands == [("disable", "jp", "a")]
    assert isinstance(worker.requests[0], DestroySlotRequest)
    assert isinstance(worker.requests[1], ProvisionSlotRequest)
    assert isinstance(worker.requests[-1], DestroySlotRequest)
    assert await database.get_active_slot("jp") is None
    await database.close()


@pytest.mark.asyncio
async def test_disabling_region_destroys_both_slots(tmp_path: Path, encoded_profile: str) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    await database.complete_switch("jp", "a", node_id)
    worker = FakeWorker()
    haproxy = FakeHaProxy()
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
    )

    await coordinator.set_region_mode("jp", RegionMode.DISABLED)

    region = await database.get_region("jp")
    assert region is not None
    assert region.mode == RegionMode.DISABLED
    assert region.status == RegionStatus.DISABLED
    assert region.active_node_id is None
    assert haproxy.commands == [("disable", "jp", "a"), ("disable", "jp", "b")]
    assert DestroySlotRequest(action="destroy_slot", region_id="jp", slot="a") in worker.requests
    assert DestroySlotRequest(action="destroy_slot", region_id="jp", slot="b") in worker.requests
    assert all(slot.state == "empty" for slot in await database.list_slots())
    await database.close()


@pytest.mark.asyncio
async def test_reconcile_restores_only_healthy_active_slot(
    tmp_path: Path, encoded_profile: str
) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    await database.complete_switch("jp", "b", node_id)
    worker = FakeWorker()
    worker.inventory = [
        {
            "region_id": "jp",
            "slot": "b",
            "exists": True,
            "tunnel_up": True,
            "openvpn_active": True,
            "socks_active": True,
        }
    ]
    haproxy = FakeHaProxy()
    probe = ProbeSequence(EgressProbe("203.0.113.10", "JP", 42.0))
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
        probe=probe,
    )

    await coordinator.reconcile()

    assert ("ready", "jp", "b") in haproxy.commands
    assert ("disable", "jp", "a") in haproxy.commands
    assert probe.calls == [("10.253.0.6", 1080)]
    active = await database.get_active_slot("jp")
    assert active is not None and active.slot == "b"
    await database.close()


@pytest.mark.asyncio
async def test_second_switch_cleans_drained_slot(tmp_path: Path, encoded_profile: str) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    worker = FakeWorker()
    haproxy = FakeHaProxy()
    probe = ProbeSequence(
        EgressProbe("203.0.113.10", "JP", 110.0),
        EgressProbe("203.0.113.10", "JP", 115.0),
        EgressProbe("203.0.113.11", "JP", 105.0),
        EgressProbe("203.0.113.11", "JP", 108.0),
    )
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
        probe=probe,
        drain_seconds=0,
    )

    first = await coordinator.switch("jp", node_id)
    second = await coordinator.switch("jp", node_id)
    while coordinator._cleanup_tasks:
        await asyncio.sleep(0)

    assert first.slot == "a"
    assert second.slot == "b"
    assert await database.get_slot("jp", "a") is not None
    assert (await database.get_slot("jp", "a")).state == "empty"  # type: ignore[union-attr]
    active = await database.get_active_slot("jp")
    assert active is not None and active.slot == "b"
    assert ("drain", "jp", "a") in haproxy.commands
    assert haproxy.commands[-1] == ("disable", "jp", "a")
    assert isinstance(worker.requests[-1], DestroySlotRequest)
    await database.close()


@pytest.mark.asyncio
async def test_reconcile_destroys_non_active_runtime_slot(
    tmp_path: Path, encoded_profile: str
) -> None:
    database, discovery, node_id = await _seed(tmp_path, encoded_profile)
    await database.complete_switch("jp", "a", node_id)
    await database.complete_switch("jp", "b", node_id)
    worker = FakeWorker()
    worker.inventory = [
        {
            "region_id": "jp",
            "slot": "a",
            "exists": True,
            "tunnel_up": True,
            "openvpn_active": True,
            "socks_active": True,
        },
        {
            "region_id": "jp",
            "slot": "b",
            "exists": True,
            "tunnel_up": True,
            "openvpn_active": True,
            "socks_active": True,
        },
    ]
    haproxy = FakeHaProxy()
    probe = ProbeSequence(EgressProbe("203.0.113.11", "JP", 42.0))
    coordinator = SwitchCoordinator(
        database,
        discovery,
        worker=worker,
        haproxy=haproxy,
        probe=probe,
    )

    await coordinator.reconcile()

    old_slot = await database.get_slot("jp", "a")
    active = await database.get_active_slot("jp")
    assert old_slot is not None and old_slot.state == "empty"
    assert active is not None and active.slot == "b"
    assert DestroySlotRequest(action="destroy_slot", region_id="jp", slot="a") in worker.requests
    await database.close()
