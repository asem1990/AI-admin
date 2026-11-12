"""Создание и отмена записей с защитой от конфликтов.

Защита работает на стороне Apps Script: проверка «слот свободен?» и запись строки
выполняются под LockService одним блоком. Поэтому, даже если клиент в WhatsApp и
администратор «по телефону» нажмут запись в одну и ту же секунду, второй получит slot_taken.
"""
from typing import Literal

from .integrations import gas

Source = Literal["client", "admin"]


async def create_booking(
    *,
    date: str,          # "2026-11-13"
    time: str,          # "15:00"
    master: str,
    client_name: str,
    phone: str,
    service: str = "",
    source: Source = "client",
) -> dict:
    """Возвращает dict. Поле message можно отправлять собеседнику как есть."""
    res = await gas(
        "book_slot",
        date=date, time=time, master=master,
        client_name=client_name, phone=phone, service=service,
    )

    if res.get("ok"):
        return {"ok": True, "id": res["id"]}

    if res.get("error") == "slot_taken":
        if source == "admin":
            who = res.get("occupied_by") or "другим клиентом"
            return {
                "ok": False,
                "error": "slot_taken",
                "message": f"⚠️ Слот уже занят клиентом {who}!",
            }
        # Клиенту имя другого человека не раскрываем
        return {
            "ok": False,
            "error": "slot_taken",
            "message": "К сожалению, это время уже занято. Подберём другое?",
        }

    return {
        "ok": False,
        "error": res.get("error", "unknown"),
        "message": "Не удалось создать запись, попробуйте ещё раз.",
    }


async def cancel_booking(*, id: str | None = None, date: str | None = None,
                         time: str | None = None, master: str | None = None) -> dict:
    """Отмена по id или по (date, time, master). Слот освобождается (статус «Отменено»)."""
    payload = {"id": id} if id else {"date": date, "time": time, "master": master}
    res = await gas("cancel_slot", **payload)
    if res.get("ok"):
        return {"ok": True, "booking": res["booking"]}
    return {"ok": False, "error": res.get("error", "unknown")}
