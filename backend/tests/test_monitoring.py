from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from gate import monitoring as monitoring_module
from gate.config import load_settings
from gate.database import Database, utc_now
from gate.errors import GateError
from gate.http_usage import bounded_get, usage_recorder
from gate.monitoring import MonitoringService
from gate.network import TunnelConnectError
from gate.probes import (
    DetectorError,
    EgressProbe,
    ProbeError,
    UnconfirmedRouteError,
    probe_socks_exit,
)
from gate.selection import failure_category
from gate.worker_protocol import Request


@pytest.mark.asyncio
async def test_light_health_one_request_but_changed_ip_needs_independent_verification() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return (
            httpx.Response(200, text="ip=8.8.8.8\nloc=JP\n")
            if "cloudflare" in request.url.host
            else httpx.Response(200, json={"ip": "8.8.8.8"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_socks_exit(
            "127.0.0.1",
            11081,
            expected_countries={"JP"},
            full=False,
            previous_ip="8.8.8.8",
            client=client,
        )
        assert result.egress_ip == "8.8.8.8" and len(calls) == 1
        calls.clear()
        await probe_socks_exit(
            "127.0.0.1",
            11081,
            expected_countries={"JP"},
            full=False,
            previous_ip="1.1.1.1",
            client=client,
        )
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_independent_fallback_avoids_false_outage_when_cloudflare_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "cloudflare" in request.url.host:
            return httpx.Response(429)
        return httpx.Response(200, json={"ip": "8.8.8.8", "country_code": "JP"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_socks_exit(
            "127.0.0.1",
            1,
            expected_countries={"JP"},
            full=False,
            previous_ip="8.8.8.8",
            client=client,
        )
        assert result.egress_ip == "8.8.8.8"
        assert result.country_code == ""  # Country wasn't re-measured by ipify.


@pytest.mark.asyncio
async def test_independent_fallback_reaches_plain_ip_provider() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host in {
            "www.cloudflare.com",
            "api.ipify.org",
            "ipwho.is",
        }:
            return httpx.Response(503)
        return httpx.Response(200, text="110.67.14.151\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_socks_exit(
            "127.0.0.1",
            1,
            expected_countries={"JP"},
            full=False,
            previous_ip="110.67.14.151",
            client=client,
        )

    assert result.egress_ip == "110.67.14.151"
    assert calls == [
        "www.cloudflare.com",
        "api.ipify.org",
        "ipwho.is",
        "ifconfig.me",
    ]


@pytest.mark.asyncio
async def test_failed_detector_requests_are_unknown_until_tunnel_failure_is_confirmed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        with pytest.raises(DetectorError):
            await probe_socks_exit("127.0.0.1", 1, expected_countries={"JP"}, client=client)

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(ProbeError) as failure:
            await probe_socks_exit("127.0.0.1", 1, expected_countries={"JP"}, client=client)
        assert isinstance(failure.value, DetectorError)


@pytest.mark.asyncio
async def test_multiple_socks_relay_failures_confirm_route_failure() -> None:
    def relay_unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("SOCKS relay rejected the connection", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(relay_unavailable)) as client:
        with pytest.raises(ProbeError) as failure:
            await probe_socks_exit("127.0.0.1", 1, expected_countries={"JP"}, client=client)

    assert not isinstance(failure.value, DetectorError)


@pytest.mark.asyncio
async def test_bounded_body_and_failed_request_are_metered() -> None:
    recorded: list[int] = []

    async def record(size: int) -> None:
        recorded.append(size)

    token = usage_recorder.set(record)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 20_000))
        ) as client:
            with pytest.raises(ValueError, match="byte limit"):
                await bounded_get(client, "https://test", limit=100)
        assert len(recorded) == 1
    finally:
        usage_recorder.reset(token)


@pytest.mark.asyncio
async def test_counter_deltas_survive_app_restart_and_reject_reset_and_gaps(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'traffic.db'}"
    db = Database(url)
    await db.initialize(load_settings().regions)
    now = utc_now()
    row = {
        "scope": "host",
        "source": "ens5",
        "identity": "boot1",
        "rx_bytes": 1_000_000,
        "tx_bytes": 500_000,
    }
    await db.record_traffic_counters([row], observed_at=now)
    assert await db.traffic_summary(now) == []
    await db.close()
    db = Database(url)
    await db.record_traffic_counters(
        [dict(row, rx_bytes=1_000_100)], observed_at=now + timedelta(seconds=60)
    )
    # Same-identity reset and a new generation are both baselines, not usage.
    await db.record_traffic_counters(
        [dict(row, rx_bytes=20, tx_bytes=5)], observed_at=now + timedelta(seconds=120)
    )
    await db.record_traffic_counters(
        [dict(row, identity="boot2")], observed_at=now + timedelta(seconds=180)
    )
    await db.record_traffic_counters(
        [dict(row, identity="boot2", rx_bytes=1_100_000)], observed_at=now + timedelta(hours=1)
    )
    totals = await db.traffic_summary(now)
    assert totals[0]["rx_bytes"] == 100
    assert totals[0]["observed_seconds"] == 60
    await db.close()


@pytest.mark.asyncio
async def test_probe_has_an_overall_deadline() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(ProbeError):
            async with asyncio.timeout(0.5):
                await probe_socks_exit(
                    "127.0.0.1", 1, expected_countries={"JP"}, timeout_seconds=0.06, client=client
                )


class InspectWorker:
    def __init__(self, *, available: bool = True, socks_active: bool = True) -> None:
        self.available = available
        self.socks_active = socks_active
        self.calls = 0

    async def request(self, request: Request) -> dict[str, object]:
        self.calls += 1
        if not self.available:
            raise GateError("worker restarting")
        return {
            "slots": [
                {
                    "region_id": "jp",
                    "slot": "a",
                    "namespace_ip": "10.253.0.2",
                    "exists": True,
                    "socks_active": self.socks_active,
                }
            ]
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_up", "socks_up", "host_up", "category"),
    [
        (False, True, True, "detector"),
        (True, False, True, "detector"),
        (True, True, False, "detector"),
        (True, True, True, "node"),
    ],
)
async def test_timeout_requires_local_and_host_controls_before_node_penalty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_up: bool,
    socks_up: bool,
    host_up: bool,
    category: str,
) -> None:
    settings = load_settings()
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'controls.db'}")
    await database.initialize(settings.regions)
    monitor = MonitoringService(
        settings,
        database,
        InspectWorker(
            available=worker_up,
            socks_active=socks_up,
        ),
    )

    async def timeout(*args: object, **kwargs: object) -> EgressProbe:
        raise UnconfirmedRouteError("no destination responded")

    async def controls() -> bool:
        return host_up

    monkeypatch.setattr(monitoring_module, "probe_socks_exit", timeout)
    monkeypatch.setattr(monitor, "_host_controls_available", controls)
    with pytest.raises(ProbeError) as failure:
        await monitor.probe("10.253.0.2", 1080, expected_countries={"JP"})
    assert failure_category(failure.value) == category
    assert monitor._running_probes == 0
    await database.close()


@pytest.mark.asyncio
async def test_detector_response_does_not_start_control_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings()
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'detector.db'}")
    await database.initialize(settings.regions)
    worker = InspectWorker()
    monitor = MonitoringService(settings, database, worker)

    async def invalid_response(*args: object, **kwargs: object) -> EgressProbe:
        raise DetectorError("country providers disagree")

    monkeypatch.setattr(monitoring_module, "probe_socks_exit", invalid_response)
    with pytest.raises(DetectorError):
        await monitor.probe("10.253.0.2", 1080, expected_countries={"JP"})
    assert worker.calls == 0
    await database.close()


@pytest.mark.asyncio
async def test_candidate_connection_failure_is_unknown_when_host_control_is_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'connection.db'}")
    monitor = MonitoringService(load_settings(), database, InspectWorker())

    async def controls() -> bool:
        return False

    monkeypatch.setattr(monitor, "_host_controls_available", controls)
    failure = await monitor.validate_connection_failure(TunnelConnectError("connect timeout"))
    assert failure_category(failure) == "detector"
    await database.close()
