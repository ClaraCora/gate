from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    delete,
    event,
    func,
    inspect,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from gate.config import RegionConfig
from gate.domain import ProbeMetrics, RegionMode, RegionStatus, SanitizedProfile, VpnGateNode


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Base(DeclarativeBase):
    pass


class RegionRecord(Base):
    __tablename__ = "regions"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(80))
    countries: Mapped[list[str]] = mapped_column(JSON)
    socks_port: Mapped[int] = mapped_column(Integer, unique=True)
    network_index: Mapped[int] = mapped_column(Integer, unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    mode: Mapped[str] = mapped_column(String(16), default=RegionMode.AUTO)
    status: Mapped[str] = mapped_column(String(24), default=RegionStatus.UNAVAILABLE)
    active_node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    active_egress_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NodeRecord(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str] = mapped_column(String(45), index=True)
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    country_long: Mapped[str] = mapped_column(String(80))
    transport: Mapped[str] = mapped_column(String(8))
    port: Mapped[int] = mapped_column(Integer)
    api_score: Mapped[int] = mapped_column(Integer)
    api_ping_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_speed_bps: Mapped[int] = mapped_column(Integer)
    sessions: Mapped[int] = mapped_column(Integer)
    uptime_ms: Mapped[int] = mapped_column(Integer)
    log_type: Mapped[str] = mapped_column(String(80), default="")
    operator: Mapped[str] = mapped_column(String(255), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    blacklisted_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    blacklist_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class NodeObservationRecord(Base):
    __tablename__ = "node_observations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    api_score: Mapped[int] = mapped_column(Integer)
    api_ping_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_speed_bps: Mapped[int] = mapped_column(Integer)
    sessions: Mapped[int] = mapped_column(Integer)
    uptime_ms: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class ProbeRunRecord(Base):
    __tablename__ = "probe_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    region_id: Mapped[str] = mapped_column(ForeignKey("regions.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), index=True)
    probe_type: Mapped[str] = mapped_column(String(24))
    result: Mapped[str] = mapped_column(String(24))
    egress_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    latency_median_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RegionSlotRecord(Base):
    __tablename__ = "region_slots"

    region_id: Mapped[str] = mapped_column(ForeignKey("regions.id"), primary_key=True)
    slot: Mapped[str] = mapped_column(String(1), primary_key=True)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    namespace_name: Mapped[str] = mapped_column(String(32))
    backend_address: Mapped[str] = mapped_column(String(45))
    state: Mapped[str] = mapped_column(String(24), default="empty")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED, index=True)
    region_id: Mapped[str | None] = mapped_column(ForeignKey("regions.id"), nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EventRecord(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    region_id: Mapped[str | None] = mapped_column(ForeignKey("regions.id"), nullable=True)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class RegionSelectionRecord(Base):
    __tablename__ = "region_selection_state"

    region_id: Mapped[str] = mapped_column(ForeignKey("regions.id"), primary_key=True)
    candidate_node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    confirmation_rounds: Mapped[int] = mapped_column(Integer, default=0)
    last_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)

    @staticmethod
    def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @staticmethod
    def _migrate_schema(connection: Any) -> None:
        inspector = inspect(connection)
        if "regions" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("regions")}
        additions = {
            "group_id": "ALTER TABLE regions ADD COLUMN group_id VARCHAR(16)",
            "network_index": "ALTER TABLE regions ADD COLUMN network_index INTEGER",
            "active_egress_ip": "ALTER TABLE regions ADD COLUMN active_egress_ip VARCHAR(45)",
        }
        for name, statement in additions.items():
            if name not in columns:
                connection.exec_driver_sql(statement)

    async def initialize(self, regions: Sequence[RegionConfig]) -> None:
        from gate.network import slot_spec

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(self._migrate_schema)
        async with self.sessions() as session, session.begin():
            for region in regions:
                group_id = region.group_id or region.id
                record = await session.get(RegionRecord, region.id)
                if record is None:
                    session.add(
                        RegionRecord(
                            id=region.id,
                            group_id=group_id,
                            name=region.name,
                            countries=list(region.countries),
                            socks_port=region.socks_port,
                            network_index=region.network_index,
                            enabled=region.enabled,
                            mode=(RegionMode.AUTO if region.enabled else RegionMode.DISABLED),
                            status=(
                                RegionStatus.UNAVAILABLE
                                if region.enabled
                                else RegionStatus.DISABLED
                            ),
                        )
                    )
                else:
                    record.group_id = group_id
                    record.name = region.name
                    record.countries = list(region.countries)
                    record.socks_port = region.socks_port
                    record.network_index = region.network_index
                    record.updated_at = utc_now()
                if await session.get(RegionSelectionRecord, region.id) is None:
                    session.add(RegionSelectionRecord(region_id=region.id))
                for slot_name in ("a", "b"):
                    spec = slot_spec(region, slot_name)
                    slot_record = await session.get(RegionSlotRecord, (region.id, slot_name))
                    if slot_record is None:
                        session.add(
                            RegionSlotRecord(
                                region_id=region.id,
                                slot=slot_name,
                                namespace_name=spec.namespace,
                                backend_address=spec.namespace_ip,
                            )
                        )
                    else:
                        slot_record.namespace_name = spec.namespace
                        slot_record.backend_address = spec.namespace_ip

    async def close(self) -> None:
        await self.engine.dispose()

    async def is_ready(self) -> bool:
        try:
            async with self.sessions() as session:
                await session.scalar(select(1))
        except Exception:
            return False
        return True

    async def ingest_nodes(
        self, items: Iterable[tuple[VpnGateNode, SanitizedProfile]], observed_at: datetime
    ) -> int:
        count = 0
        async with self.sessions() as session, session.begin():
            for node, profile in items:
                record = await session.scalar(
                    select(NodeRecord).where(NodeRecord.fingerprint == profile.fingerprint)
                )
                if record is None:
                    record = NodeRecord(
                        fingerprint=profile.fingerprint,
                        hostname=node.hostname,
                        ip=node.ip,
                        country_code=node.country_code,
                        country_long=node.country_long,
                        transport=profile.transport,
                        port=profile.remote_port,
                        api_score=node.score,
                        api_ping_ms=node.ping_ms,
                        api_speed_bps=node.speed_bps,
                        sessions=node.sessions,
                        uptime_ms=node.uptime_ms,
                        log_type=node.log_type,
                        operator=node.operator,
                        message=node.message,
                        first_seen_at=observed_at,
                        last_seen_at=observed_at,
                    )
                    session.add(record)
                    await session.flush()
                else:
                    record.hostname = node.hostname
                    record.api_score = node.score
                    record.api_ping_ms = node.ping_ms
                    record.api_speed_bps = node.speed_bps
                    record.sessions = node.sessions
                    record.uptime_ms = node.uptime_ms
                    record.log_type = node.log_type
                    record.operator = node.operator
                    record.message = node.message
                    record.last_seen_at = observed_at

                session.add(
                    NodeObservationRecord(
                        node_id=record.id,
                        api_score=node.score,
                        api_ping_ms=node.ping_ms,
                        api_speed_bps=node.speed_bps,
                        sessions=node.sessions,
                        uptime_ms=node.uptime_ms,
                        observed_at=observed_at,
                    )
                )
                count += 1
        return count

    async def list_regions(self) -> list[tuple[RegionRecord, int]]:
        async with self.sessions() as session:
            latest_observation = await session.scalar(
                select(func.max(NodeObservationRecord.observed_at))
            )
            regions = list(
                await session.scalars(select(RegionRecord).order_by(RegionRecord.socks_port))
            )
            result: list[tuple[RegionRecord, int]] = []
            for region in regions:
                candidate_count = 0
                if latest_observation is not None:
                    sibling_nodes = select(RegionRecord.active_node_id).where(
                        RegionRecord.group_id == region.group_id,
                        RegionRecord.id != region.id,
                        RegionRecord.active_node_id.is_not(None),
                    )
                    candidate_count = int(
                        await session.scalar(
                            select(func.count(NodeRecord.id)).where(
                                NodeRecord.country_code.in_(region.countries),
                                NodeRecord.last_seen_at == latest_observation,
                                NodeRecord.id.not_in(sibling_nodes),
                            )
                        )
                        or 0
                    )
                result.append((region, candidate_count))
            return result

    async def get_region(self, region_id: str) -> RegionRecord | None:
        async with self.sessions() as session:
            return await session.get(RegionRecord, region_id)

    async def get_node(self, node_id: int) -> NodeRecord | None:
        async with self.sessions() as session:
            return await session.get(NodeRecord, node_id)

    async def get_active_conflict(
        self,
        region_id: str,
        *,
        node_id: int,
        egress_ip: str | None = None,
    ) -> RegionRecord | None:
        async with self.sessions() as session:
            region = await session.get(RegionRecord, region_id)
            if region is None:
                return None
            conflicts = [RegionRecord.active_node_id == node_id]
            if egress_ip is not None:
                conflicts.append(RegionRecord.active_egress_ip == egress_ip)
            record: RegionRecord | None = await session.scalar(
                select(RegionRecord).where(
                    RegionRecord.group_id == region.group_id,
                    RegionRecord.id != region_id,
                    RegionRecord.enabled.is_(True),
                    or_(*conflicts),
                )
            )
            return record

    async def get_active_slot(self, region_id: str) -> RegionSlotRecord | None:
        async with self.sessions() as session:
            record: RegionSlotRecord | None = await session.scalar(
                select(RegionSlotRecord).where(
                    RegionSlotRecord.region_id == region_id,
                    RegionSlotRecord.state == "active",
                )
            )
            return record

    async def get_slot(self, region_id: str, slot: str) -> RegionSlotRecord | None:
        async with self.sessions() as session:
            return await session.get(RegionSlotRecord, (region_id, slot))

    async def list_slots(self) -> list[RegionSlotRecord]:
        async with self.sessions() as session:
            statement = select(RegionSlotRecord).order_by(
                RegionSlotRecord.region_id, RegionSlotRecord.slot
            )
            return list(await session.scalars(statement))

    async def set_region_status(self, region_id: str, status: RegionStatus) -> None:
        async with self.sessions() as session, session.begin():
            region = await session.get(RegionRecord, region_id)
            if region is None:
                raise ValueError(f"unknown region: {region_id}")
            region.status = status
            region.updated_at = utc_now()

    async def set_region_mode(self, region_id: str, mode: RegionMode) -> RegionRecord:
        async with self.sessions() as session, session.begin():
            region = await session.get(RegionRecord, region_id)
            if region is None:
                raise ValueError(f"unknown region: {region_id}")
            region.mode = mode
            region.enabled = mode != RegionMode.DISABLED
            if mode == RegionMode.DISABLED:
                region.status = RegionStatus.DISABLED
            elif region.status == RegionStatus.DISABLED:
                active = await session.scalar(
                    select(RegionSlotRecord).where(
                        RegionSlotRecord.region_id == region_id,
                        RegionSlotRecord.state == "active",
                    )
                )
                region.status = (
                    RegionStatus.HEALTHY if active is not None else RegionStatus.UNAVAILABLE
                )
            region.updated_at = utc_now()
            return region

    async def complete_switch(
        self,
        region_id: str,
        slot: str,
        node_id: int,
        egress_ip: str | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            region = await session.get(RegionRecord, region_id)
            target = await session.get(RegionSlotRecord, (region_id, slot))
            if region is None or target is None:
                raise ValueError(f"unknown region slot: {region_id}/{slot}")
            conflict_conditions = [RegionRecord.active_node_id == node_id]
            if egress_ip is not None:
                conflict_conditions.append(RegionRecord.active_egress_ip == egress_ip)
            conflict = await session.scalar(
                select(RegionRecord.id).where(
                    RegionRecord.group_id == region.group_id,
                    RegionRecord.id != region_id,
                    RegionRecord.enabled.is_(True),
                    or_(*conflict_conditions),
                )
            )
            if conflict is not None:
                raise ValueError(f"active exit conflicts with sibling entry: {conflict}")
            slots = await session.scalars(
                select(RegionSlotRecord).where(RegionSlotRecord.region_id == region_id)
            )
            for record in slots:
                if record.slot != slot and record.state == "active":
                    record.state = "draining"
            target.node_id = node_id
            target.state = "active"
            target.started_at = utc_now()
            target.last_verified_at = utc_now()
            region.active_node_id = node_id
            region.active_egress_ip = egress_ip
            region.status = RegionStatus.HEALTHY
            region.updated_at = utc_now()
            selection = await session.get(RegionSelectionRecord, region_id)
            if selection is None:
                selection = RegionSelectionRecord(region_id=region_id)
                session.add(selection)
            selection.candidate_node_id = None
            selection.confirmation_rounds = 0
            selection.last_switch_at = utc_now()
            selection.updated_at = utc_now()

    async def mark_slot_empty(self, region_id: str, slot: str) -> None:
        async with self.sessions() as session, session.begin():
            record = await session.get(RegionSlotRecord, (region_id, slot))
            if record is not None:
                was_active = record.state == "active"
                record.node_id = None
                record.state = "empty"
                record.started_at = None
                record.last_verified_at = None
                if was_active:
                    region = await session.get(RegionRecord, region_id)
                    if region is not None:
                        region.active_node_id = None
                        region.active_egress_ip = None
                        region.status = RegionStatus.UNAVAILABLE
                        region.updated_at = utc_now()

    async def list_candidates(self, region_id: str, limit: int = 100) -> list[NodeRecord]:
        async with self.sessions() as session:
            region = await session.get(RegionRecord, region_id)
            if region is None:
                return []
            latest_observation = await session.scalar(
                select(func.max(NodeObservationRecord.observed_at))
            )
            if latest_observation is None:
                return []
            statement = (
                select(NodeRecord)
                .where(
                    NodeRecord.country_code.in_(region.countries),
                    NodeRecord.last_seen_at == latest_observation,
                    NodeRecord.id.not_in(
                        select(RegionRecord.active_node_id).where(
                            RegionRecord.group_id == region.group_id,
                            RegionRecord.id != region_id,
                            RegionRecord.active_node_id.is_not(None),
                        )
                    ),
                )
                .order_by(NodeRecord.api_score.desc(), NodeRecord.last_seen_at.desc())
                .limit(limit)
            )
            return list(await session.scalars(statement))

    async def set_active_egress_ip(self, region_id: str, egress_ip: str) -> None:
        async with self.sessions() as session, session.begin():
            region = await session.get(RegionRecord, region_id)
            if region is None:
                raise ValueError(f"unknown region: {region_id}")
            region.active_egress_ip = egress_ip
            region.updated_at = utc_now()

    async def record_probe(
        self,
        *,
        region_id: str,
        node_id: int,
        probe_type: str,
        result: str,
        egress_ip: str | None = None,
        country_code: str | None = None,
        latency_ms: float | None = None,
        error_code: str | None = None,
        started_at: datetime | None = None,
    ) -> ProbeRunRecord:
        record = ProbeRunRecord(
            region_id=region_id,
            node_id=node_id,
            probe_type=probe_type,
            result=result,
            egress_ip=egress_ip,
            country_code=country_code,
            latency_median_ms=latency_ms,
            success_rate=1.0 if result == "succeeded" else 0.0,
            error_code=error_code,
            started_at=started_at or utc_now(),
            finished_at=utc_now(),
        )
        async with self.sessions() as session, session.begin():
            session.add(record)
        return record

    async def get_probe_metrics(self, region_id: str, node_id: int) -> ProbeMetrics | None:
        cutoff = utc_now() - timedelta(hours=24)
        async with self.sessions() as session:
            node = await session.get(NodeRecord, node_id)
            if node is None:
                return None
            probes = list(
                await session.scalars(
                    select(ProbeRunRecord).where(
                        ProbeRunRecord.region_id == region_id,
                        ProbeRunRecord.node_id == node_id,
                        ProbeRunRecord.finished_at.is_not(None),
                        ProbeRunRecord.finished_at >= cutoff,
                    )
                )
            )
        if not probes:
            return None
        successful = [probe for probe in probes if probe.result == "succeeded"]
        latencies = [
            probe.latency_median_ms for probe in successful if probe.latency_median_ms is not None
        ]
        throughputs = [
            probe.throughput_mbps for probe in successful if probe.throughput_mbps is not None
        ]
        return ProbeMetrics(
            availability_24h=len(successful) / max(len(probes), 3),
            latency_ms=sum(latencies) / len(latencies) if latencies else 2_000.0,
            throughput_mbps=(sum(throughputs) / len(throughputs) if throughputs else 0.0),
            api_speed_bps=node.api_speed_bps,
            uptime_ms=node.uptime_ms,
            sessions=node.sessions,
        )

    async def confirm_selection_candidate(self, region_id: str, node_id: int) -> int:
        async with self.sessions() as session, session.begin():
            record = await session.get(RegionSelectionRecord, region_id)
            if record is None:
                record = RegionSelectionRecord(region_id=region_id)
                session.add(record)
            if record.candidate_node_id == node_id:
                record.confirmation_rounds += 1
            else:
                record.candidate_node_id = node_id
                record.confirmation_rounds = 1
            record.updated_at = utc_now()
            return record.confirmation_rounds

    async def reset_selection_candidate(self, region_id: str) -> None:
        async with self.sessions() as session, session.begin():
            record = await session.get(RegionSelectionRecord, region_id)
            if record is None:
                record = RegionSelectionRecord(region_id=region_id)
                session.add(record)
            record.candidate_node_id = None
            record.confirmation_rounds = 0
            record.updated_at = utc_now()

    async def get_selection_state(self, region_id: str) -> RegionSelectionRecord | None:
        async with self.sessions() as session:
            return await session.get(RegionSelectionRecord, region_id)

    async def add_event(
        self,
        *,
        code: str,
        message: str,
        level: str = "info",
        region_id: str | None = None,
        node_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                EventRecord(
                    code=code,
                    level=level,
                    message=message,
                    region_id=region_id,
                    node_id=node_id,
                    details=details or {},
                )
            )

    async def create_job(self, *, kind: str, region_id: str | None = None) -> JobRecord:
        record = JobRecord(id=str(uuid4()), kind=kind, region_id=region_id)
        async with self.sessions() as session, session.begin():
            session.add(record)
        return record

    async def get_job(self, job_id: str) -> JobRecord | None:
        async with self.sessions() as session:
            return await session.get(JobRecord, job_id)

    async def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        async with self.sessions() as session:
            statement = select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit)
            return list(await session.scalars(statement))

    async def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus,
        progress: float,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            record = await session.get(JobRecord, job_id)
            if record is None:
                raise ValueError(f"unknown job: {job_id}")
            record.status = status
            record.progress = max(0.0, min(1.0, progress))
            record.error_code = error_code
            if detail is not None:
                record.detail = detail
            record.updated_at = utc_now()

    async def cancel_job(self, job_id: str) -> JobRecord | None:
        async with self.sessions() as session, session.begin():
            record = await session.get(JobRecord, job_id)
            if record is None:
                return None
            if record.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                record.status = JobStatus.CANCELLED
                record.progress = 1.0
                record.error_code = "CANCELLED_BY_USER"
                record.detail = {"message": "任务已由操作员取消。"}
                record.updated_at = utc_now()
            return record

    async def fail_interrupted_jobs(self) -> None:
        async with self.sessions() as session, session.begin():
            records = await session.scalars(
                select(JobRecord).where(JobRecord.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            )
            for record in records:
                record.status = JobStatus.FAILED
                record.error_code = "PROCESS_RESTARTED"
                record.detail = {"message": "API 重启, 任务未能完成。"}
                record.updated_at = utc_now()

    async def cleanup_retention(
        self,
        *,
        observations_days: int,
        probes_days: int,
        completed_jobs_days: int,
        events_days: int,
    ) -> dict[str, int]:
        now = utc_now()
        completed_statuses = [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED]
        async with self.sessions() as session, session.begin():
            statements = {
                "observations": delete(NodeObservationRecord).where(
                    NodeObservationRecord.observed_at < now - timedelta(days=observations_days)
                ),
                "probes": delete(ProbeRunRecord).where(
                    ProbeRunRecord.finished_at.is_not(None),
                    ProbeRunRecord.finished_at < now - timedelta(days=probes_days),
                ),
                "jobs": delete(JobRecord).where(
                    JobRecord.status.in_(completed_statuses),
                    JobRecord.updated_at < now - timedelta(days=completed_jobs_days),
                ),
                "events": delete(EventRecord).where(
                    EventRecord.created_at < now - timedelta(days=events_days)
                ),
            }
            counts: dict[str, int] = {}
            for name, statement in statements.items():
                result = await session.execute(statement)
                rowcount = getattr(result, "rowcount", 0)
                counts[name] = max(0, rowcount or 0)
            return counts

    async def list_events(self, limit: int = 100) -> list[EventRecord]:
        async with self.sessions() as session:
            statement = select(EventRecord).order_by(EventRecord.created_at.desc()).limit(limit)
            return list(await session.scalars(statement))

    async def list_events_after(self, event_id: int, limit: int = 100) -> list[EventRecord]:
        async with self.sessions() as session:
            statement = (
                select(EventRecord)
                .where(EventRecord.id > event_id)
                .order_by(EventRecord.id)
                .limit(limit)
            )
            return list(await session.scalars(statement))
