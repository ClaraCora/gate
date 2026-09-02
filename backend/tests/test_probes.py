from __future__ import annotations

from gate.probes import parse_cloudflare_trace


def test_parses_cloudflare_trace_without_accepting_malformed_lines() -> None:
    trace = parse_cloudflare_trace("fl=123\nip=203.0.113.9\nmalformed\nloc=JP\n")

    assert trace == {"fl": "123", "ip": "203.0.113.9", "loc": "JP"}
