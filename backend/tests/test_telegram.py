from __future__ import annotations

from typing import Any

import httpx
import pytest
from gate.config import TelegramConfig
from gate.telegram import TelegramNotifier


@pytest.mark.asyncio
async def test_telegram_notifier_posts_message(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    notifier = TelegramNotifier(
        TelegramConfig(
            enabled=True,
            bot_token="token",
            chat_id="chat",
            api_base_url="https://telegram.test",
        )
    )

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert await notifier.send("人工干预") is True

    assert requests[0].url == "https://telegram.test/bottoken/sendMessage"
    assert requests[0].content.decode("utf-8") == (
        '{"chat_id":"chat","text":"人工干预","disable_web_page_preview":true}'
    )
