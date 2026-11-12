"""Тонкие обёртки над Google Apps Script и Green-API.

Если у вас в проекте уже есть функция отправки сообщений и клиент для Apps Script —
оставьте свои и просто подставьте их вместо gas() и send_whatsapp() в остальных модулях.
"""
import re

import httpx

from config import settings

# follow_redirects обязателен: Apps Script отвечает на POST редиректом 302.
_http = httpx.AsyncClient(timeout=30, follow_redirects=True)


async def gas(action: str, **payload) -> dict:
    """Вызов действия Apps Script. Возвращает JSON-ответ как dict."""
    body = {"action": action, "token": settings.gas_token, **payload}
    r = await _http.post(settings.gas_url, json=body)
    r.raise_for_status()
    return r.json()


def to_chat_id(phone: str) -> str:
    return re.sub(r"\D", "", phone) + "@c.us"


async def send_whatsapp(phone: str, text: str) -> None:
    """Отправка текстового сообщения через Green-API. Бросает исключение при ошибке."""
    url = f"{settings.green_url}/waInstance{settings.green_id}/sendMessage/{settings.green_token}"
    r = await _http.post(url, json={"chatId": to_chat_id(phone), "message": text})
    r.raise_for_status()


async def notify_admin(text: str) -> None:
    if not settings.admin_phone:
        return
    try:
        await send_whatsapp(settings.admin_phone, text)
    except Exception:  # уведомление админу не должно ломать основной поток
        import logging
        logging.getLogger("notify").exception("Не удалось отправить сообщение администратору")
