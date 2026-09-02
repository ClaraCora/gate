from __future__ import annotations

import csv
import io
import ipaddress
import re
from collections.abc import Mapping

import httpx

from gate.domain import FeedParseResult, VpnGateNode
from gate.errors import FeedParseError

DEFAULT_FEED_URL = "https://www.vpngate.net/api/iphone/"
MAX_FEED_BYTES = 32 * 1024 * 1024
MAX_CONFIG_BASE64_CHARS = 1024 * 1024
COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")

REQUIRED_COLUMNS = frozenset(
    {
        "HostName",
        "IP",
        "Score",
        "Ping",
        "Speed",
        "CountryLong",
        "CountryShort",
        "NumVpnSessions",
        "Uptime",
        "TotalUsers",
        "TotalTraffic",
        "LogType",
        "Operator",
        "Message",
        "OpenVPN_ConfigData_Base64",
    }
)


def _normalize_feed(payload: str) -> str:
    normalized = payload.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    try:
        header_index = next(i for i, line in enumerate(lines) if line.startswith("#HostName,"))
    except StopIteration as exc:
        raise FeedParseError("VPN Gate feed is missing the CSV header") from exc

    csv_lines: list[str] = []
    for line in lines[header_index:]:
        if line.strip() == "*":
            break
        if line.strip():
            csv_lines.append(line)

    if not csv_lines:
        raise FeedParseError("VPN Gate feed contains no CSV data")
    csv_lines[0] = csv_lines[0].removeprefix("#")
    return "\n".join(csv_lines)


def _required_text(row: Mapping[str, str | None], column: str) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise ValueError(f"{column} is empty")
    return value


def _non_negative_int(row: Mapping[str, str | None], column: str) -> int:
    value = int(_required_text(row, column))
    if value < 0:
        raise ValueError(f"{column} must not be negative")
    return value


def _optional_non_negative_int(row: Mapping[str, str | None], column: str) -> int | None:
    raw = (row.get(column) or "").strip()
    if not raw or raw == "-":
        return None
    value = int(raw)
    if value < 0:
        raise ValueError(f"{column} must not be negative")
    return value


def _parse_row(row: Mapping[str, str | None]) -> VpnGateNode:
    ip = _required_text(row, "IP")
    address = ipaddress.ip_address(ip)
    if address.version != 4 or not address.is_global:
        raise ValueError("IP must be a global IPv4 address")

    country_code = _required_text(row, "CountryShort").upper()
    if not COUNTRY_CODE.fullmatch(country_code):
        raise ValueError("CountryShort must be a two-letter code")

    config_base64 = _required_text(row, "OpenVPN_ConfigData_Base64")
    if len(config_base64) > MAX_CONFIG_BASE64_CHARS:
        raise ValueError("OpenVPN config is larger than the configured limit")

    return VpnGateNode(
        hostname=_required_text(row, "HostName"),
        ip=ip,
        country_long=_required_text(row, "CountryLong"),
        country_code=country_code,
        score=_non_negative_int(row, "Score"),
        ping_ms=_optional_non_negative_int(row, "Ping"),
        speed_bps=_non_negative_int(row, "Speed"),
        sessions=_non_negative_int(row, "NumVpnSessions"),
        uptime_ms=_non_negative_int(row, "Uptime"),
        total_users=_non_negative_int(row, "TotalUsers"),
        total_traffic_bytes=_non_negative_int(row, "TotalTraffic"),
        log_type=(row.get("LogType") or "").strip(),
        operator=(row.get("Operator") or "").strip(),
        message=(row.get("Message") or "").strip(),
        openvpn_config_base64=config_base64,
    )


def parse_vpngate_feed(payload: str) -> FeedParseResult:
    """Parse VPN Gate's non-standard CSV envelope with row-level fault isolation."""

    if len(payload.encode("utf-8")) > MAX_FEED_BYTES:
        raise FeedParseError("VPN Gate feed is larger than the configured limit")

    csv.field_size_limit(MAX_CONFIG_BASE64_CHARS * 2)
    reader = csv.DictReader(io.StringIO(_normalize_feed(payload)))
    columns = set(reader.fieldnames or ())
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise FeedParseError(f"VPN Gate feed is missing columns: {', '.join(sorted(missing))}")

    nodes: list[VpnGateNode] = []
    warnings: list[str] = []
    rejected = 0
    for line_number, row in enumerate(reader, start=2):
        try:
            nodes.append(_parse_row(row))
        except (TypeError, ValueError) as exc:
            rejected += 1
            if len(warnings) < 20:
                warnings.append(f"row {line_number}: {exc}")

    if not nodes:
        raise FeedParseError("VPN Gate feed contains no valid nodes")
    return FeedParseResult(tuple(nodes), rejected, tuple(warnings))


async def fetch_vpngate_feed(
    url: str = DEFAULT_FEED_URL,
    *,
    client: httpx.AsyncClient | None = None,
) -> FeedParseResult:
    """Fetch and parse the public feed with bounded timeouts and response size."""

    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=8.0),
        follow_redirects=True,
        headers={"User-Agent": "Gate/0.1 (+https://github.com/ClaraCora/gate)"},
    )
    try:
        response = await http_client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_FEED_BYTES:
            raise FeedParseError("VPN Gate feed is larger than the configured limit")
        return parse_vpngate_feed(response.text)
    finally:
        if owns_client:
            await http_client.aclose()


async def fetch_vpngate_feed_sources(
    urls: tuple[str, ...],
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[FeedParseResult, str]:
    """Try configured feed sources in order and report the source that validated."""

    if not urls:
        raise FeedParseError("No VPN Gate feed sources are configured")
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=8.0),
        follow_redirects=True,
        headers={"User-Agent": "Gate/0.1 (+https://github.com/ClaraCora/gate)"},
    )
    failures: list[str] = []
    try:
        for url in dict.fromkeys(urls):
            try:
                return await fetch_vpngate_feed(url, client=http_client), url
            except (FeedParseError, httpx.HTTPError) as exc:
                failures.append(f"{url}: {exc}")
    finally:
        if owns_client:
            await http_client.aclose()
    raise FeedParseError("All VPN Gate feed sources failed: " + "; ".join(failures))
