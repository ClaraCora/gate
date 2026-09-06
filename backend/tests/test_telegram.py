from __future__ import annotations

from typing import Any

import httpx
import pytest
from gate.config import TelegramConfig
from gate.telegram import TelegramBot, TelegramNotifier


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


@pytest.mark.asyncio
async def test_telegram_bot_status_command_is_chat_scoped() -> None:
    notifier = TelegramNotifier(TelegramConfig(enabled=True, bot_token="token", chat_id="chat"))
    sent: list[tuple[str, dict[str, object] | None]] = []

    async def send(message: str, *, reply_markup: dict[str, object] | None = None) -> bool:
        sent.append((message, reply_markup))
        return True

    notifier.send = send  # type: ignore[method-assign]
    bot = TelegramBot(notifier)

    async def status() -> tuple[str, list[list[dict[str, str]]]]:
        return "Japan 01 - 203.0.113.10 - success 4/5", [
            [{"text": "Switch Japan 01", "callback_data": "switch:jp"}]
        ]

    async def switch(region_id: str) -> str:
        return f"queued {region_id}"

    bot.set_handlers(status_provider=status, switch_handler=switch)
    await bot.handle_update({"message": {"chat": {"id": "other"}, "text": "/status"}})
    assert sent == []
    await bot.handle_update({"message": {"chat": {"id": "chat"}, "text": "/status"}})
    assert sent == [
        (
            "Japan 01 - 203.0.113.10 - success 4/5",
            {"inline_keyboard": [[{"text": "Switch Japan 01", "callback_data": "switch:jp"}]]},
        )
    ]


@pytest.mark.asyncio
async def test_telegram_bot_callback_queues_requested_region() -> None:
    notifier = TelegramNotifier(TelegramConfig(enabled=True, bot_token="token", chat_id="123"))
    sent: list[str] = []
    answered: list[dict[str, object]] = []

    async def send(message: str, *, reply_markup: dict[str, object] | None = None) -> bool:
        del reply_markup
        sent.append(message)
        return True

    async def request(
        method: str,
        payload: dict[str, object],
        *,
        read_timeout: float | None = None,
    ) -> dict[str, object]:
        del read_timeout
        if method == "answerCallbackQuery":
            answered.append(payload)
        return {"ok": True, "result": {}}

    notifier.send = send  # type: ignore[method-assign]
    notifier._request = request  # type: ignore[method-assign]
    bot = TelegramBot(notifier)

    async def status() -> tuple[str, list[list[dict[str, str]]]]:
        return "status", []

    async def switch(region_id: str) -> str:
        return f"queued {region_id}"

    bot.set_handlers(status_provider=status, switch_handler=switch)
    await bot.handle_update(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "switch:jp-02",
                "message": {"chat": {"id": 123}},
            }
        }
    )
    assert answered == [{"callback_query_id": "callback-1"}]
    assert sent == [
        "确认切换到「jp-02」?\n\n系统将按排除机制随机尝试最多 5 个候选出口。"
    ]

    await bot.handle_update(
        {
            "callback_query": {
                "id": "callback-2",
                "data": "switch:confirm:jp-02",
                "message": {"chat": {"id": 123}},
            }
        }
    )
    assert sent[-1] == "queued jp-02"


@pytest.mark.asyncio
async def test_telegram_bot_shortcuts_and_cancel_do_not_switch() -> None:
    notifier = TelegramNotifier(TelegramConfig(enabled=True, bot_token="token", chat_id="chat"))
    sent: list[tuple[str, dict[str, object] | None]] = []
    switched: list[str] = []

    async def send(message: str, *, reply_markup: dict[str, object] | None = None) -> bool:
        sent.append((message, reply_markup))
        return True

    notifier.send = send  # type: ignore[method-assign]
    bot = TelegramBot(notifier)

    async def status() -> tuple[str, list[list[dict[str, str]]]]:
        return "status", [[{"text": "🔀 切换 JP", "callback_data": "switch:jp"}]]

    async def switch(region_id: str) -> str:
        switched.append(region_id)
        return "queued"

    bot.set_handlers(status_provider=status, switch_handler=switch)
    await bot.handle_update({"message": {"chat": {"id": "chat"}, "text": "/start"}})
    assert sent[-1][1] is not None
    assert sent[-1][1]["keyboard"][0][0]["text"] == "📊 查看状态"  # type: ignore[index]

    await bot.handle_update({"message": {"chat": {"id": "chat"}, "text": "🔀 切换出口"}})
    assert sent[-1] == (
        "请选择要切换的入口:",
        {"inline_keyboard": [[{"text": "🔀 切换 JP", "callback_data": "switch:jp"}]]},
    )

    await bot.handle_update(
        {
            "callback_query": {
                "id": "callback-cancel",
                "data": "switch:cancel",
                "message": {"chat": {"id": "chat"}},
            }
        }
    )
    assert switched == []
    assert sent[-1][0] == "已取消切换。"
