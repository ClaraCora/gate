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
RegionNameProvider = Callable[[str], Awaitable[str | None]]


def _operator_keyboard() -> dict[str, Any]:
    """Persistent shortcuts shown below the Telegram compose box."""
    return {
        "keyboard": [
            [{"text": "📊 查看状态"}, {"text": "🔀 切换出口"}],
            [{"text": "💡 使用帮助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "选择操作或输入命令",
    }


class TelegramBot:
    """Chat-scoped long-polling operator bot."""

    def __init__(self, notifier: TelegramNotifier) -> None:
        self.notifier = notifier
        self.status_provider: StatusProvider | None = None
        self.switch_handler: SwitchHandler | None = None
        self.region_name_provider: RegionNameProvider | None = None
        self._offset = 0
        self._commands_configured = False

    def set_handlers(
        self,
        *,
        status_provider: StatusProvider,
        switch_handler: SwitchHandler,
        region_name_provider: RegionNameProvider | None = None,
    ) -> None:
        self.status_provider = status_provider
        self.switch_handler = switch_handler
        self.region_name_provider = region_name_provider

    def reset_offset(self) -> None:
        self._offset = 0
        self._commands_configured = False

    async def _ensure_commands(self) -> None:
        if self._commands_configured:
            return
        await self.notifier._request(
            "setMyCommands",
            {
                "commands": [
                    {"command": "status", "description": "查看出口状态"},
                    {"command": "switch", "description": "确认后切换出口"},
                    {"command": "help", "description": "打开操作菜单"},
                ]
            },
        )
        self._commands_configured = True

    async def run_forever(self) -> None:
        while True:
            if not self.notifier.config.enabled or self.status_provider is None:
                await asyncio.sleep(5)
                continue
            try:
                await self._ensure_commands()
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
        if not isinstance(data, str):
            return
        if data == "status:refresh":
            status_text, keyboard = await self.status_provider()
            await self.notifier.send(status_text, reply_markup={"inline_keyboard": keyboard})
            return
        if data == "menu:home":
            await self._send_help()
            return
        if data == "switch:cancel":
            await self.notifier.send("已取消切换。", reply_markup=_operator_keyboard())
            return
        if data.startswith("switch:confirm:"):
            region_id = data.removeprefix("switch:confirm:").strip()
            if self._valid_region_id(region_id):
                await self.notifier.send(
                    await self.switch_handler(region_id),
                    reply_markup=_operator_keyboard(),
                )
            return
        if not data.startswith("switch:"):
            return
        region_id = data.removeprefix("switch:").strip()
        if not self._valid_region_id(region_id):
            return
        await self._send_switch_confirmation(region_id)

    @staticmethod
    def _valid_region_id(region_id: str) -> bool:
        return bool(region_id and region_id.replace("-", "").isalnum())

    async def _region_name(self, region_id: str) -> str | None:
        if self.region_name_provider is None:
            return region_id
        return await self.region_name_provider(region_id)

    async def _send_switch_confirmation(self, region_id: str) -> None:
        region_name = await self._region_name(region_id)
        if region_name is None:
            await self.notifier.send(
                f"入口 {region_id} 不存在。",
                reply_markup=_operator_keyboard(),
            )
            return
        await self.notifier.send(
            f"确认切换到「{region_name}」?\n\n系统将按排除机制随机尝试最多 5 个候选出口。",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "✅ 确认切换", "callback_data": f"switch:confirm:{region_id}"},
                        {"text": "取消", "callback_data": "switch:cancel"},
                    ],
                    [{"text": "返回菜单", "callback_data": "menu:home"}],
                ]
            },
        )

    async def _send_help(self) -> None:
        await self.notifier.send(
            "🛠 Gate 出口助手\n\n"
            "📊 查看状态: 查看最近 2 小时成功率和实际出口 IP\n"
            "🔀 切换出口: 选择入口并确认后提交切换\n\n"
            "命令:\n/status 查看状态\n/switch <入口ID> 请求切换\n/help 查看帮助",
            reply_markup=_operator_keyboard(),
        )

    async def _handle_command(self, text: str) -> None:
        if self.status_provider is None or self.switch_handler is None:
            return
        command, _, argument = text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/help"} or text.strip() in {"💡 使用帮助", "帮助"}:
            await self._send_help()
        elif command == "/status" or text.strip() in {"📊 查看状态", "状态"}:
            status_text, keyboard = await self.status_provider()
            await self.notifier.send(status_text, reply_markup={"inline_keyboard": keyboard})
        elif command == "/switch" and argument.strip():
            await self._send_switch_confirmation(argument.strip())
        elif command == "/switch" or text.strip() in {"🔀 切换出口", "切换出口"}:
            status_text, keyboard = await self.status_provider()
            del status_text
            await self.notifier.send(
                "请选择要切换的入口:",
                reply_markup={"inline_keyboard": keyboard},
            )

    async def _answer_callback(self, callback_id: str) -> None:
        try:
            await self.notifier._request("answerCallbackQuery", {"callback_query_id": callback_id})
        except (TelegramNotificationError, httpx.HTTPError, OSError):
            return
