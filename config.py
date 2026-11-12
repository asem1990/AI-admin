import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Google Apps Script Web App (URL вида https://script.google.com/macros/s/.../exec)
    gas_url: str = os.getenv("SHEET_URL", "")
    gas_token: str = os.getenv("SHEET_TOKEN", "")  # должен совпадать с SECRET в Apps Script (если задан)

    # Green-API
    green_url: str = os.getenv("GREEN_API_URL", "https://api.green-api.com")
    green_id: str = os.getenv("GREEN_API_ID_INSTANCE", "")
    green_token: str = os.getenv("GREEN_API_TOKEN_INSTANCE", "")

    # Телефон администратора, цифры без "+": 77001234567
    admin_phone: str = os.getenv("ADMIN_PHONE", "")

    # Часовой пояс салона (на хостинге сервер живёт в UTC, поэтому задаём явно)
    tz: str = os.getenv("TZ", "Asia/Almaty")

    # За сколько минут до записи напоминать и допуск окна (±)
    reminder_minutes: int = int(os.getenv("REMINDER_MINUTES", "60"))
    reminder_window: int = int(os.getenv("REMINDER_WINDOW", "5"))
    reminder_every_minutes: int = int(os.getenv("REMINDER_EVERY_MINUTES", "5"))


settings = Settings()
