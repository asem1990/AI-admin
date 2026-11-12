"""Фоновая задача: каждые 5 минут находит записи, до которых ~1 час, и шлёт напоминание.

Запускается внутри FastAPI-приложения (APScheduler). Работает только в ОДНОМ процессе —
не запускайте uvicorn с несколькими воркерами. Даже при дублях повторной отправки не будет:
перед отправкой запись «захватывается» атомарной отметкой в таблице (set_reminded).
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from integrations import gas, send_whatsapp

log = logging.getLogger("reminders")

REMINDER_TEXT = (
    "Здравствуйте, {name}! Напоминаем о вашей записи сегодня в {time} к мастеру {master}. "
    "Подтвердите, пожалуйста, ваш визит: ответьте 'Да' или 'Отменяю'"
)

scheduler = AsyncIOScheduler(timezone=settings.tz)
_run_lock = asyncio.Lock()


async def send_due_reminders() -> None:
    if _run_lock.locked():  # предыдущий запуск ещё идёт
        return
    async with _run_lock:
        # Окно 55–65 минут: запуск раз в 5 минут гарантированно попадает в него хотя бы раз.
        lo = settings.reminder_minutes - settings.reminder_window
        hi = settings.reminder_minutes + settings.reminder_window
        try:
            res = await gas("get_upcoming_reminders", from_min=lo, to_min=hi)
        except Exception:
            log.exception("Не удалось получить записи из таблицы")
            return

        for b in res.get("bookings", []):
            try:
                # 1) атомарно «занимаем» напоминание; если уже занято — пропускаем
                claim = await gas("set_reminded", id=b["id"], value=True)
                if not claim.get("changed"):
                    continue
                # 2) отправляем; при ошибке снимаем отметку, чтобы повторить на следующем тике
                try:
                    text = REMINDER_TEXT.format(name=b["name"], time=b["time"], master=b["master"])
                    await send_whatsapp(b["phone"], text)
                    log.info("Напоминание отправлено: %s %s %s", b["name"], b["date"], b["time"])
                except Exception:
                    log.exception("Ошибка отправки, откатываю отметку: %s", b["id"])
                    await gas("set_reminded", id=b["id"], value=False)
            except Exception:
                log.exception("Ошибка обработки записи %s", b.get("id"))


def start_scheduler() -> None:
    scheduler.add_job(
        send_due_reminders,
        "interval",
        minutes=settings.reminder_every_minutes,
        id="reminders",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=datetime.now(ZoneInfo(settings.tz)),  # первый запуск сразу при старте
    )
    scheduler.start()
