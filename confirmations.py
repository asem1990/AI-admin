"""Обработка ответов клиента на напоминание: «Да» → Подтверждено, «Отменяю» → слот свободен.

Вызывается ДО передачи сообщения в ИИ: это дешевле и надёжнее, чем спрашивать модель.
Если сообщение не похоже на ответ или у клиента нет напомненной записи рядом по времени —
возвращает False, и сообщение идёт в обычный ИИ-поток.
"""
import re

from .booking_service import cancel_booking
from .config import settings
from .integrations import gas, notify_admin, send_whatsapp

CONFIRM_WORDS = {"да", "yes", "ок", "окей", "ok", "подтверждаю", "+", "конечно", "буду", "приду"}
CANCEL_WORDS = {"отменяю", "отмена", "отменить", "нет", "не приду", "не смогу"}


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s+]", "", text.lower()).strip()


def classify(text: str) -> str | None:
    t = _norm(text)
    if t in CONFIRM_WORDS:
        return "confirm"
    if t in CANCEL_WORDS or t.startswith("отмен"):
        return "cancel"
    return None


async def handle_client_reply(phone: str, text: str) -> bool:
    """True — сообщение обработано здесь, дальше в ИИ отправлять не нужно."""
    intent = classify(text)
    if intent is None:
        return False

    res = await gas(
        "find_booking_by_phone",
        phone=phone,
        within_min=settings.reminder_minutes + settings.reminder_window,
    )
    b = res.get("booking")
    if not b:
        return False  # нет напомненной записи рядом — пусть разбирается ИИ

    if intent == "confirm":
        if b["status"] == "Подтверждено":
            await send_whatsapp(phone, "Ваш визит уже подтверждён. Ждём вас! 💈")
            return True
        r = await gas("confirm_booking", id=b["id"])
        if r.get("ok"):
            await send_whatsapp(phone, f"Спасибо, {b['name']}! Визит подтверждён. Ждём вас в {b['time']} 💈")
        else:
            await send_whatsapp(phone, "Не получилось подтвердить запись, администратор свяжется с вами.")
            await notify_admin(f"⚠️ Не удалось подтвердить запись {b['name']} на {b['time']} ({r.get('error')}).")
        return True

    # cancel
    r = await cancel_booking(id=b["id"])
    if r["ok"]:
        await send_whatsapp(phone, "Запись отменена. Будем рады видеть вас снова — напишите, когда захотите записаться.")
        await notify_admin(
            f"❌ Клиент {b['name']} ({b['phone']}) отменил запись: {b['date']} в {b['time']} "
            f"к мастеру {b['master']}. Слот освобождён."
        )
    else:
        await send_whatsapp(phone, "Не получилось отменить запись, администратор свяжется с вами.")
        await notify_admin(f"⚠️ Клиент {b['name']} хотел отменить запись на {b['time']}, но отмена не прошла ({r.get('error')}).")
    return True
