"""Optional Telegram messages for trades, risk events and errors.

Create a bot with @BotFather, send it any message, then put ``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID`` in ``.env``. Without them, messages only go to the log.
"""

from __future__ import annotations

import logging

import requests

from forex_bot.config import TelegramConfig

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4000


class Notifier:
    def send(self, text: str) -> None:
        log.info("notify: %s", text)


class TelegramNotifier(Notifier):
    def __init__(
        self,
        token: str,
        chat_id: str,
        prefix: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.prefix = prefix
        self.session = session or requests.Session()

    def send(self, text: str) -> None:
        super().send(text)
        message = f"{self.prefix}{text}"[:TELEGRAM_LIMIT]
        try:
            response = self.session.post(
                self.url,
                data={"chat_id": self.chat_id, "text": message, "disable_web_page_preview": True},
                timeout=10,
            )
            if response.status_code != 200:
                log.warning(
                    "Telegram returned HTTP %s: %s", response.status_code, response.text[:200]
                )
        except requests.RequestException as exc:
            # A notification failure must never stop trading.
            log.warning("could not send Telegram message: %s", exc)


def make_notifier(config: TelegramConfig, prefix: str = "") -> Notifier:
    if config.enabled:
        return TelegramNotifier(config.token, config.chat_id, prefix)
    return Notifier()
