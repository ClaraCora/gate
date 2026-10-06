from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from gate.errors import GateError
from gate.http_usage import bounded_get


class ProbeError(GateError):
    code = "PROBE_FAILED"


class DetectorError(ProbeError):
    """An endpoint did not provide evidence that the tunnel is broken."""

    code = "DETECTOR_UNAVAILABLE"


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
    # Use remote DNS resolution so the panel probe follows the same path as
    # real SOCKS5 clients and does not depend on VPS-side DNS reachability.
    return f"socks5h://{credentials}{authority}:{port}"


async def probe_socks_exit(
    host: str,
    port: int,
    *,
    expected_countries: set[str] | frozenset[str],
    timeout_seconds: float = 12.0,
    username: str | None = None,
    password: str | None = None,
    full: bool = True,
    previous_ip: str | None = None,
    secondary_first: bool = False,
    client: httpx.AsyncClient | None = None,
) -> EgressProbe:
    proxy = socks_proxy_url(host, port, username=username, password=password)
    started = time.perf_counter()
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(timeout_seconds, connect=timeout_seconds),
        follow_redirects=False,
        trust_env=False,
    )
    # Keep the normal health path to one request, but use independent providers
    # when a route changes or the first detector is blocked by the exit.
    endpoints = ["cloudflare", "ipify", "ipwho", "ifconfig", "icanhaz", "checkip"]
    if secondary_first:
        endpoints[:2] = ["ipify", "cloudflare"]
    urls = {
        "cloudflare": "https://www.cloudflare.com/cdn-cgi/trace",
        "ipify": "https://api.ipify.org?format=json",
        "ipwho": "https://ipwho.is/",
        "ifconfig": "https://ifconfig.me/ip",
        "icanhaz": "https://icanhazip.com/",
        "checkip": "https://checkip.amazonaws.com/",
    }
    answers: list[tuple[str, str]] = []
    detector_failed = False
    try:
        async with asyncio.timeout(timeout_seconds):
            for endpoint in endpoints:
                try:
                    # Leave time for a second, independent provider after a timeout.
                    async with asyncio.timeout(timeout_seconds / 3):
                        payload = await bounded_get(http_client, urls[endpoint], limit=16_384)
                    if endpoint == "cloudflare":
                        trace = parse_cloudflare_trace(payload.decode())
                        ip, country = trace.get("ip", ""), trace.get("loc", "").upper()
                    elif endpoint in {"ipify", "ipwho"}:
                        value = json.loads(payload)
                        ip, country = str(value["ip"]), str(value.get("country_code", ""))
                        if endpoint == "ipify":
                            country = ""
                    else:
                        ip, country = payload.decode().strip(), ""
                    if not ipaddress.ip_address(ip).is_global:
                        raise ValueError("non-public egress address")
                    if endpoint in {"cloudflare", "ipwho"} and not country:
                        raise ValueError("missing country")
                except (httpx.HTTPStatusError, ValueError, KeyError, TypeError):
                    detector_failed = True
                    continue
                except (httpx.HTTPError, OSError, TimeoutError):
                    continue
                if country and country not in expected_countries:
                    raise ProbeError(f"egress country {country} is outside the region")
                answers.append((ip, country))
                # An unchanged address can be checked cheaply. A changed address
                # always requires two independent providers plus country evidence.
                needs_full = full or (previous_ip is not None and ip != previous_ip)
                if not needs_full and (country or ip == previous_ip):
                    return EgressProbe(
                        ip, country, round((time.perf_counter() - started) * 1000, 2)
                    )
                if len(answers) >= 2:
                    if len({answer[0] for answer in answers}) != 1:
                        raise DetectorError("egress providers disagree; keep current route")
                    verified_country = next((c for _, c in answers if c), "")
                    if verified_country:
                        return EgressProbe(
                            ip, verified_country, round((time.perf_counter() - started) * 1000, 2)
                        )
    except TimeoutError as exc:
        if answers:
            raise DetectorError("verification incomplete; HTTPS tunnel responded") from exc
        raise ProbeError("SOCKS HTTPS deadline exceeded") from exc
    finally:
        if owns_client:
            await http_client.aclose()
    if answers or detector_failed:
        raise DetectorError("verification providers unavailable; keep current route")
    raise ProbeError("independent HTTPS providers could not be reached through SOCKS")
