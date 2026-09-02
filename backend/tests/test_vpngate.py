from __future__ import annotations

import csv
import io

import httpx
import pytest
from gate.errors import FeedParseError
from gate.vpngate import fetch_vpngate_feed_sources, parse_vpngate_feed

FIELDS = [
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
]


def _feed(rows: list[list[str]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(FIELDS)
    writer.writerows(rows)
    csv_text = stream.getvalue().replace("HostName,", "#HostName,", 1)
    return "*vpn_servers\r\r\n" + csv_text + "*\r\n"


def _valid_row(encoded_profile: str) -> list[str]:
    return [
        "vpn-test",
        "128.211.249.131",
        "3610259",
        "6",
        "5266675",
        "United States",
        "US",
        "12",
        "952461655",
        "1043606",
        "201067291804450",
        "2weeks",
        "Volunteer",
        "fast, stable",
        encoded_profile,
    ]


def test_parses_nonstandard_feed_envelope(encoded_profile: str) -> None:
    result = parse_vpngate_feed(_feed([_valid_row(encoded_profile)]))

    assert len(result.nodes) == 1
    assert result.rejected_rows == 0
    assert result.nodes[0].country_code == "US"
    assert result.nodes[0].message == "fast, stable"


def test_rejects_bad_rows_without_losing_valid_rows(encoded_profile: str) -> None:
    invalid = _valid_row(encoded_profile)
    invalid[1] = "127.0.0.1"
    result = parse_vpngate_feed(_feed([invalid, _valid_row(encoded_profile)]))

    assert len(result.nodes) == 1
    assert result.rejected_rows == 1
    assert "global IPv4" in result.warnings[0]


def test_requires_expected_header() -> None:
    with pytest.raises(FeedParseError, match="missing the CSV header"):
        parse_vpngate_feed("not a VPN Gate response")


def test_requires_at_least_one_valid_node(encoded_profile: str) -> None:
    invalid = _valid_row(encoded_profile)
    invalid[6] = "USA"
    with pytest.raises(FeedParseError, match="no valid nodes"):
        parse_vpngate_feed(_feed([invalid]))


@pytest.mark.asyncio
async def test_feed_sources_fall_back_after_invalid_response(encoded_profile: str) -> None:
    valid_feed = _feed([_valid_row(encoded_profile)])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "direct.test":
            return httpx.Response(200, text="<html>not a feed</html>")
        return httpx.Response(200, text=valid_feed)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result, source_url = await fetch_vpngate_feed_sources(
            ("https://direct.test/feed", "https://relay.test/feed"),
            client=client,
        )

    assert len(result.nodes) == 1
    assert source_url == "https://relay.test/feed"
