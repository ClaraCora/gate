from __future__ import annotations

from gate.probes import parse_cloudflare_trace, socks_proxy_url


def test_parses_cloudflare_trace_without_accepting_malformed_lines() -> None:
    trace = parse_cloudflare_trace("fl=123\nip=203.0.113.9\nmalformed\nloc=JP\n")

    assert trace == {"fl": "123", "ip": "203.0.113.9", "loc": "JP"}


def test_socks_proxy_url_encodes_credentials_and_ipv6_hosts() -> None:
    assert (
        socks_proxy_url(
            "::1",
            11081,
            username="gate.user",
            password="p@ss:/?#[]!word",
        )
        == "socks5://gate.user:p%40ss%3A%2F%3F%23%5B%5D%21word@[::1]:11081"
    )
