"""
Бэкенд ИИ-администратора парикмахерской.

Поток обработки:
  Green-API webhook -> parse_webhook -> определить роль (admin/client)
  -> если голосовое: скачать + Whisper -> текст
  -> admin: разобрать команду (LLM -> JSON) -> выполнить -> ответить
  -> client: handle_client_message (заглушка)

Запуск:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import json
import logging
import os
import re
import secrets
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("barber-bot")

# ───────────────────────── Конфигурация (переменные окружения) ─────────────────────────

ADMIN_PHONE = os.environ["ADMIN_PHONE"]                       # например 77011234567
GREEN_API_URL = os.getenv("GREEN_API_URL", "https://api.green-api.com").rstrip("/")
GREEN_ID_INSTANCE = os.environ["GREEN_API_ID_INSTANCE"]
GREEN_API_TOKEN = os.environ["GREEN_API_TOKEN_INSTANCE"]
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")                    # необязательно, но желательно
SCHEDULE_API_URL = os.getenv("SCHEDULE_API_URL")              # куда отправлять команды (Apps Script и т.п.)
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))     # нужен, чтобы понимать «завтра»
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "ru")        # подсказка языка для Whisper

openai_client = AsyncOpenAI()                                 # ключ берётся из OPENAI_API_KEY
http: httpx.AsyncClient                                       # создаётся в lifespan


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http
    http = httpx.AsyncClient(timeout=60, follow_redirects=True)
    yield
    await http.aclose()


app = FastAPI(title="Barbershop AI admin", lifespan=lifespan)

# ───────────────────────── Утилиты ─────────────────────────


def normalize_phone(raw: str) -> str:
    """'+7 (701) 123-45-67', '87011234567', '77011234567@c.us' -> '77011234567'."""
    digits = re.sub(r"\D", "", raw.split("@")[0])
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


ADMIN_PHONE_NORM = normalize_phone(ADMIN_PHONE)

# Green-API может прислать один вебхук повторно — отсекаем дубли по idMessage.
_seen_ids: "OrderedDict[str, None]" = OrderedDict()


def already_processed(message_id: str) -> bool:
    if message_id in _seen_ids:
        return True
    _seen_ids[message_id] = None
    if len(_seen_ids) > 5000:
        _seen_ids.popitem(last=False)
    return False


# ───────────────────────── Разбор вебхука ─────────────────────────


@dataclass
class IncomingMessage:
    id: str
    chat_id: str
    phone: str
    sender_name: str
    role: Literal["admin", "client"]
    kind: Literal["text", "voice", "other"]
    text: str | None = None
    audio_url: str | None = None


def parse_webhook(payload: dict) -> IncomingMessage | None:
    """Возвращает IncomingMessage или None, если вебхук нам не интересен."""
    if payload.get("typeWebhook") != "incomingMessageReceived":
        return None  # статусы доставки, исходящие и т.д.

    sender = payload.get("senderData") or {}
    chat_id = sender.get("chatId", "")
    if not chat_id.endswith("@c.us"):
        return None  # игнорируем группы

    phone = normalize_phone(chat_id)
    role = "admin" if phone == ADMIN_PHONE_NORM else "client"

    data = payload.get("messageData") or {}
    msg_type = data.get("typeMessage")

    text = audio_url = None
    kind: Literal["text", "voice", "other"] = "other"

    if msg_type == "textMessage":
        kind, text = "text", (data.get("textMessageData") or {}).get("textMessage")
    elif msg_type == "extendedTextMessage":
        kind, text = "text", (data.get("extendedTextMessageData") or {}).get("text")
    elif msg_type == "audioMessage":
        kind, audio_url = "voice", (data.get("fileMessageData") or {}).get("downloadUrl")

    return IncomingMessage(
        id=payload.get("idMessage", ""),
        chat_id=chat_id,
        phone=phone,
        sender_name=sender.get("senderName", ""),
        role=role,
        kind=kind,
        text=text,
        audio_url=audio_url,
    )


# ───────────────────────── WhatsApp: отправка ─────────────────────────


async def send_whatsapp_message(phone: str, text: str) -> None:
    """Отправить текст на номер (в любом формате: +7..., 8..., 7...)."""
    url = f"{GREEN_API_URL}/waInstance{GREEN_ID_INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
    body = {"chatId": f"{normalize_phone(phone)}@c.us", "message": text}
    resp = await http.post(url, json=body)
    resp.raise_for_status()


# ───────────────────────── Голос -> текст (Whisper) ─────────────────────────


async def download_file(url: str) -> bytes:
    resp = await http.get(url)
    resp.raise_for_status()
    return resp.content


async def transcribe_audio(audio: bytes) -> str:
    # Голосовые WhatsApp — это ogg/opus, Whisper принимает их напрямую.
    result = await openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=("voice.ogg", audio, "audio/ogg"),
        language=WHISPER_LANGUAGE,
    )
    return result.text.strip()


# ───────────────────────── Команды администратора ─────────────────────────

# action -> обязательные параметры
REQUIRED_PARAMS: dict[str, list[str]] = {
    "book_slot": ["client_name", "master", "date", "time"],
    "cancel_slot": ["client_name", "date"],
    "update_master_schedule": ["master", "date", "status"],
    "get_schedule": ["date"],
}

PARAM_LABELS = {
    "client_name": "имя клиента", "master": "мастер", "date": "дата",
    "time": "время", "status": "статус смены",
}


async def parse_admin_command(text: str) -> dict:
    """Превращает свободную фразу админа в {"action": ..., "params": {...}}."""
    now = datetime.now(TIMEZONE)
    system_prompt = f"""Ты разбираешь команды администратора парикмахерской.
Сейчас {now:%Y-%m-%d %H:%M}, день недели: {now:%A}, часовой пояс {TIMEZONE.key}.
Верни ТОЛЬКО JSON вида {{"action": "...", "params": {{...}}}}.

Допустимые action:
- "book_slot": client_name, client_phone (или null), master, date (YYYY-MM-DD), time (HH:MM)
- "cancel_slot": client_name, master (или null), date (YYYY-MM-DD), time (HH:MM или null)
- "update_master_schedule": master, date (YYYY-MM-DD), status ("day_off" | "working"), from (HH:MM или null), to (HH:MM или null)
- "get_schedule": date (YYYY-MM-DD), master (или null)
- "unknown": если это не команда

Правила: относительные даты («завтра», «в пятницу») переводи в конкретные даты.
Имена мастеров ставь в именительном падеже («у Ольги» -> «Ольга», «к Максиму» -> «Максим»).
Номер телефона оставь как сказан. Чего нет в сообщении — ставь null, не выдумывай.
Фраза «смена отменяется» = update_master_schedule со status "day_off"."""

    resp = await openai_client.chat.completions.create(
        model=LLM_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    )
    return json.loads(resp.choices[0].message.content)


async def call_schedule_backend(action: str, params: dict) -> dict:
    """Отправляет действие в хранилище расписания (Google Apps Script, БД и т.д.)."""
    if not SCHEDULE_API_URL:
        log.warning("SCHEDULE_API_URL не задан, действие только залогировано: %s %s", action, params)
        return {"ok": True, "stub": True}
    resp = await http.post(SCHEDULE_API_URL, json={"action": action, **params})
    resp.raise_for_status()
    return resp.json()


def describe_action(action: str, p: dict) -> str:
    if action == "book_slot":
        return f"Записал: {p['client_name']} к мастеру {p['master']}, {p['date']} в {p['time']}"
    if action == "cancel_slot":
        return f"Отменил запись: {p['client_name']}, {p['date']}"
    if action == "update_master_schedule":
        what = "выходной" if p["status"] == "day_off" else "работает"
        return f"График обновлён: {p['master']}, {p['date']} — {what}"
    return f"Запросил расписание на {p['date']}"


async def handle_admin_message(msg: IncomingMessage, text: str) -> None:
    command = await parse_admin_command(text)
    action = command.get("action", "unknown")
    params = command.get("params") or {}

    if action not in REQUIRED_PARAMS:
        await send_whatsapp_message(msg.phone, "Не понял команду. Пример: «Запиши Алексея на завтра в 14:00 к Максиму».")
        return

    missing = [k for k in REQUIRED_PARAMS[action] if not params.get(k)]
    if missing:
        names = ", ".join(PARAM_LABELS.get(k, k) for k in missing)
        await send_whatsapp_message(msg.phone, f"Не хватает данных: {names}. Повторите команду целиком.")
        return

    result = await call_schedule_backend(action, params)
    if result.get("ok", True):
        await send_whatsapp_message(msg.phone, "✅ " + describe_action(action, params))
    else:
        await send_whatsapp_message(msg.phone, f"⚠️ Не получилось: {result.get('error', 'неизвестная ошибка')}")


# ───────────────────────── Сообщения клиентов ─────────────────────────


async def handle_client_message(msg: IncomingMessage, text: str) -> None:
    """Заглушка. Здесь будет диалог с клиентом: запись, свободные окна, цены."""
    log.info("Клиент %s (%s): %s", msg.phone, msg.sender_name, text)
    await send_whatsapp_message(msg.phone, "Здравствуйте! Сообщение получил, сейчас помогу с записью.")


# ───────────────────────── Главный обработчик ─────────────────────────


async def process_message(msg: IncomingMessage) -> None:
    try:
        text = msg.text

        if msg.kind == "voice":
            if not msg.audio_url:
                return
            audio = await download_file(msg.audio_url)
            text = await transcribe_audio(audio)
            log.info("Голос от %s (%s) -> %r", msg.phone, msg.role, text)
            if msg.role == "admin":
                # Показываем админу, как мы его поняли — Whisper иногда ошибается в именах.
                await send_whatsapp_message(msg.phone, f"🎙 Расшифровка: «{text}»")

        if not text:
            return  # картинки, стикеры и т.п. пока игнорируем

        if msg.role == "admin":
            await handle_admin_message(msg, text)
        else:
            await handle_client_message(msg, text)

    except Exception:
        log.exception("Ошибка при обработке сообщения %s", msg.id)
        if msg.role == "admin":
            try:
                await send_whatsapp_message(msg.phone, "⚠️ Внутренняя ошибка, команда не выполнена.")
            except Exception:
                log.exception("Не удалось отправить сообщение об ошибке")


# ───────────────────────── Эндпоинты ─────────────────────────


@app.post("/webhook")
async def webhook(
    payload: dict,
    background: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    # Green-API присылает токен из настроек (webhookUrlToken) в заголовке Authorization.
    if WEBHOOK_TOKEN and not secrets.compare_digest(authorization or "", f"Bearer {WEBHOOK_TOKEN}"):
        raise HTTPException(status_code=401, detail="bad token")

    msg = parse_webhook(payload)
    if msg and not already_processed(msg.id):
        # Тяжёлую работу (скачивание, Whisper, LLM) выносим в фон,
        # чтобы быстро вернуть 200 и Green-API не слал повторы.
        background.add_task(process_message, msg)

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
