from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

import httpx

UsageRecorder = Callable[[int], Awaitable[None]]
usage_recorder: ContextVar[UsageRecorder | None] = ContextVar("http_usage", default=None)


async def bounded_get(client: httpx.AsyncClient, url: str, *, limit: int) -> bytes:
    """Measure response body bytes (compressed on the wire), including failed requests.

    HTTP headers, TLS, TCP and tunnel overhead are deliberately not called measured
    HTTP bytes. They are included only in the independent interface counters.
    """
    downloaded = 0
    try:
        async with client.stream("GET", url, headers={"Accept-Encoding": "gzip"}) as response:
            try:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16_384):
                    body.extend(chunk)
                    if len(body) > limit or response.num_bytes_downloaded > limit:
                        raise ValueError("HTTP response exceeds the byte limit")
                return bytes(body)
            finally:
                downloaded = response.num_bytes_downloaded
                # Pre-buffered MockTransport responses do not increment the counter.
                if downloaded == 0 and response.is_stream_consumed:
                    downloaded = len(response.content) if hasattr(response, "_content") else 0
    finally:
        recorder = usage_recorder.get()
        if recorder is not None:
            await recorder(downloaded)
