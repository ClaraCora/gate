"""Durable endpoint evidence and deterministic, availability-first selection.

No network operations live here: explanations and ranking are safe to read from UI.
Evidence is shared across entrances; configuration fingerprints never reset cooldowns.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, Float, Integer, String, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from gate.config import SelectionPolicy
from gate.database import (
    Base,
    Database,
    NodeObservationRecord,
    NodeRecord,
    ProbeRunRecord,
    RegionRecord,
    RegionSlotRecord,
    RuntimeStateRecord,
    utc_now,
)
from gate.errors import GateError


def timestamp(value: datetime | None) -> float:
    return value.replace(tzinfo=UTC).timestamp() if value else 0


def endpoint(node: NodeRecord) -> str:
    return f"{node.country_code}:{node.ip}:{node.transport}:{node.port}"


def failure_category(exc: BaseException) -> str:
    code = exc.code if isinstance(exc, GateError) else "INTERNAL"
    if code in {"PROBE_FAILED", "TUNNEL_CONNECT_FAILED"}:
        return "node"
    if code == "SOCKS_UNAVAILABLE":
        return "local"
    if code == "DETECTOR_UNAVAILABLE":
        return "detector"
    if code in {"NOISY_CANDIDATE", "DUPLICATE_EXIT", "CANDIDATE_EXCLUDED"}:
        return "policy"
    if code == "REGION_MISMATCH":
        return "node"
    if code == "SWITCH_BUSY":
        return "busy"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    return "local"


class EndpointHistory(Base):
    __tablename__ = "selection_endpoint_history"
    endpoint: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_success: Mapped[float] = mapped_column(Float, default=0)
    last_failure: Mapped[float] = mapped_column(Float, default=0)
    failure_streak: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[float] = mapped_column(Float, default=0)
    continuous_seconds: Mapped[float] = mapped_column(Float, default=0)
    pre_failure_continuous: Mapped[float] = mapped_column(Float, default=0)
    last_observation: Mapped[float] = mapped_column(Float, default=0)
    last_sample: Mapped[float] = mapped_column(Float, default=0)
    observation_session: Mapped[str] = mapped_column(String(128), default="")
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_bps: Mapped[float | None] = mapped_column(Float, nullable=True)


class HealthEvidence(Base):
    __tablename__ = "selection_health_evidence"
    endpoint: Mapped[str] = mapped_column(String(128), primary_key=True)
    sampled_at: Mapped[int] = mapped_column(Integer, primary_key=True)
    success: Mapped[bool] = mapped_column(Boolean)
    observed_seconds: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class ProfileEvidence(Base):
    __tablename__ = "selection_profile_evidence"
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    verified_at: Mapped[float] = mapped_column(Float)
    egress_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)


class FailureIncident(Base):
    __tablename__ = "selection_failure_incidents"
    incident_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128), index=True)
    occurred_at: Mapped[float] = mapped_column(Float, index=True)


class StandbyRecord(Base):
    __tablename__ = "selection_standby_pool"
    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    egress_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    validated_at: Mapped[float] = mapped_column(Float)


@dataclass
class Evidence:
    tier: str = "unverified"
    successes: int = 0
    samples: int = 0
    observed_seconds: float = 0
    continuous_seconds: float = 0
    failures_24h: int = 0
    wilson_lower: float = 0
    last_verified: float = 0
    profile_verified: bool = False
    cooldown_until: float = 0
    latency_ms: float | None = None
    noise_bps: float | None = None
    egress_ip: str | None = None
    feed_presence: float = 0
    feed_observations: int = 0


@dataclass
class SelectionEntry:
    node: NodeRecord
    evidence: Evidence
    excluded: list[str]

    def response(self) -> dict[str, Any]:
        return {
            "node_id": self.node.id,
            "endpoint": endpoint(self.node),
            "name": self.node.hostname,
            "ip": self.node.ip,
            "evidence": asdict(self.evidence),
            "exclusions": self.excluded,
        }


def wilson_lower(successes: int, samples: int) -> float:
    if not samples:
        return 0
    z = 1.959963984540054
    p = successes / samples
    return (
        p
        + z * z / (2 * samples)
        - z * math.sqrt(p * (1 - p) / samples + z * z / (4 * samples * samples))
    ) / (1 + z * z / samples)


def ranking(entry: SelectionEntry) -> tuple[float, ...]:
    e, n = entry.evidence, entry.node
    if e.tier == "unverified":
        return (
            2,
            -e.feed_presence,
            -e.feed_observations,
            -n.uptime_ms,
            n.api_ping_ms if n.api_ping_ms is not None else math.inf,
            n.id,
        )
    return (
        0 if e.tier == "stable" else 1,
        e.failures_24h,
        -e.wilson_lower,
        -e.continuous_seconds,
        -e.last_verified,
        e.latency_ms if e.latency_ms is not None else math.inf,
        n.id,
    )


class SelectionStore:
    def __init__(self, database: Database) -> None:
        self.db = database
        self.lock = asyncio.Lock()
        # A restart is an observation gap. Never infer uptime while the API was down.
        self.session_id = uuid4().hex

    async def observe(
        self,
        node_id: int,
        *,
        success: bool | None,
        region_id: str,
        now: float | None = None,
        latency_ms: float | None = None,
        egress_ip: str | None = None,
        active: bool = True,
    ) -> None:
        if success is None:
            return
        now = utc_now().timestamp() if now is None else now
        async with self.lock, self.db.sessions() as session, session.begin():
            node = await session.get(NodeRecord, node_id)
            if node is None:
                return
            key = endpoint(node)
            h = await session.get(EndpointHistory, key)
            if h is None:
                h = EndpointHistory(endpoint=key)
                session.add(h)
                await session.flush()
            if success:
                h.last_success = now
                if latency_ms is not None:
                    h.latency_ms = latency_ms
                p = await session.get(ProfileEvidence, node.fingerprint)
                if p is None:
                    session.add(
                        ProfileEvidence(
                            fingerprint=node.fingerprint, verified_at=now, egress_ip=egress_ip
                        )
                    )
                else:
                    p.verified_at = now
                    if egress_ip is not None:
                        p.egress_ip = egress_ip
            last: HealthEvidence | None = await session.scalar(
                select(HealthEvidence)
                .where(HealthEvidence.endpoint == key)
                .order_by(HealthEvidence.sampled_at.desc())
                .limit(1)
            )
            within_bucket = last is not None and now - last.sampled_at < 300
            if last is not None and within_bucket and (not active or last.active):
                last.success = success
                return
            elapsed = now - h.last_observation
            same_session = h.observation_session == self.session_id
            observed = min(elapsed, 300) if active and same_session and 0 < elapsed <= 450 else 0
            if not success:
                observed = 0
            if active:
                previous_continuous = h.continuous_seconds
                h.continuous_seconds = h.continuous_seconds + observed if observed else 0
                h.last_observation = now if success else 0
                h.last_sample = now
                h.observation_session = self.session_id
                if success:
                    h.pre_failure_continuous = 0
                elif h.pre_failure_continuous == 0:
                    h.pre_failure_continuous = previous_continuous
            if within_bucket and last is not None:
                last.success = success
                last.active = active
                last.observed_seconds = observed
            else:
                session.add(
                    HealthEvidence(
                        endpoint=key,
                        sampled_at=int(now),
                        success=success,
                        observed_seconds=observed,
                        active=active,
                    )
                )

    async def fail(self, node_id: int, incident: str, *, now: float | None = None) -> bool:
        now = utc_now().timestamp() if now is None else now
        async with self.lock, self.db.sessions() as session, session.begin():
            if await session.get(FailureIncident, incident):
                return False
            node = await session.get(NodeRecord, node_id)
            if node is None:
                return False
            key = endpoint(node)
            h = await session.get(EndpointHistory, key)
            if h is None:
                h = EndpointHistory(endpoint=key)
                session.add(h)
                await session.flush()
            # A day of observed recovery ends an old escalation episode. A single
            # successful probe or a changed profile cannot reset its penalty.
            recovered = max(h.continuous_seconds, h.pre_failure_continuous)
            if recovered >= 86400 and now - h.last_failure >= 86400:
                h.failure_streak = 0
            h.failure_streak += 1
            h.last_failure = now
            h.cooldown_until = now + (1800, 7200, 43200, 86400)[min(h.failure_streak - 1, 3)]
            h.continuous_seconds = 0
            h.pre_failure_continuous = 0
            h.last_observation = 0
            session.add(FailureIncident(incident_id=incident, endpoint=key, occurred_at=now))
            return True

    async def set_noise(self, node_id: int, rate: float) -> None:
        node = await self.db.get_node(node_id)
        if node is None:
            return
        async with self.lock, self.db.sessions() as session, session.begin():
            h = await session.get(EndpointHistory, endpoint(node))
            if h:
                h.noise_bps = rate

    async def verify_profile(self, node_id: int, egress_ip: str) -> None:
        node = await self.db.get_node(node_id)
        if node is None:
            return
        now = utc_now().timestamp()
        async with self.lock, self.db.sessions() as session, session.begin():
            history = await session.get(EndpointHistory, endpoint(node))
            if history is None:
                session.add(EndpointHistory(endpoint=endpoint(node), last_success=now))
            else:
                history.last_success = now
            profile = await session.get(ProfileEvidence, node.fingerprint)
            if profile is None:
                session.add(
                    ProfileEvidence(
                        fingerprint=node.fingerprint,
                        verified_at=now,
                        egress_ip=egress_ip,
                    )
                )
            else:
                profile.verified_at = now
                profile.egress_ip = egress_ip

    async def get_evidence(
        self, policy: SelectionPolicy, node_ids: set[int] | None = None
    ) -> dict[int, Evidence]:
        evidence = await self.snapshot(policy)
        return evidence if node_ids is None else {i: evidence[i] for i in node_ids if i in evidence}

    async def defer_candidate(self, group_id: str, node: NodeRecord) -> None:
        """Briefly defer incomplete verification without marking a node as failed."""
        now = utc_now().timestamp()
        key = f"candidate_deferrals:{group_id}"
        async with self.lock:
            state = await self.db.get_runtime_state(key)
            state = {key: until for key, until in state.items() if until > now}
            state[endpoint(node)] = now + 300
            await self.db.set_runtime_state(key, state)

    async def standby(self, group_id: str) -> list[StandbyRecord]:
        async with self.db.sessions() as session:
            return list(
                await session.scalars(
                    select(StandbyRecord)
                    .where(StandbyRecord.group_id == group_id)
                    .order_by(StandbyRecord.validated_at)
                )
            )

    async def save_standby(self, group_id: str, node: NodeRecord, egress_ip: str | None) -> None:
        async with self.lock, self.db.sessions() as session, session.begin():
            record = await session.get(StandbyRecord, (group_id, endpoint(node)))
            values = {
                "node_id": node.id,
                "fingerprint": node.fingerprint,
                "egress_ip": egress_ip,
                "validated_at": utc_now().timestamp(),
            }
            if record is None:
                session.add(StandbyRecord(group_id=group_id, endpoint=endpoint(node), **values))
            else:
                for name, value in values.items():
                    setattr(record, name, value)

    async def remove_standby(self, group_id: str, node_endpoint: str) -> None:
        async with self.lock, self.db.sessions() as session, session.begin():
            await session.execute(
                delete(StandbyRecord).where(
                    StandbyRecord.group_id == group_id, StandbyRecord.endpoint == node_endpoint
                )
            )

    async def snapshot(self, policy: SelectionPolicy) -> dict[int, Evidence]:
        now = utc_now().timestamp()
        async with self.db.sessions() as session:
            nodes = list(await session.scalars(select(NodeRecord)))
            histories = {h.endpoint: h for h in await session.scalars(select(EndpointHistory))}
            profiles = {p.fingerprint: p for p in await session.scalars(select(ProfileEvidence))}
            samples = (
                await session.execute(
                    select(
                        HealthEvidence.endpoint,
                        func.count(),
                        func.sum(HealthEvidence.success.cast(Integer)),
                        func.sum(HealthEvidence.observed_seconds),
                    )
                    .where(HealthEvidence.sampled_at >= now - 7 * 86400)
                    .group_by(HealthEvidence.endpoint)
                )
            ).all()
            totals = {row[0]: row[1:] for row in samples}
            # After a confirmed incident, stable eligibility requires NEW evidence.
            new_observed_rows = (
                await session.execute(
                    select(
                        HealthEvidence.endpoint,
                        func.sum(HealthEvidence.observed_seconds),
                    )
                    .join(EndpointHistory, EndpointHistory.endpoint == HealthEvidence.endpoint)
                    .where(
                        HealthEvidence.sampled_at >= now - 7 * 86400,
                        HealthEvidence.sampled_at > EndpointHistory.last_failure,
                    )
                    .group_by(HealthEvidence.endpoint)
                )
            ).all()
            new_observed: dict[str, float] = {
                str(key): float(value or 0) for key, value in new_observed_rows
            }
            incident_rows = (
                await session.execute(
                    select(
                        FailureIncident.endpoint,
                        func.count(),
                    )
                    .where(FailureIncident.occurred_at >= now - 86400)
                    .group_by(FailureIncident.endpoint)
                )
            ).all()
            incidents: dict[str, int] = {str(key): int(value or 0) for key, value in incident_rows}
            since = utc_now() - timedelta(days=7)
            total_feeds = await session.scalar(
                select(func.count(func.distinct(NodeObservationRecord.observed_at))).where(
                    NodeObservationRecord.observed_at >= since
                )
            )
            observation_rows = (
                await session.execute(
                    select(
                        NodeObservationRecord.node_id,
                        func.count(func.distinct(NodeObservationRecord.observed_at)),
                    )
                    .where(NodeObservationRecord.observed_at >= since)
                    .group_by(NodeObservationRecord.node_id)
                )
            ).all()
            observations: dict[int, int] = {
                int(key): int(value or 0) for key, value in observation_rows
            }
        result: dict[int, Evidence] = {}
        for n in nodes:
            key = endpoint(n)
            h, p = histories.get(key), profiles.get(n.fingerprint)
            count, successes, observed = totals.get(key, (0, 0, 0))
            e = Evidence(
                samples=count,
                successes=successes,
                observed_seconds=observed,
                wilson_lower=wilson_lower(successes, count),
                failures_24h=incidents.get(key, 0),
                feed_observations=observations.get(n.id, 0),
            )
            e.feed_presence = e.feed_observations / max(total_feeds or 0, 1)
            if h:
                e.last_verified = h.last_success
                e.continuous_seconds = (
                    h.continuous_seconds if now - h.last_observation <= 450 else 0
                )
                e.cooldown_until, e.latency_ms, e.noise_bps = (
                    h.cooldown_until,
                    h.latency_ms,
                    h.noise_bps,
                )
                if h.last_success:
                    e.tier = "verified"
                if (
                    observed >= policy.stable_observation_hours * 3600
                    and new_observed.get(key, 0) >= policy.stable_observation_hours * 3600
                    and count
                    and successes / count >= policy.stable_success_rate
                    and not e.failures_24h
                    and now - h.last_success <= 86400
                    and p is not None
                    and now - p.verified_at <= 86400
                ):
                    e.tier = "stable"
            if p:
                e.profile_verified = now - p.verified_at <= 86400
                e.egress_ip = p.egress_ip
            result[n.id] = e
        return result

    async def explain(
        self,
        region_id: str,
        policy: SelectionPolicy,
        profiles: set[str],
        *,
        ignore_round: bool = False,
    ) -> list[SelectionEntry]:
        now = utc_now().timestamp()
        evidence = await self.snapshot(policy)
        async with self.db.sessions() as session:
            region = await session.get(RegionRecord, region_id)
            if region is None:
                return []
            nodes = list(
                await session.scalars(
                    select(NodeRecord).where(NodeRecord.country_code.in_(region.countries))
                )
            )
            all_nodes = {n.id: n for n in await session.scalars(select(NodeRecord))}
            latest = await session.scalar(select(func.max(NodeObservationRecord.observed_at)))
            siblings = list(
                await session.scalars(
                    select(RegionRecord).where(RegionRecord.group_id == region.group_id)
                )
            )
            slots = list(
                await session.scalars(
                    select(RegionSlotRecord).where(
                        RegionSlotRecord.region_id.in_([r.id for r in siblings]),
                        RegionSlotRecord.state.in_(("active", "switching", "draining")),
                    )
                )
            )
            # Existing per-entry policy exclusions also apply to sibling entries.
            # A noisy endpoint must not be provisioned again once for each port.
            noise_records = list(
                await session.scalars(
                    select(RuntimeStateRecord).where(
                        RuntimeStateRecord.key.in_([f"noise_exclusions:{r.id}" for r in siblings])
                    )
                )
            )
            noise_state: dict[str, float] = {}
            for record in noise_records:
                for key, until in record.value.items():
                    noise_state[key] = max(noise_state.get(key, 0), until)
            deferred = await self.db.get_runtime_state(f"candidate_deferrals:{region.group_id}")
        occupied_ids = {s.node_id for s in slots if s.node_id is not None}
        occupied_ids.update(r.active_node_id for r in siblings if r.active_node_id is not None)
        occupied = {endpoint(all_nodes[i]) for i in occupied_ids if i in all_nodes}
        occupied_ips = {s.egress_ip for s in slots if s.egress_ip}
        occupied_ips.update(r.active_egress_ip for r in siblings if r.active_egress_ip)
        round_state = await self.db.get_runtime_state(f"selection_round:{region_id}")
        failed = set(round_state.get("failed_endpoints", []))
        result = []
        for node in nodes:
            e = evidence[node.id]
            reasons = []
            if node.id == region.active_node_id:
                reasons.append("current")
            elif endpoint(node) in occupied:
                reasons.append("occupied_or_reserved")
            if e.egress_ip and e.egress_ip in occupied_ips:
                reasons.append("duplicate_exit")
            if timestamp(node.blacklisted_until) > now:
                reasons.append("blacklisted")
            if e.cooldown_until > now:
                reasons.append("cooldown")
            if noise_state.get(endpoint(node), 0) > now or noise_state.get(str(node.id), 0) > now:
                reasons.append("noise_cooldown")
            if deferred.get(endpoint(node), 0) > now:
                reasons.append("verification_pending")
            if not ignore_round and endpoint(node) in failed:
                reasons.append("round_failed")
            if node.fingerprint not in profiles:
                reasons.append("profile_missing")
            if node.last_seen_at != latest and not (
                e.tier == "stable"
                and now - timestamp(node.last_seen_at) <= 86400
                and now - e.last_verified <= 86400
            ):
                reasons.append("absent_from_feed")
            result.append(SelectionEntry(node, e, reasons))
        result.sort(key=ranking)
        # A feed can contain multiple profiles for one endpoint. Try it only once.
        seen: set[str] = set()
        for entry in result:
            if not entry.excluded:
                key = endpoint(entry.node)
                if key in seen:
                    entry.excluded.append("duplicate_endpoint")
                seen.add(key)
        return result

    async def migrate(self) -> None:
        if await self.db.get_runtime_state("selection_migration_v1"):
            return
        # Legacy probes reliably identify a successful profile, but not continuously
        # observed time or confirmed incident boundaries. Do not invent those.
        async with self.lock, self.db.sessions() as session, session.begin():
            nodes = {n.id: n for n in await session.scalars(select(NodeRecord))}
            probes = list(
                await session.scalars(
                    select(ProbeRunRecord)
                    .where(
                        ProbeRunRecord.result == "succeeded",
                        ProbeRunRecord.finished_at >= utc_now() - timedelta(days=7),
                    )
                    .order_by(ProbeRunRecord.finished_at)
                )
            )
            histories: dict[str, EndpointHistory] = {}
            profiles: dict[str, ProfileEvidence] = {}
            for probe in probes:
                node = nodes.get(probe.node_id)
                if node is None:
                    continue
                key, at = endpoint(node), timestamp(probe.finished_at)
                h = histories.get(key)
                if h is None:
                    h = await session.get(EndpointHistory, key)
                    if h is None:
                        h = EndpointHistory(endpoint=key)
                        session.add(h)
                    histories[key] = h
                h.last_success, h.latency_ms = at, probe.latency_median_ms
                p = profiles.get(node.fingerprint)
                if p is None:
                    p = await session.get(ProfileEvidence, node.fingerprint)
                    if p is None:
                        p = ProfileEvidence(fingerprint=node.fingerprint, verified_at=at)
                        session.add(p)
                    profiles[node.fingerprint] = p
                p.verified_at = at
                if probe.egress_ip is not None:
                    p.egress_ip = probe.egress_ip
        await self.db.set_runtime_state(
            "selection_migration_v1",
            {
                "completed_at": utc_now().isoformat(),
                "observed_time_backfilled": False,
            },
        )

    async def prune(self) -> None:
        cutoff = utc_now().timestamp() - 30 * 86400
        async with self.lock, self.db.sessions() as session, session.begin():
            await session.execute(delete(HealthEvidence).where(HealthEvidence.sampled_at < cutoff))
            await session.execute(
                delete(FailureIncident).where(FailureIncident.occurred_at < cutoff)
            )
            await session.execute(
                delete(ProfileEvidence).where(ProfileEvidence.verified_at < cutoff)
            )
            await session.execute(delete(StandbyRecord).where(StandbyRecord.validated_at < cutoff))
