"""Пример, как подключить новые модули к вашему main.py (скопируйте нужные части, файл целиком не заменяйте)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from booking_service import create_booking
from .config import settings
from .confirmations import handle_client_reply
from .reminders import scheduler, start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()                 # фоновые напоминания стартуют вместе с приложением
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


@app.get("/health")                   # для проверки хостинга и внешнего пинга
async def health():
    return {"status": "ok"}


@app.post("/webhook")                 # адрес вебхука Green-API
async def webhook(request: Request):
    data = await request.json()
    if data.get("typeWebhook") != "incomingMessageReceived":
        return {"ok": True}

    sender = data["senderData"]["sender"].split("@")[0]   # "77001234567"
    md = data.get("messageData", {})
    text = md.get("textMessageData", {}).get("textMessage")

    # 1) Ответы клиентов «Да» / «Отменяю» обрабатываем сразу, без ИИ
    if text and sender != settings.admin_phone and await handle_client_reply(sender, text):
        return {"ok": True}

    # 2) ... дальше ваш прежний поток: голос → Whisper → ИИ-ядро ...
    return {"ok": True}


# --- Как подключить защиту от конфликтов к вашему tool create_booking в ИИ-ядре ---
#
# async def tool_create_booking(args: dict, is_admin: bool) -> dict:
#     return await create_booking(
#         date=args["date"], time=args["time"], master=args["master"],
#         client_name=args["client_name"], phone=args["phone"],
#         service=args.get("service", ""),
#         source="admin" if is_admin else "client",
#     )
#
# И добавьте в системный промпт:
#   "Если результат инструмента содержит поле message — передай его собеседнику ДОСЛОВНО,
#    ничего не добавляя и не перефразируя."
