"""
sheets_service.py: слой доступа к Google Таблице для ИИ-администратора парикмахерской.

Ожидаемая структура таблицы (3 листа, заголовки в первой строке):

  «Мастера»  ->  Имя | Начало работы | Конец работы          (ID и часы работы необязательны)
  «Услуги»   ->  Название | Длительность (мин) | Цена          (ID необязателен)
  «Записи»   ->  ID | Дата | Время | Окончание | Мастер | Услуга | Клиент | Телефон | Статус | Создана

Названия столбцов можно менять: допустимые варианты заголовков перечислены в *_COLUMNS ниже.
Порядок столбцов в листе не важен, модуль ищет их по заголовкам.

Формат значений: дата «ДД.ММ.ГГГГ», время «ЧЧ:ММ». Даты и время пишутся как текст (RAW),
чтобы Google Таблицы не переводили их в другой формат из-за локали.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

SHEET_MASTERS = "Мастера"
SHEET_SERVICES = "Услуги"
SHEET_BOOKINGS = "Записи"

CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "30"))
DEFAULT_WORK_START = os.getenv("DEFAULT_WORK_START", "10:00")  # если у мастера не заданы часы
DEFAULT_WORK_END = os.getenv("DEFAULT_WORK_END", "20:00")
FALLBACK_DURATION_MIN = 60  # если у записи нет окончания и услуга не найдена

DATE_FMT = "%d.%m.%Y"
TIME_FMT = "%H:%M"

# Статусы записей
STATUS_NEW = "Новая"
STATUS_CONFIRMED = "Подтверждена"
STATUS_CANCELLED = "Отменена"
STATUS_DONE = "Выполнена"
STATUS_NO_SHOW = "Не пришёл"
ALL_STATUSES = (STATUS_NEW, STATUS_CONFIRMED, STATUS_CANCELLED, STATUS_DONE, STATUS_NO_SHOW)
INACTIVE_STATUSES = (STATUS_CANCELLED, STATUS_NO_SHOW)  # такие записи не занимают время

# Допустимые заголовки столбцов: поле -> варианты названия
MASTER_COLUMNS = {
    "id": ("ID", "№"),
    "name": ("Имя", "Мастер", "ФИО"),
    "work_start": ("Начало работы", "Начало", "Начало смены"),
    "work_end": ("Конец работы", "Конец", "Конец смены"),
}
MASTER_REQUIRED = {"name"}

SERVICE_COLUMNS = {
    "id": ("ID", "№"),
    "name": ("Название", "Услуга"),
    "duration": ("Длительность (мин)", "Длительность", "Длительность, мин", "Минуты"),
    "price": ("Цена", "Стоимость"),
}
SERVICE_REQUIRED = {"name", "duration"}

BOOKING_COLUMNS = {
    "id": ("ID", "№"),
    "date": ("Дата",),
    "start": ("Время", "Время начала", "Начало"),
    "end": ("Окончание", "Время окончания", "Конец"),
    "master": ("Мастер",),
    "service": ("Услуга",),
    "client": ("Клиент", "Имя клиента"),
    "phone": ("Телефон", "Тел."),
    "status": ("Статус",),
    "created": ("Создана", "Создано", "Дата создания"),
}
BOOKING_REQUIRED = {"id", "date", "start", "master", "service", "client", "status"}


# --------------------------------------------------------------------------- #
# Исключения
# --------------------------------------------------------------------------- #

class SheetsServiceError(Exception):
    """Базовая ошибка модуля."""


class ConfigError(SheetsServiceError):
    """Не настроен доступ к таблице (SPREADSHEET_ID, credentials.json)."""


class SheetStructureError(SheetsServiceError):
    """Лист или столбец не найден, либо данные в таблице некорректны."""


class NotFoundError(SheetsServiceError):
    """Мастер, услуга или запись не найдены."""


class InvalidStatusError(SheetsServiceError, ValueError):
    """Недопустимый статус записи."""


class SlotUnavailableError(SheetsServiceError):
    """Время занято или вне рабочих часов. В conflict лежит запись, которая мешает (если есть)."""

    def __init__(self, message: str, conflict: "Booking | None" = None):
        super().__init__(message)
        self.conflict = conflict


# --------------------------------------------------------------------------- #
# Модели
# --------------------------------------------------------------------------- #

@dataclass
class Master:
    id: str
    name: str
    work_start: time
    work_end: time


@dataclass
class Service:
    id: str
    name: str
    duration_min: int
    price: float | None


@dataclass
class Slot:
    master: str
    date: date
    start: time
    end: time


@dataclass
class Booking:
    id: str
    date: date
    start: time
    end: time
    master: str
    service: str
    client: str
    phone: str
    status: str
    row: int | None = None  # номер строки в листе «Записи»

    @property
    def is_active(self) -> bool:
        return _status_is_active(self.status)


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #

def _norm(value) -> str:
    return str(value).strip().casefold()


def _status_is_active(status: str) -> bool:
    return _norm(status) not in {_norm(s) for s in INACTIVE_STATUSES}


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _to_time(minutes: int) -> time:
    return time((minutes // 60) % 24, minutes % 60)


def parse_date(value) -> date:
    """Принимает date/datetime или строку «ДД.ММ.ГГГГ», «ГГГГ-ММ-ДД», «ДД/ММ/ГГГГ»."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in (DATE_FMT, "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Не удалось распознать дату: {value!r}")


def parse_time(value) -> time:
    """Принимает time/datetime или строку «ЧЧ:ММ», «ЧЧ:ММ:СС», «ЧЧ.ММ»."""
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    text = str(value).strip()
    for fmt in (TIME_FMT, "%H:%M:%S", "%H.%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Не удалось распознать время: {value!r}")


def _optional_time(value) -> time | None:
    return parse_time(value) if value not in (None, "") else None


def _interval(b: Booking) -> tuple[int, int]:
    start, end = _minutes(b.start), _minutes(b.end)
    if end <= start:  # запись заканчивается в полночь или позже
        end += 24 * 60
    return start, end


def _now() -> datetime:
    return datetime.now(TIMEZONE)


# --------------------------------------------------------------------------- #
# Подключение к Google Таблице
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _spreadsheet() -> gspread.Spreadsheet:
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ConfigError("Не задана переменная окружения SPREADSHEET_ID (см. .env.example).")
    if not os.path.exists(CREDENTIALS_FILE):
        raise ConfigError(f"Файл ключей не найден: {CREDENTIALS_FILE}")
    client = gspread.service_account(filename=CREDENTIALS_FILE)
    return client.open_by_key(spreadsheet_id)


@lru_cache(maxsize=None)
def _worksheet(title: str) -> gspread.Worksheet:
    try:
        return _spreadsheet().worksheet(title)
    except gspread.WorksheetNotFound:
        raise SheetStructureError(f"В таблице нет листа «{title}».") from None


def _resolve_columns(headers, spec, required, sheet_title) -> dict[str, int]:
    """Сопоставляет поля с индексами столбцов по заголовкам (регистр не важен)."""
    positions = {_norm(h): i for i, h in enumerate(headers)}
    cols: dict[str, int] = {}
    for field, aliases in spec.items():
        for alias in aliases:
            if _norm(alias) in positions:
                cols[field] = positions[_norm(alias)]
                break
        else:
            if field in required:
                raise SheetStructureError(
                    f"На листе «{sheet_title}» нет столбца для поля «{field}» "
                    f"(ожидается один из заголовков: {', '.join(aliases)})."
                )
    return cols


def _read_table(sheet_title, spec, required):
    """Возвращает (лист, {поле: индекс столбца}, [(номер строки, {поле: значение})])."""
    ws = _worksheet(sheet_title)
    values = ws.get_all_values()
    if not values:
        raise SheetStructureError(f"Лист «{sheet_title}» пуст: нужна строка заголовков.")
    cols = _resolve_columns(values[0], spec, required, sheet_title)
    rows = []
    for row_num, raw in enumerate(values[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue
        record = {f: (raw[i].strip() if i < len(raw) else "") for f, i in cols.items()}
        rows.append((row_num, record))
    return ws, cols, rows


# --------------------------------------------------------------------------- #
# 1. Мастера и услуги
# --------------------------------------------------------------------------- #

def get_masters() -> list[Master]:
    """Список мастеров. Если часы работы не указаны, берутся DEFAULT_WORK_START/END."""
    _, _, rows = _read_table(SHEET_MASTERS, MASTER_COLUMNS, MASTER_REQUIRED)
    default_start, default_end = parse_time(DEFAULT_WORK_START), parse_time(DEFAULT_WORK_END)
    masters = []
    for row_num, r in rows:
        if not r["name"]:
            continue
        try:
            masters.append(Master(
                id=r.get("id") or r["name"],
                name=r["name"],
                work_start=_optional_time(r.get("work_start")) or default_start,
                work_end=_optional_time(r.get("work_end")) or default_end,
            ))
        except ValueError as exc:
            raise SheetStructureError(f"Лист «{SHEET_MASTERS}», строка {row_num}: {exc}") from None
    return masters


def get_services() -> list[Service]:
    """Список услуг с длительностью в минутах и ценой."""
    _, _, rows = _read_table(SHEET_SERVICES, SERVICE_COLUMNS, SERVICE_REQUIRED)
    services = []
    for row_num, r in rows:
        if not r["name"]:
            continue
        digits = re.search(r"\d+", r["duration"])
        if not digits:
            raise SheetStructureError(
                f"Лист «{SHEET_SERVICES}», строка {row_num}: не задана длительность услуги «{r['name']}»."
            )
        price_text = re.sub(r"[^\d.,]", "", r.get("price", "")).replace(",", ".")
        try:
            price = float(price_text)
            price = int(price) if price.is_integer() else price
        except ValueError:
            price = None
        services.append(Service(
            id=r.get("id") or r["name"],
            name=r["name"],
            duration_min=int(digits.group()),
            price=price,
        ))
    return services


def _pick(items, key, what: str):
    """Находит мастера/услугу по ID или названию (регистр не важен). Объект возвращает как есть."""
    if not isinstance(key, str):
        return key
    k = _norm(key)
    for item in items:
        if _norm(item.id) == k or _norm(item.name) == k:
            return item
    raise NotFoundError(f"{what} не найден(а): {key!r}")


def get_master(key: str) -> Master:
    return _pick(get_masters(), key, "Мастер")


def get_service(key: str) -> Service:
    return _pick(get_services(), key, "Услуга")


# --------------------------------------------------------------------------- #
# Записи: чтение и проверка пересечений
# --------------------------------------------------------------------------- #

def _rows_to_bookings(rows, services: list[Service]) -> list[Booking]:
    durations = {_norm(s.name): s.duration_min for s in services}
    bookings = []
    for row_num, r in rows:
        try:
            day = parse_date(r["date"])
            start = parse_time(r["start"])
            end = _optional_time(r.get("end"))
            if end is None:
                minutes = durations.get(_norm(r["service"]), FALLBACK_DURATION_MIN)
                end = _to_time(_minutes(start) + minutes)
        except ValueError as exc:
            logger.warning("Лист «%s», строка %d пропущена: %s", SHEET_BOOKINGS, row_num, exc)
            continue
        bookings.append(Booking(
            id=r["id"], date=day, start=start, end=end, master=r["master"],
            service=r["service"], client=r["client"], phone=r.get("phone", ""),
            status=r["status"], row=row_num,
        ))
    return bookings


def _find_conflict(bookings, day, master_name, start_min, end_min, ignore_id=None) -> Booking | None:
    for b in bookings:
        if b.date != day or not b.is_active or _norm(b.master) != _norm(master_name):
            continue
        if ignore_id is not None and _norm(b.id) == _norm(ignore_id):
            continue
        b_start, b_end = _interval(b)
        if start_min < b_end and end_min > b_start:
            return b
    return None


def get_bookings(day=None, master: str | None = None, status: str | None = None) -> list[Booking]:
    """Записи с необязательными фильтрами по дате, мастеру и статусу."""
    _, _, rows = _read_table(SHEET_BOOKINGS, BOOKING_COLUMNS, BOOKING_REQUIRED)
    bookings = _rows_to_bookings(rows, get_services())
    if day is not None:
        day = parse_date(day)
        bookings = [b for b in bookings if b.date == day]
    if master is not None:
        bookings = [b for b in bookings if _norm(b.master) == _norm(master)]
    if status is not None:
        bookings = [b for b in bookings if _norm(b.status) == _norm(status)]
    return sorted(bookings, key=lambda b: (b.date, b.start))


def get_booking(booking_id: str) -> Booking:
    for b in get_bookings():
        if _norm(b.id) == _norm(booking_id):
            return b
    raise NotFoundError(f"Запись не найдена: {booking_id!r}")


# --------------------------------------------------------------------------- #
# 2. Поиск свободных окошек
# --------------------------------------------------------------------------- #

def find_free_slots(day, service, master: str | None = None,
                    step_minutes: int | None = None) -> list[Slot]:
    """
    Свободные окошки на дату с учётом длительности услуги.

    day      -- date или строка «ДД.ММ.ГГГГ»
    service  -- название/ID услуги
    master   -- название/ID мастера; если None, ищем у всех мастеров
    Возвращает список Slot, отсортированный по времени, затем по мастеру.
    """
    day = parse_date(day)
    now = _now()
    if day < now.date():
        return []

    services = get_services()
    svc = _pick(services, service, "Услуга")
    masters = get_masters()
    if master is not None:
        masters = [_pick(masters, master, "Мастер")]

    _, _, rows = _read_table(SHEET_BOOKINGS, BOOKING_COLUMNS, BOOKING_REQUIRED)
    bookings = [b for b in _rows_to_bookings(rows, services) if b.date == day]

    step = step_minutes or SLOT_STEP_MINUTES
    now_min = _minutes(now.time())
    slots: list[Slot] = []
    for m in masters:
        t = _minutes(m.work_start)
        last_start = _minutes(m.work_end) - svc.duration_min
        while t <= last_start:
            end = t + svc.duration_min
            in_past = day == now.date() and t <= now_min
            if not in_past and _find_conflict(bookings, day, m.name, t, end) is None:
                slots.append(Slot(master=m.name, date=day, start=_to_time(t), end=_to_time(end)))
            t += step
    return sorted(slots, key=lambda s: (s.start, s.master))


# --------------------------------------------------------------------------- #
# 3. Добавление записи
# --------------------------------------------------------------------------- #

_write_lock = threading.Lock()  # защита от двойной записи внутри одного процесса


def add_booking(day, start, master: str, service: str, client_name: str,
                phone: str = "", status: str = STATUS_CONFIRMED,
                allow_past: bool = False) -> Booking:
    """
    Добавляет запись в лист «Записи».
    Перед записью заново проверяет рабочие часы и пересечения; при конфликте
    бросает SlotUnavailableError (в .conflict лежит мешающая запись).
    """
    day, start_t = parse_date(day), parse_time(start)
    status = _canonical_status(status)

    with _write_lock:
        services = get_services()
        svc = _pick(services, service, "Услуга")
        m = _pick(get_masters(), master, "Мастер")
        ws, cols, rows = _read_table(SHEET_BOOKINGS, BOOKING_COLUMNS, BOOKING_REQUIRED)

        start_min = _minutes(start_t)
        end_min = start_min + svc.duration_min
        end_t = _to_time(end_min)

        if start_min < _minutes(m.work_start) or end_min > _minutes(m.work_end):
            raise SlotUnavailableError(
                f"{m.name} работает {m.work_start:%H:%M}-{m.work_end:%H:%M}, "
                f"услуга «{svc.name}» на {start_t:%H:%M} не помещается в этот график."
            )
        now = _now()
        if not allow_past and (day, start_min) <= (now.date(), _minutes(now.time())):
            raise SlotUnavailableError("Это время уже прошло.")

        bookings = _rows_to_bookings(rows, services)
        if _status_is_active(status):
            conflict = _find_conflict(bookings, day, m.name, start_min, end_min)
            if conflict:
                raise SlotUnavailableError(
                    f"Слот уже занят клиентом {conflict.client} "
                    f"({conflict.start:%H:%M}-{conflict.end:%H:%M}).",
                    conflict=conflict,
                )

        numeric_ids = [int(r["id"]) for _, r in rows if r["id"].isdigit()]
        new_id = str(max(numeric_ids, default=0) + 1)

        values = {
            "id": new_id,
            "date": day.strftime(DATE_FMT),
            "start": start_t.strftime(TIME_FMT),
            "end": end_t.strftime(TIME_FMT),
            "master": m.name,
            "service": svc.name,
            "client": client_name.strip(),
            "phone": phone.strip(),
            "status": status,
            "created": _now().strftime(f"{DATE_FMT} {TIME_FMT}"),
        }
        row = [""] * (max(cols.values()) + 1)
        for field, idx in cols.items():
            row[idx] = values.get(field, "")
        response = ws.append_row(row, value_input_option="RAW", table_range="A1")

        match = re.search(r"![A-Z]+(\d+)", response.get("updates", {}).get("updatedRange", ""))
        return Booking(
            id=new_id, date=day, start=start_t, end=end_t, master=m.name, service=svc.name,
            client=values["client"], phone=values["phone"], status=status,
            row=int(match.group(1)) if match else None,
        )


# --------------------------------------------------------------------------- #
# 4. Изменение статуса и отмена
# --------------------------------------------------------------------------- #

def _canonical_status(status: str) -> str:
    for s in ALL_STATUSES:
        if _norm(s) == _norm(status):
            return s
    raise InvalidStatusError(f"Недопустимый статус {status!r}. Допустимые: {', '.join(ALL_STATUSES)}.")


def update_booking_status(booking_id: str, new_status: str) -> Booking:
    """
    Меняет статус записи. Если отменённая запись возвращается в активные,
    проверяется, не заняли ли это время за это время.
    """
    status = _canonical_status(new_status)

    with _write_lock:
        services = get_services()
        ws, cols, rows = _read_table(SHEET_BOOKINGS, BOOKING_COLUMNS, BOOKING_REQUIRED)
        bookings = _rows_to_bookings(rows, services)
        target = next((b for b in bookings if _norm(b.id) == _norm(booking_id)), None)
        if target is None:
            raise NotFoundError(f"Запись не найдена: {booking_id!r}")

        if _norm(target.status) == _norm(status):
            return target
        if _status_is_active(status) and not target.is_active:
            start_min, end_min = _interval(target)
            conflict = _find_conflict(bookings, target.date, target.master, start_min, end_min,
                                      ignore_id=target.id)
            if conflict:
                raise SlotUnavailableError(
                    f"Нельзя восстановить запись: слот уже занят клиентом {conflict.client}.",
                    conflict=conflict,
                )

        ws.update_cell(target.row, cols["status"] + 1, status)
        target.status = status
        return target


def cancel_booking(booking_id: str) -> Booking:
    """Отменяет запись (статус «Отменена»); слот снова становится свободным."""
    return update_booking_status(booking_id, STATUS_CANCELLED)


# --------------------------------------------------------------------------- #
# Быстрая проверка подключения: python sheets_service.py
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    masters, services = get_masters(), get_services()
    print("Мастера:", [m.name for m in masters])
    print("Услуги:", [(s.name, s.duration_min, s.price) for s in services])
    if services:
        free = find_free_slots(_now().date(), services[0].name)
        print(f"Свободно сегодня на «{services[0].name}»:",
              [f"{s.start:%H:%M} {s.master}" for s in free[:10]])
