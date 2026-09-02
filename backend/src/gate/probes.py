from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from gate.errors import GateError


class ProbeError(GateError):
    code = "PROBE_FAILED"


@dataclass(frozen=True, slots=True)
class EgressProbe:
    egress_ip: str
    country_code: str
    latency_ms: float


def parse_cloudflare_trace(payload: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            values[key] = value.strip()
    return values


def socks_proxy_url(
    host: str,
    port: int,
    *,
    username: str | None = None,
    password: str | None = None,
) -> str:
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    credentials = ""
    if username is not None:
        credentials = f"{quote(username, safe='')}:{quote(password or '', safe='')}@"
    return f"socks5://{credentials}{authority}:{port}"


async def probe_socks_exit(
    host: str,
    port: int,
    *,
    expected_countries: set[str] | frozenset[str],
    timeout_seconds: float = 12.0,
    username: str | None = None,
    password: str | None = None,
) -> EgressProbe:
    proxy = socks_proxy_url(host, port, username=username, password=password)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(timeout_seconds, connect=timeout_seconds),
            follow_redirects=False,
        ) as client:
            trace_response = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
            trace_response.raise_for_status()
            ip_response = await client.get("https://api.ipify.org", params={"format": "json"})
            ip_response.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        raise ProbeError(f"SOCKS exit HTTPS probe failed: {exc}") from exc

    trace = parse_cloudflare_trace(trace_response.text)
    trace_ip = trace.get("ip", "")
    country = trace.get("loc", "").upper()
    try:
        ipify_ip = str(ip_response.json()["ip"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError("secondary egress service returned an invalid response") from exc
    if not trace_ip or trace_ip != ipify_ip:
        raise ProbeError("egress IP services returned different addresses")
    if country not in expected_countries:
        raise ProbeError(f"egress country {country or 'unknown'} is outside the region")
    return EgressProbe(
        egress_ip=trace_ip,
        country_code=country,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )
