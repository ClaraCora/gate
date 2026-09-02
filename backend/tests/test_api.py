from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from gate.api import create_app
from gate.config import DatabaseConfig, SecurityConfig, load_settings
from gate.coordinator import (
    CandidateProbeResult,
    ProgressCallable,
    SwitchCoordinator,
    SwitchResult,
)
from gate.database import Database
from gate.discovery import DiscoveryService
from gate.domain import FeedParseResult, VpnGateNode
from gate.errors import GateError
from gate.worker_protocol import HealthRequest


class HealthyWorker:
    async def request(self, request: HealthRequest) -> dict[str, object]:
        assert request.action == "health"
        return {"status": "ok"}


class UnavailableWorker:
    async def request(self, request: HealthRequest) -> dict[str, object]:
        raise GateError("worker unavailable")


@dataclass
class BlockingCoordinator:
    started: asyncio.Event
    cancelled: asyncio.Event

    async def probe_candidate(
        self, region_id: str, node_id: int, *, progress: ProgressCallable | None = None
    ) -> CandidateProbeResult:
        del progress
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def switch(
        self, region_id: str, node_id: int, *, progress: ProgressCallable | None = None
    ) -> SwitchResult:
        del progress
        return SwitchResult(region_id, node_id, "b", "203.0.113.10", "JP", 30.0)


MUTATION_HEADERS = {"Origin": "http://test", "X-Gate-Request": "webui"}


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _settings(path: Path):  # type: ignore[no-untyped-def]
    return load_settings().model_copy(
        update={
            "database": DatabaseConfig(url=_database_url(path)),
            "security": SecurityConfig(enabled=False, allowed_origins=("http://test",)),
        }
    )


def _node(encoded_profile: str) -> VpnGateNode:
    return VpnGateNode(
        hostname="vpn-jp-test",
        ip="128.211.249.131",
        country_long="Japan",
        country_code="JP",
        score=3_000_000,
        ping_ms=12,
        speed_bps=150_000_000,
        sessions=8,
        uptime_ms=86_400_000,
        total_users=1_000,
        total_traffic_bytes=1_000_000,
        log_type="2weeks",
        operator="Test operator",
        message="",
        openvpn_config_base64=encoded_profile,
    )


@pytest.mark.asyncio
async def test_discovery_populates_region_candidates_and_events(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = _settings(tmp_path / "api.db")
    database = Database(settings.database.url)

    async def fetcher() -> FeedParseResult:
        return FeedParseResult(nodes=(_node(encoded_profile),), rejected_rows=0, warnings=())

    discovery = DiscoveryService(database, feed_url="unused", fetcher=fetcher)
    app = create_app(
        settings,
        database=database,
        discovery=discovery,
        worker_health=HealthyWorker(),
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ready = await client.get("/api/v1/health/ready")
            refresh = await client.post("/api/v1/discovery/refresh", headers=MUTATION_HEADERS)
            regions = await client.get("/api/v1/regions")
            initial_candidates = await client.get("/api/v1/regions/jp/candidates")
            node_id = initial_candidates.json()[0]["id"]
            await database.record_probe(
                region_id="jp",
                node_id=node_id,
                probe_type="candidate",
                result="succeeded",
                egress_ip="203.0.113.10",
                country_code="JP",
                latency_ms=42.0,
            )
            candidates = await client.get("/api/v1/regions/jp/candidates")
            events = await client.get("/api/v1/events")

    assert ready.status_code == 200
    assert refresh.json()["accepted"] == 1
    japan = next(region for region in regions.json() if region["id"] == "jp")
    assert japan["candidate_count"] == 1
    assert candidates.json()[0]["country_code"] == "JP"
    assert candidates.json()[0]["transport"] == "udp"
    assert candidates.json()[0]["measured_latency_ms"] == 42.0
    assert candidates.json()[0]["quality_score"] > 0
    assert events.json()[0]["code"] == "DISCOVERY_COMPLETED"


@pytest.mark.asyncio
async def test_readiness_requires_worker(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "not-ready.db")
    app = create_app(
        settings,
        worker_health=UnavailableWorker(),
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_candidates_only_include_latest_discovery_batch(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = _settings(tmp_path / "latest.db")
    database = Database(settings.database.url)
    second_profile = base64.b64encode(
        base64.b64decode(encoded_profile).decode().replace(" 1195", " 1196").encode()
    ).decode()
    profiles = iter((encoded_profile, second_profile))

    async def fetcher() -> FeedParseResult:
        return FeedParseResult(nodes=(_node(next(profiles)),), rejected_rows=0, warnings=())

    discovery = DiscoveryService(database, feed_url="unused", fetcher=fetcher)
    app = create_app(
        settings,
        database=database,
        discovery=discovery,
        worker_health=HealthyWorker(),
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/v1/discovery/refresh", headers=MUTATION_HEADERS)
            await client.post("/api/v1/discovery/refresh", headers=MUTATION_HEADERS)
            regions = await client.get("/api/v1/regions")
            candidates = await client.get("/api/v1/regions/jp/candidates")

    japan = next(region for region in regions.json() if region["id"] == "jp")
    assert japan["candidate_count"] == 1
    assert len(candidates.json()) == 1
    assert candidates.json()[0]["port"] == 1196


@pytest.mark.asyncio
async def test_unknown_region_returns_not_found(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "missing.db")
    app = create_app(
        settings,
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/regions/unknown/candidates")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_built_webui_is_served_without_shadowing_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "web.db")
    web_root = tmp_path / "dist"
    web_root.mkdir()
    (web_root / "index.html").write_text(
        "<html><body><!-- seed 9b31d87c --><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    app = create_app(
        settings,
        web_root=web_root,
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            index = await client.get("/")
            fallback = await client.get("/regions/jp")
            missing_api = await client.get("/api/v1/not-a-route")

    assert index.status_code == 200
    assert "9b31d87c" in index.text
    assert fallback.status_code == 200
    assert missing_api.status_code == 404


@pytest.mark.asyncio
async def test_running_candidate_probe_can_be_cancelled(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = _settings(tmp_path / "cancel.db")
    database = Database(settings.database.url)

    async def fetcher() -> FeedParseResult:
        return FeedParseResult(nodes=(_node(encoded_profile),), rejected_rows=0, warnings=())

    discovery = DiscoveryService(database, feed_url="unused", fetcher=fetcher)
    coordinator = BlockingCoordinator(asyncio.Event(), asyncio.Event())
    app = create_app(
        settings,
        database=database,
        discovery=discovery,
        coordinator=cast(SwitchCoordinator, coordinator),
        worker_health=HealthyWorker(),
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/v1/discovery/refresh", headers=MUTATION_HEADERS)
            candidate = (await client.get("/api/v1/regions/jp/candidates")).json()[0]
            created = await client.post(
                f"/api/v1/regions/jp/candidates/{candidate['id']}/probe",
                headers=MUTATION_HEADERS,
            )
            await asyncio.wait_for(coordinator.started.wait(), timeout=1)
            cancelled = await client.post(
                f"/api/v1/jobs/{created.json()['id']}/cancel",
                headers=MUTATION_HEADERS,
            )
            await asyncio.wait_for(coordinator.cancelled.wait(), timeout=1)
            await database.complete_switch("jp", "a", candidate["id"])
            reconnect = await client.post("/api/v1/regions/jp/reconnect", headers=MUTATION_HEADERS)
            reconnect_job = reconnect.json()
            for _ in range(20):
                status_response = await client.get(f"/api/v1/jobs/{reconnect_job['id']}")
                reconnect_job = status_response.json()
                if reconnect_job["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)

    assert created.status_code == 202
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["error_code"] == "CANCELLED_BY_USER"
    assert reconnect.status_code == 202
    assert reconnect_job["kind"] == "reconnect"
    assert reconnect_job["status"] == "succeeded"
