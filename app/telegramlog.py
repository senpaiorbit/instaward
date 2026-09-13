"""Telegram notifications + logging handler. Import-safe (lazy Bot import)."""
import logging
import os

log = logging.getLogger("instaward-bot")


def _botlog_enabled(explicit: int | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    try:
        return int(os.getenv("BOTLOG", "1") or "1") != 0
    except ValueError:
        return True


def _creds() -> tuple[str, str]:
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


async def send_message(text: str, botlog: int | None = None) -> bool:
    """Send a Telegram message if BOTLOG=1 and credentials exist. Never raises."""
    if not _botlog_enabled(botlog):
        return False
    token, chat_id = _creds()
    if not token or not chat_id:
        return False
    try:
        from telegram import Bot

        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text[:4000])
        return True
    except Exception as exc:  # noqa: BLE001 - notify path must not crash caller
        log.warning("telegram send failed: %s", exc)
        return False


async def notify_checkpoint(username: str) -> None:
    await send_message(f"Instagram checkpoint for @{username}: login needs manual verification code.")


async def notify_error(context: str, err: Exception) -> None:
    await send_message(f"{context}: {type(err).__name__}: {err}")


async def send_daily_summary(stats: dict) -> None:
    await send_message(f"daily summary: {stats}")


class TelegramLogHandler(logging.Handler):
    """logging.Handler that forwards ERROR+ records to Telegram (fire-and-forget)."""

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        if not _botlog_enabled():
            return
        token, chat_id = _creds()
        if not token or not chat_id:
            return
        try:
            msg = self.format(record)[:4000]
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(send_message(msg))
        except Exception:  # noqa: BLE001
            pass
