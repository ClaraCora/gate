from __future__ import annotations

import asyncio

import pytest
from gate.probes import (
    SocksTransportError,
    parse_cloudflare_trace,
    probe_socks_exit,
    socks_proxy_url,
)
from gate.selection import failure_category


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
        == "socks5h://gate.user:p%40ss%3A%2F%3F%23%5B%5D%21word@[::1]:11081"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [b"", b"\x04\x00"])
async def test_empty_or_malformed_real_socks_reply_is_a_local_error(reply: bytes) -> None:
    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.read(64)
            if data:
                writer.write(reply)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(relay, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(SocksTransportError) as failure:
            await probe_socks_exit("127.0.0.1", port, expected_countries={"JP"})
    assert failure_category(failure.value) == "local"
