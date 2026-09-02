from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from gate import __version__
from gate.config import GateSettings, SocksAuthConfig, load_settings
from gate.controller import AutomationController
from gate.coordinator import SwitchCoordinator
from gate.database import Database, JobStatus, utc_now
from gate.discovery import DiscoveryService
from gate.domain import RegionMode
from gate.errors import GateError
from gate.probes import probe_socks_exit
from gate.schemas import (
    AutomationResponse,
    AutomationUpdateRequest,
    CandidateResponse,
    ChangePasswordRequest,
    DiscoveryResponse,
    EventResponse,
    HealthCheckResponse,
    HealthHistoryResponse,
    HealthResponse,
    JobResponse,
    LoginRequest,
    MetaResponse,
    RegionModeRequest,
    RegionResponse,
    SessionResponse,
    SlotRuntimeResponse,
    SocksAuthResponse,
    SocksAuthUpdateRequest,
)
from gate.scoring import calculate_quality
from gate.security import SESSION_COOKIE, SessionError, SessionManager
from gate.worker_client import WorkerClient
from gate.worker_protocol import HealthRequest, InspectRequest, UpdateSocksAuthRequest
from gate.worker_protocol import Request as WorkerRequest


class WorkerHealthGateway(Protocol):
    async def request(self, request: HealthRequest) -> dict[str, object]: ...


class WorkerGateway(Protocol):
    async def request(self, request: WorkerRequest) -> dict[str, object]: ...


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def create_app(
    settings: GateSettings | None = None,
    *,
    database: Database | None = None,
    discovery: DiscoveryService | None = None,
    coordinator: SwitchCoordinator | None = None,
    worker: WorkerGateway | None = None,
    worker_health: WorkerHealthGateway | None = None,
    password_hash: str | None = None,
    session_secret: str | None = None,
    web_root: Path | None = None,
    reconcile_on_startup: bool = True,
    automation_on_startup: bool = True,
) -> FastAPI:
    app_settings = settings or load_settings()
    app_database = database or Database(app_settings.database.url)
    app_discovery = discovery or DiscoveryService(
        app_database,
        feed_url=app_settings.discovery.url,
        fallback_urls=app_settings.discovery.fallback_urls,
    )
    app_worker: WorkerGateway = worker or WorkerClient()
    app_coordinator = coordinator or SwitchCoordinator(
        app_database,
        app_discovery,
        worker=app_worker,
        socks_auth=app_settings.socks_auth,
    )
    if isinstance(app_coordinator, SwitchCoordinator):
        app_coordinator.set_socks_auth(app_settings.socks_auth)
    app_worker_health = worker_health or WorkerClient(timeout_seconds=1.0)
    app_automation = AutomationController(
        app_settings,
        app_database,
        app_discovery,
        app_coordinator,
    )
    app_sessions = SessionManager(
        app_settings.security,
        password_hash=password_hash or os.environ.get("GATE_ADMIN_PASSWORD_HASH"),
        session_secret=session_secret or os.environ.get("GATE_SESSION_SECRET"),
    )
    background_tasks: set[asyncio.Task[None]] = set()
    job_tasks: dict[str, asyncio.Task[None]] = {}
    socks_auth_update_lock = asyncio.Lock()
    runtime_slots_lock = asyncio.Lock()
    runtime_slots_cache: tuple[float, list[SlotRuntimeResponse]] | None = None

    def schedule_job(job_id: str, operation: Coroutine[object, object, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(operation)
        background_tasks.add(task)
        job_tasks[job_id] = task

        def finished(completed: asyncio.Task[None]) -> None:
            background_tasks.discard(completed)
            job_tasks.pop(job_id, None)

        task.add_done_callback(finished)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await app_database.initialize(app_settings.regions)
        stored_credentials = await app_database.get_security_credentials()
        if stored_credentials is not None:
            app_sessions.replace_credentials(*stored_credentials)
        stored_automation_enabled = await app_database.get_automation_enabled()
        app_automation.set_enabled(
            app_settings.automation.enabled
            if stored_automation_enabled is None
            else stored_automation_enabled
        )
        await app_database.fail_interrupted_jobs()
        await app_database.cleanup_retention(**app_settings.retention.model_dump())
        if reconcile_on_startup:
            await app_coordinator.reconcile()
        if automation_on_startup:
            task = asyncio.create_task(app_automation.run_forever())
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
        yield
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        await app_database.close()

    app = FastAPI(
        title="Gate API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = app_settings
    app.state.database = app_database
    app.state.discovery = app_discovery
    app.state.coordinator = app_coordinator
    app.state.automation = app_automation
    app.state.worker = app_worker
    app.state.sessions = app_sessions

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=app_settings.security.session_hours * 3600,
            httponly=True,
            secure=app_settings.security.cookie_secure,
            samesite="strict",
            path="/",
        )

    public_read_paths = {
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/meta",
    }

    @app.middleware("http")
    async def protect_control_plane(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if is_mutation:
            if request.headers.get("x-gate-request") != "webui":
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Missing Gate request header"},
                )
            if not app_sessions.origin_allowed(request.headers.get("origin")):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Request origin is not allowed"},
                )

        is_public = (
            request.url.path in public_read_paths
            or (request.url.path == "/api/v1/session" and request.method == "GET")
            or (request.url.path == "/api/v1/session/login" and request.method == "POST")
        )
        if is_public:
            return await call_next(request)
        if not app_settings.security.enabled:
            return await call_next(request)
        if not app_sessions.configured:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Authentication is not configured"},
            )
        try:
            session = app_sessions.decode(request.cookies.get(SESSION_COOKIE))
        except SessionError:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required"},
            )
        request.state.session = session
        supplied_csrf = request.headers.get("x-gate-csrf")
        if is_mutation and (
            supplied_csrf is None or not secrets.compare_digest(supplied_csrf, session.csrf_token)
        ):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Invalid CSRF token"},
            )
        return await call_next(request)

    async def run_switch_job(job_id: str, region_id: str, node_id: int) -> None:
        async def progress(value: float, message: str) -> None:
            await app_database.update_job(
                job_id,
                status=JobStatus.RUNNING,
                progress=value,
                detail={"message": message, "node_id": node_id},
            )

        await app_database.update_job(
            job_id,
            status=JobStatus.RUNNING,
            progress=0.0,
            detail={"message": "线路切换任务已开始", "node_id": node_id},
        )
        try:
            result = await app_coordinator.switch(
                region_id,
                node_id,
                progress=progress,
            )
        except asyncio.CancelledError:
            raise
        except GateError as exc:
            await app_database.update_job(
                job_id,
                status=JobStatus.FAILED,
                progress=1.0,
                error_code=exc.code,
                detail={"message": str(exc), "node_id": node_id},
            )
        except Exception:
            await app_database.update_job(
                job_id,
                status=JobStatus.FAILED,
                progress=1.0,
                error_code="UNEXPECTED_SWITCH_ERROR",
                detail={"message": "线路切换发生意外错误", "node_id": node_id},
            )
        else:
            await app_database.update_job(
                job_id,
                status=JobStatus.SUCCEEDED,
                progress=1.0,
                detail={
                    "message": "线路切换完成",
                    "node_id": node_id,
                    "slot": result.slot,
                    "egress_ip": result.egress_ip,
                    "country_code": result.country_code,
                    "latency_ms": result.latency_ms,
                },
            )

    async def run_probe_job(job_id: str, region_id: str) -> None:
        region = await app_database.get_region(region_id)
        if region is None:
            return
        await app_database.update_job(
            job_id,
            status=JobStatus.RUNNING,
            progress=0.1,
            detail={"message": "正在测试固定 SOCKS 端口"},
        )
        try:
            result = await probe_socks_exit(
                "127.0.0.1",
                region.socks_port,
                expected_countries=set(region.countries),
                username=(
                    app_settings.socks_auth.username if app_settings.socks_auth.enabled else None
                ),
                password=(
                    app_settings.socks_auth.password if app_settings.socks_auth.enabled else None
                ),
            )
        except GateError as exc:
            await app_database.update_job(
                job_id,
                status=JobStatus.FAILED,
                progress=1.0,
                error_code=exc.code,
                detail={"message": str(exc)},
            )
        else:
            await app_database.update_job(
                job_id,
                status=JobStatus.SUCCEEDED,
                progress=1.0,
                detail={
                    "message": "出口测试完成",
                    "egress_ip": result.egress_ip,
                    "country_code": result.country_code,
                    "latency_ms": result.latency_ms,
                },
            )

    async def run_candidate_probe_job(job_id: str, region_id: str, node_id: int) -> None:
        async def progress(value: float, message: str) -> None:
            await app_database.update_job(
                job_id,
                status=JobStatus.RUNNING,
                progress=value,
                detail={"message": message, "node_id": node_id},
            )

        await app_database.update_job(
            job_id,
            status=JobStatus.RUNNING,
            progress=0.0,
            detail={"message": "候选节点测试已开始", "node_id": node_id},
        )
        try:
            result = await app_coordinator.probe_candidate(
                region_id,
                node_id,
                progress=progress,
            )
        except asyncio.CancelledError:
            raise
        except GateError as exc:
            await app_database.update_job(
                job_id,
                status=JobStatus.FAILED,
                progress=1.0,
                error_code=exc.code,
                detail={"message": str(exc), "node_id": node_id},
            )
        except Exception:
            await app_database.update_job(
                job_id,
                status=JobStatus.FAILED,
                progress=1.0,
                error_code="UNEXPECTED_PROBE_ERROR",
                detail={"message": "候选节点测试发生意外错误", "node_id": node_id},
            )
        else:
            await app_database.update_job(
                job_id,
                status=JobStatus.SUCCEEDED,
                progress=1.0,
                detail={
                    "message": "候选节点测试完成",
                    "node_id": node_id,
                    "slot": result.slot,
                    "egress_ip": result.egress_ip,
                    "country_code": result.country_code,
                    "latency_ms": result.latency_ms,
                },
            )

    @app.get("/api/v1/meta", response_model=MetaResponse)
    async def meta() -> MetaResponse:
        return MetaResponse(version=__version__)

    @app.get("/api/v1/session", response_model=SessionResponse)
    async def get_session(request: Request) -> SessionResponse:
        if not app_settings.security.enabled:
            return SessionResponse(authenticated=True, security_enabled=False)
        try:
            session = app_sessions.decode(request.cookies.get(SESSION_COOKIE))
        except SessionError:
            return SessionResponse(authenticated=False)
        return SessionResponse(
            authenticated=True,
            security_enabled=True,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )

    @app.post("/api/v1/session/login", response_model=SessionResponse)
    async def login(payload: LoginRequest, response: Response) -> SessionResponse:
        if not app_settings.security.enabled:
            return SessionResponse(authenticated=True)
        if not app_sessions.configured:
            raise HTTPException(status_code=503, detail="Authentication is not configured")
        if not app_sessions.verify_password(payload.password):
            await asyncio.sleep(0.25)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token, session = app_sessions.issue()
        set_session_cookie(response, token)
        return SessionResponse(
            authenticated=True,
            security_enabled=True,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )

    @app.put("/api/v1/session/password", response_model=SessionResponse)
    async def change_password(
        payload: ChangePasswordRequest,
        response: Response,
    ) -> SessionResponse:
        if not app_settings.security.enabled:
            raise HTTPException(status_code=409, detail="管理员认证未启用")
        if not app_sessions.verify_password(payload.current_password):
            await asyncio.sleep(0.25)
            raise HTTPException(status_code=400, detail="当前密码不正确")
        if len(payload.new_password) < 8:
            raise HTTPException(status_code=422, detail="新密码至少需要 8 个字符")
        if secrets.compare_digest(payload.current_password, payload.new_password):
            raise HTTPException(status_code=422, detail="新密码不能与当前密码相同")

        password_hash = await asyncio.to_thread(
            app_sessions.hash_password,
            payload.new_password,
        )
        session_secret = secrets.token_urlsafe(48)
        await app_database.set_security_credentials(password_hash, session_secret)
        app_sessions.replace_credentials(password_hash, session_secret)
        token, session = app_sessions.issue()
        set_session_cookie(response, token)
        return SessionResponse(
            authenticated=True,
            security_enabled=True,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )

    @app.delete("/api/v1/session", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    @app.get("/api/v1/socks-auth", response_model=SocksAuthResponse)
    async def get_socks_auth() -> SocksAuthResponse:
        auth = app_settings.socks_auth
        return SocksAuthResponse(
            enabled=auth.enabled,
            username=auth.username,
            password_set=bool(auth.password),
            listen=auth.listen,
        )

    @app.get("/api/v1/automation", response_model=AutomationResponse)
    async def get_automation() -> AutomationResponse:
        return AutomationResponse(enabled=app_automation.enabled)

    @app.put("/api/v1/automation", response_model=AutomationResponse)
    async def update_automation(payload: AutomationUpdateRequest) -> AutomationResponse:
        await app_database.set_automation_enabled(payload.enabled)
        app_automation.set_enabled(payload.enabled)
        return AutomationResponse(enabled=app_automation.enabled)

    @app.put("/api/v1/socks-auth", response_model=SocksAuthResponse)
    async def update_socks_auth(payload: SocksAuthUpdateRequest) -> SocksAuthResponse:
        async with socks_auth_update_lock:
            current = app_settings.socks_auth
            listen = payload.listen or current.listen
            if payload.enabled:
                password = payload.password or current.password
                if not password:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="首次启用 SOCKS 认证时必须设置密码",
                    )
                try:
                    updated = SocksAuthConfig(
                        enabled=True,
                        username=payload.username,
                        password=password,
                        listen=listen,
                    )
                except ValidationError as exc:
                    message = str(exc)
                    if "username" in message:
                        detail = "用户名须为 3-32 位字母、数字、点、下划线或连字符"
                    elif "visible ASCII" in message:
                        detail = "密码只能包含可见 ASCII 字符, 不能包含空格或中文"
                    else:
                        detail = "密码须为 8-128 个字符"
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=detail,
                    ) from exc
            else:
                if listen == "0.0.0.0":
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="公网监听必须启用 SOCKS 用户名和密码认证",
                    )
                updated = SocksAuthConfig(listen=listen)

            try:
                await app_worker.request(
                    UpdateSocksAuthRequest(
                        action="update_socks_auth",
                        enabled=updated.enabled,
                        username=updated.username,
                        password=updated.password,
                        listen=updated.listen,
                    )
                )
            except GateError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": exc.code,
                        "message": "SOCKS 接入设置更新失败, 原配置已保留",
                    },
                ) from exc

            app_settings.socks_auth = updated
            if isinstance(app_coordinator, SwitchCoordinator):
                app_coordinator.set_socks_auth(updated)
            auth_state = "已启用认证" if updated.enabled else "未启用认证"
            listen_label = "全部网卡" if updated.listen == "0.0.0.0" else "仅本机"
            await app_database.add_event(
                code="SOCKS_AUTH_UPDATED",
                message=f"SOCKS 接入设置已更新: 监听{listen_label}, {auth_state}",
                details={
                    "enabled": updated.enabled,
                    "username": updated.username,
                    "listen": updated.listen,
                },
            )
            return SocksAuthResponse(
                enabled=updated.enabled,
                username=updated.username,
                password_set=bool(updated.password),
                listen=updated.listen,
            )

    @app.get("/api/v1/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/api/v1/health/ready", response_model=HealthResponse)
    async def ready(response: Response) -> HealthResponse:
        if app_settings.security.enabled and not app_sessions.configured:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="not_ready")
        if not await app_database.is_ready():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="not_ready")
        try:
            worker_status = await app_worker_health.request(HealthRequest(action="health"))
        except (GateError, ValueError):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="not_ready")
        if worker_status.get("status") != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="not_ready")
        return HealthResponse(status="ok")

    @app.get("/api/v1/regions", response_model=list[RegionResponse])
    async def list_regions() -> list[RegionResponse]:
        records = await app_database.list_regions()
        return [
            RegionResponse(
                id=region.id,
                group_id=region.group_id,
                name=region.name,
                countries=region.countries,
                socks_port=region.socks_port,
                network_index=region.network_index,
                enabled=region.enabled,
                mode=region.mode,
                status=region.status,
                active_node_id=region.active_node_id,
                active_egress_ip=region.active_egress_ip,
                candidate_count=candidate_count,
                updated_at=region.updated_at,
            )
            for region, candidate_count in records
        ]

    @app.get("/api/v1/health-history", response_model=HealthHistoryResponse)
    async def health_history(
        hours: int = Query(default=2, ge=1, le=24),
    ) -> HealthHistoryResponse:
        generated_at = utc_now()
        records = await app_database.list_active_health_probes(
            generated_at - timedelta(hours=hours),
            generated_at,
        )
        return HealthHistoryResponse(
            window_hours=hours,
            generated_at=generated_at,
            checks=[
                HealthCheckResponse(
                    id=record.id,
                    region_id=record.region_id,
                    result=cast(Literal["succeeded", "failed"], record.result),
                    egress_ip=record.egress_ip,
                    latency_median_ms=record.latency_median_ms,
                    error_code=record.error_code,
                    started_at=_as_utc(record.started_at),
                    finished_at=_as_utc(record.finished_at),
                )
                for record in records
                if record.finished_at is not None and record.result in {"succeeded", "failed"}
            ],
        )

    @app.get(
        "/api/v1/regions/{region_id}/candidates",
        response_model=list[CandidateResponse],
    )
    async def list_candidates(
        region_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[CandidateResponse]:
        region = await app_database.get_region(region_id)
        if region is None:
            raise HTTPException(status_code=404, detail="Region not found")
        records = await app_database.list_candidates(region_id, limit)
        if region.active_node_id is not None:
            active_index = next(
                (index for index, node in enumerate(records) if node.id == region.active_node_id),
                None,
            )
            if active_index is not None:
                records.insert(0, records.pop(active_index))
            else:
                active_node = await app_database.get_node(region.active_node_id)
                if active_node is not None:
                    records.insert(0, active_node)
            records = records[:limit]
        result: list[CandidateResponse] = []
        for node in records:
            metrics = await app_database.get_probe_metrics(region_id, node.id)
            quality = calculate_quality(metrics) if metrics is not None else None
            result.append(
                CandidateResponse(
                    id=node.id,
                    hostname=node.hostname,
                    ip=node.ip,
                    country_code=node.country_code,
                    country_long=node.country_long,
                    transport=node.transport,
                    port=node.port,
                    api_score=node.api_score,
                    api_ping_ms=node.api_ping_ms,
                    api_speed_bps=node.api_speed_bps,
                    sessions=node.sessions,
                    uptime_ms=node.uptime_ms,
                    log_type=node.log_type,
                    operator=node.operator,
                    last_seen_at=node.last_seen_at,
                    availability_24h=metrics.availability_24h if metrics is not None else None,
                    measured_latency_ms=(
                        metrics.latency_ms
                        if metrics is not None and metrics.availability_24h > 0
                        else None
                    ),
                    measured_throughput_mbps=(
                        metrics.throughput_mbps
                        if metrics is not None and metrics.throughput_mbps > 0
                        else None
                    ),
                    quality_score=quality.total if quality is not None else None,
                )
            )
        return result

    @app.get("/api/v1/runtime/slots", response_model=list[SlotRuntimeResponse])
    async def runtime_slots() -> list[SlotRuntimeResponse]:
        nonlocal runtime_slots_cache
        now = asyncio.get_running_loop().time()
        if runtime_slots_cache is not None and now - runtime_slots_cache[0] < 2.0:
            return runtime_slots_cache[1]
        async with runtime_slots_lock:
            now = asyncio.get_running_loop().time()
            if runtime_slots_cache is not None and now - runtime_slots_cache[0] < 2.0:
                return runtime_slots_cache[1]
            try:
                data = await app_worker.request(InspectRequest(action="inspect"))
            except GateError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": exc.code, "message": "gate-worker is unavailable"},
                ) from exc
            slots = data.get("slots")
            if not isinstance(slots, list):
                raise HTTPException(status_code=502, detail="Worker returned invalid runtime state")
            parsed = [SlotRuntimeResponse.model_validate(item) for item in slots]
            runtime_slots_cache = (asyncio.get_running_loop().time(), parsed)
            return parsed

    @app.post(
        "/api/v1/discovery/refresh",
        response_model=DiscoveryResponse,
        status_code=status.HTTP_200_OK,
    )
    async def refresh_discovery() -> DiscoveryResponse:
        try:
            summary = await app_discovery.refresh()
        except GateError as exc:
            await app_database.add_event(
                code="DISCOVERY_FAILED",
                level="error",
                message="手动刷新 VPN Gate 节点失败",
                details={"error_code": exc.code},
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return DiscoveryResponse(
            discovered=summary.discovered,
            accepted=summary.accepted,
            rejected_feed_rows=summary.rejected_feed_rows,
            rejected_profiles=summary.rejected_profiles,
            warnings=list(summary.warnings),
            observed_at=summary.observed_at,
            source_url=summary.source_url,
        )

    @app.get("/api/v1/events", response_model=list[EventResponse])
    async def list_events(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[EventResponse]:
        events = await app_database.list_events(limit)
        return [EventResponse.model_validate(event) for event in events]

    @app.post(
        "/api/v1/regions/{region_id}/candidates/{node_id}/switch",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def switch_candidate(region_id: str, node_id: int) -> JobResponse:
        region = await app_database.get_region(region_id)
        node = await app_database.get_node(node_id)
        if region is None:
            raise HTTPException(status_code=404, detail="Region not found")
        if not region.enabled or region.mode == "disabled":
            raise HTTPException(status_code=409, detail="Region is disabled")
        if node is None or node.country_code not in region.countries:
            raise HTTPException(status_code=404, detail="Candidate not found in region")
        if node.fingerprint not in app_discovery.profiles:
            raise HTTPException(
                status_code=409,
                detail="Candidate profile is not cached; refresh discovery and retry",
            )
        job = await app_database.create_job(kind="switch", region_id=region_id)
        schedule_job(job.id, run_switch_job(job.id, region_id, node_id))
        return JobResponse.model_validate(job)

    @app.post(
        "/api/v1/regions/{region_id}/candidates/{node_id}/probe",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def probe_candidate(region_id: str, node_id: int) -> JobResponse:
        region = await app_database.get_region(region_id)
        node = await app_database.get_node(node_id)
        if region is None:
            raise HTTPException(status_code=404, detail="Region not found")
        if not region.enabled or region.mode == "disabled":
            raise HTTPException(status_code=409, detail="Region is disabled")
        if node is None or node.country_code not in region.countries:
            raise HTTPException(status_code=404, detail="Candidate not found in region")
        if node.fingerprint not in app_discovery.profiles:
            raise HTTPException(
                status_code=409,
                detail="Candidate profile is not cached; refresh discovery and retry",
            )
        job = await app_database.create_job(kind="candidate_probe", region_id=region_id)
        schedule_job(job.id, run_candidate_probe_job(job.id, region_id, node_id))
        return JobResponse.model_validate(job)

    @app.post(
        "/api/v1/regions/{region_id}/probe",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def probe_region(region_id: str) -> JobResponse:
        region = await app_database.get_region(region_id)
        if region is None:
            raise HTTPException(status_code=404, detail="Region not found")
        job = await app_database.create_job(kind="probe", region_id=region_id)
        schedule_job(job.id, run_probe_job(job.id, region_id))
        return JobResponse.model_validate(job)

    @app.post(
        "/api/v1/regions/{region_id}/reconnect",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def reconnect_region(region_id: str) -> JobResponse:
        region = await app_database.get_region(region_id)
        active = await app_database.get_active_slot(region_id)
        if region is None:
            raise HTTPException(status_code=404, detail="Region not found")
        if not region.enabled or region.mode == "disabled":
            raise HTTPException(status_code=409, detail="Region is disabled")
        if active is None or active.node_id is None:
            raise HTTPException(status_code=409, detail="Region has no active exit to reconnect")
        job = await app_database.create_job(kind="reconnect", region_id=region_id)
        schedule_job(job.id, run_switch_job(job.id, region_id, active.node_id))
        return JobResponse.model_validate(job)

    @app.put("/api/v1/regions/{region_id}/mode", response_model=RegionResponse)
    async def set_region_mode(region_id: str, payload: RegionModeRequest) -> RegionResponse:
        region = await app_database.get_region(region_id)
        if region is None:
            raise HTTPException(status_code=404, detail="Region not found")
        await app_coordinator.set_region_mode(region_id, RegionMode(payload.mode))
        records = await app_database.list_regions()
        updated, candidate_count = next(item for item in records if item[0].id == region_id)
        return RegionResponse(
            id=updated.id,
            group_id=updated.group_id,
            name=updated.name,
            countries=updated.countries,
            socks_port=updated.socks_port,
            network_index=updated.network_index,
            enabled=updated.enabled,
            mode=updated.mode,
            status=updated.status,
            active_node_id=updated.active_node_id,
            active_egress_ip=updated.active_egress_ip,
            candidate_count=candidate_count,
            updated_at=updated.updated_at,
        )

    @app.get("/api/v1/jobs", response_model=list[JobResponse])
    async def list_jobs(limit: int = Query(default=50, ge=1, le=500)) -> list[JobResponse]:
        jobs = await app_database.list_jobs(limit)
        return [JobResponse.model_validate(job) for job in jobs]

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: str) -> JobResponse:
        job = await app_database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobResponse.model_validate(job)

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(job_id: str) -> JobResponse:
        job = await app_database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            raise HTTPException(status_code=409, detail="Job can no longer be cancelled")
        task = job_tasks.get(job_id)
        if task is None:
            raise HTTPException(status_code=409, detail="Job is not running in this process")
        cancelled = await app_database.cancel_job(job_id)
        task.cancel()
        if cancelled is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobResponse.model_validate(cancelled)

    @app.get("/api/v1/events/stream")
    async def stream_events(request: Request) -> StreamingResponse:
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor is None:
            initial_cursor = await app_database.latest_event_id()
        else:
            try:
                initial_cursor = max(0, int(raw_cursor))
            except ValueError:
                initial_cursor = await app_database.latest_event_id()

        async def generate() -> AsyncIterator[str]:
            cursor = initial_cursor
            keepalive = 0
            while not await request.is_disconnected():
                events = await app_database.list_events_after(cursor)
                for event in events:
                    cursor = event.id
                    payload = EventResponse.model_validate(event).model_dump_json()
                    yield f"id: {event.id}\nevent: gate-event\ndata: {payload}\n\n"
                keepalive += 1
                if keepalive >= 15:
                    yield ": keepalive\n\n"
                    keepalive = 0
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    configured_web_root = web_root
    if configured_web_root is None:
        environment_root = os.environ.get("GATE_WEB_ROOT")
        configured_web_root = (
            Path(environment_root)
            if environment_root
            else Path(__file__).resolve().parents[3] / "frontend" / "dist"
        )
    index_file = configured_web_root / "index.html"
    assets_root = configured_web_root / "assets"
    if index_file.is_file():
        if assets_root.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_root), name="web-assets")

        @app.get("/", include_in_schema=False)
        async def web_index() -> FileResponse:
            return FileResponse(index_file)

        @app.get("/{route:path}", include_in_schema=False)
        async def web_fallback(route: str) -> FileResponse:
            if route == "api" or route.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            return FileResponse(index_file)

    return app


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api.listen,
        port=settings.api.port,
        timeout_graceful_shutdown=5,
    )


def create_dev_app() -> FastAPI:
    return create_app(
        load_settings(),
        reconcile_on_startup=False,
        automation_on_startup=False,
    )
