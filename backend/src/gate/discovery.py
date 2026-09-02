from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from gate.database import Database
from gate.domain import FeedParseResult, SanitizedProfile
from gate.errors import ProfileRejectedError
from gate.profiles import sanitize_openvpn_profile
from gate.vpngate import fetch_vpngate_feed_sources

FeedFetcher = Callable[[], Awaitable[FeedParseResult]]


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    discovered: int
    accepted: int
    rejected_feed_rows: int
    rejected_profiles: int
    warnings: tuple[str, ...]
    observed_at: datetime
    source_url: str


class DiscoveryService:
    def __init__(
        self,
        database: Database,
        *,
        feed_url: str,
        fallback_urls: tuple[str, ...] = (),
        fetcher: FeedFetcher | None = None,
    ) -> None:
        self.database = database
        self.feed_url = feed_url
        self.fallback_urls = fallback_urls
        self.fetcher = fetcher
        self.profiles: dict[str, SanitizedProfile] = {}
        self.last_source_url = ""

    async def _fetch(self) -> FeedParseResult:
        if self.fetcher is not None:
            self.last_source_url = "injected"
            return await self.fetcher()
        feed, source_url = await fetch_vpngate_feed_sources((self.feed_url, *self.fallback_urls))
        self.last_source_url = source_url
        return feed

    async def refresh(self) -> DiscoverySummary:
        feed = await self._fetch()
        accepted = []
        warnings = list(feed.warnings)
        rejected_profiles = 0
        for node in feed.nodes:
            try:
                profile = sanitize_openvpn_profile(
                    node.openvpn_config_base64,
                    expected_ip=node.ip,
                )
            except ProfileRejectedError as exc:
                rejected_profiles += 1
                if len(warnings) < 40:
                    warnings.append(f"{node.hostname} ({node.ip}): {exc}")
                continue
            accepted.append((node, profile))
            self.profiles[profile.fingerprint] = profile

        observed_at = datetime.now(UTC)
        ingested = await self.database.ingest_nodes(accepted, observed_at)
        summary = DiscoverySummary(
            discovered=len(feed.nodes) + feed.rejected_rows,
            accepted=ingested,
            rejected_feed_rows=feed.rejected_rows,
            rejected_profiles=rejected_profiles,
            warnings=tuple(warnings),
            observed_at=observed_at,
            source_url=self.last_source_url,
        )
        await self.database.add_event(
            code="DISCOVERY_COMPLETED",
            message=f"节点刷新完成, 已接收 {summary.accepted} 个 VPN Gate 节点",
            details={
                "discovered": summary.discovered,
                "accepted": summary.accepted,
                "rejected_feed_rows": summary.rejected_feed_rows,
                "rejected_profiles": summary.rejected_profiles,
                "source_url": summary.source_url,
            },
        )
        return summary
