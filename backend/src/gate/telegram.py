from __future__ import annotations

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

    async def send(self, message: str) -> bool:
        if not self.config.enabled:
            return False
        url = f"{self.config.api_base_url.rstrip('/')}/bot{self.config.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds),
                trust_env=False,
            ) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self.config.chat_id,
                        "text": message,
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, OSError) as exc:
            raise TelegramNotificationError("Telegram notification request failed") from exc
        except ValueError as exc:
            raise TelegramNotificationError("Telegram notification returned invalid data") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramNotificationError("Telegram API rejected the notification")
        return True
