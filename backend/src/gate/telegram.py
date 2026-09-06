from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from gate.config import TelegramConfig
from gate.errors import GateError


class TelegramNotificationError(GateError):
    code = "TELEGRAM_NOTIFICATION_FAILED"


class TelegramNotifier:
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config

    def set_config(self, config: TelegramConfig) -> None:
        self.config = config

    async def _request(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {}
        url = f"{self.config.api_base_url.rstrip('/')}/bot{self.config.bot_token}/{method}"
        timeout_seconds = read_timeout or self.config.timeout_seconds
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=False,
            ) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                response_payload = response.json()
        except (httpx.HTTPError, OSError) as exc:
            raise TelegramNotificationError("Telegram notification request failed") from exc
        except ValueError as exc:
            raise TelegramNotificationError("Telegram API returned invalid data") from exc
        if not isinstance(response_payload, dict) or response_payload.get("ok") is not True:
            raise TelegramNotificationError("Telegram API rejected the request")
        return response_payload

    async def send(
        self,
        message: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        if not self.config.enabled:
            return False
        payload: dict[str, Any] = {
            "chat_id": self.config.chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._request("sendMessage", payload)
        return True


StatusProvider = Callable[[], Awaitable[tuple[str, list[list[dict[str, str]]]]]]
SwitchHandler = Callable[[str], Awaitable[str]]


class TelegramBot:
    """Chat-scoped long-polling operator bot."""

    def __init__(self, notifier: TelegramNotifier) -> None:
        self.notifier = notifier
        self.status_provider: StatusProvider | None = None
        self.switch_handler: SwitchHandler | None = None
        self._offset = 0

    def set_handlers(
        self,
        *,
        status_provider: StatusProvider,
        switch_handler: SwitchHandler,
    ) -> None:
        self.status_provider = status_provider
        self.switch_handler = switch_handler

    def reset_offset(self) -> None:
        self._offset = 0

    async def run_forever(self) -> None:
        while True:
            if not self.notifier.config.enabled or self.status_provider is None:
                await asyncio.sleep(5)
                continue
            try:
                updates = await self.notifier._request(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    read_timeout=max(self.notifier.config.timeout_seconds, 35.0),
                )
                raw_updates = updates.get("result", [])
                if not isinstance(raw_updates, list):
                    raise TelegramNotificationError("Telegram returned invalid updates")
                for update in raw_updates:
                    if isinstance(update, dict):
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            self._offset = max(self._offset, update_id + 1)
                        await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except (TelegramNotificationError, httpx.HTTPError, OSError):
                await asyncio.sleep(5)
            except Exception:
                await asyncio.sleep(5)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if self.status_provider is None or self.switch_handler is None:
            return
        message = update.get("message")
        callback = update.get("callback_query")
        if isinstance(message, dict):
            chat = message.get("chat")
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            if str(chat_id) != self.notifier.config.chat_id:
                return
            text = message.get("text")
            if isinstance(text, str):
                await self._handle_command(text)
            return
        if not isinstance(callback, dict):
            return
        callback_message = callback.get("message")
        callback_chat = callback_message.get("chat") if isinstance(callback_message, dict) else None
        chat_id = callback_chat.get("id") if isinstance(callback_chat, dict) else None
        if str(chat_id) != self.notifier.config.chat_id:
            return
        callback_id = callback.get("id")
        data = callback.get("data")
        if isinstance(callback_id, str):
            await self._answer_callback(callback_id)
        if not isinstance(data, str) or not data.startswith("switch:"):
            return
        region_id = data.removeprefix("switch:").strip()
        if not region_id or not region_id.replace("-", "").isalnum():
            return
        await self.notifier.send(await self.switch_handler(region_id))

    async def _handle_command(self, text: str) -> None:
        if self.status_provider is None or self.switch_handler is None:
            return
        command, _, argument = text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await self.notifier.send(
                "可用命令:\n/status 查看出口状态\n/switch <入口ID> 切换指定出口"
            )
        elif command == "/status":
            status_text, keyboard = await self.status_provider()
            await self.notifier.send(status_text, reply_markup={"inline_keyboard": keyboard})
        elif command == "/switch" and argument.strip():
            await self.notifier.send(await self.switch_handler(argument.strip()))

    async def _answer_callback(self, callback_id: str) -> None:
        try:
            await self.notifier._request("answerCallbackQuery", {"callback_query_id": callback_id})
        except (TelegramNotificationError, httpx.HTTPError, OSError):
            return
