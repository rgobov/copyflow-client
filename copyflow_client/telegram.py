from __future__ import annotations

import logging

from copyflow_client.config import ClientSettings

log = logging.getLogger(__name__)


class ClientNotifier:
    """Telegram notifications + inline confirm buttons. Degrades to logs."""

    def __init__(self, settings: ClientSettings):
        self._settings = settings
        self._bot = None
        self._on_callback = None
        if settings.telegram_bot_token and settings.telegram_chat_id:
            try:
                from aiogram import Bot

                self._bot = Bot(token=settings.telegram_bot_token)
            except Exception:
                log.exception("client telegram init failed")

    def set_callback_handler(self, handler) -> None:
        self._on_callback = handler

    async def notify(self, text: str) -> None:
        if not self._bot:
            log.info("client notify: %s", text)
            return
        try:
            await self._bot.send_message(self._settings.telegram_chat_id, text)
        except Exception:
            log.exception("client telegram send failed")

    async def ask(self, text: str, signal_id: str, buttons: list[tuple[str, str]]) -> None:
        """buttons: [(label, callback_data)]. Without telegram -> log only."""
        if not self._bot:
            log.info("client ask (no tg): %s | %s", text, buttons)
            return
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        kb = InlineKeyboardBuilder()
        for label, data in buttons:
            kb.button(text=label, callback_data=data)
        kb.adjust(2)
        try:
            await self._bot.send_message(
                self._settings.telegram_chat_id, text,
                reply_markup=kb.as_markup(),
            )
        except Exception:
            log.exception("client telegram ask failed")

    async def run_polling(self, on_callback) -> None:
        """Start aiogram polling; routes cf:exec/<cf:skip> callbacks to handler."""
        if not self._bot:
            return
        from aiogram import Dispatcher, F
        from aiogram.types import CallbackQuery

        self.set_callback_handler(on_callback)
        dp = Dispatcher()

        @dp.callback_query(F.data.startswith("cf:"))
        async def cb(query: CallbackQuery):
            _, action, signal_id = query.data.split(":", 2)
            if self._on_callback:
                await self._on_callback(action, signal_id)
            await query.answer("done")

        try:
            await dp.start_polling(self._bot)
        except Exception:
            log.exception("client telegram polling stopped")
