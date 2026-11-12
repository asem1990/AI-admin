/**
 * Дополнения к Google Apps Script (Web App) для листа «Записи».
 *
 * ОЖИДАЕМЫЕ КОЛОНКИ листа «Записи» (строка 1 — заголовки):
 *   A ID | B Дата (yyyy-MM-dd) | C Время (HH:mm) | D Мастер | E Имя |
 *   F Телефон | G Услуга | H Статус | I Напоминание | J Создано
 *
 * ВАЖНО: колонки B, C и F отформатируйте как «Обычный текст»
 * (Формат → Числа → Обычный текст), иначе Google превратит даты и время в объекты Date.
 *
 * Статусы: «Записан», «Подтверждено», «Отменено».
 * Отменённая запись остаётся в таблице (для истории и вечернего отчёта),
 * но слот считается свободным.
 *
 * Как встроить: добавьте эти функции в ваш проект, а в вашем doPost добавьте
 * case'ы из функции doPost ниже. Если у вас уже есть book_slot / cancel_slot —
 * ЗАМЕНИТЕ их версиями отсюда: именно они защищены блокировкой (LockService).
 *
 * После изменений: Развернуть → Управление развертываниями → ✏️ → Версия: «Новая версия».
 * Без этого Web App продолжит работать на СТАРОМ коде.
 */

const TZ = 'Asia/Almaty';
const SHEET_NAME = 'Записи';
const SECRET = ''; // необязательно: впишите строку и задайте такую же в GAS_TOKEN на сервере
const COL = { ID: 0, DATE: 1, TIME: 2, MASTER: 3, NAME: 4, PHONE: 5, SERVICE: 6, STATUS: 7, REMINDED: 8, CREATED: 9 };
const ST = { BOOKED: 'Записан', CONFIRMED: 'Подтверждено', CANCELED: 'Отменено' };
const ACTIVE = [ST.BOOKED, ST.CONFIRMED];
const REMINDED_MARK = 'да';

// ---------- Роутер (объедините со своим doPost) ----------
function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    if (SECRET && req.token !== SECRET) return out_({ ok: false, error: 'unauthorized' });

    switch (req.action) {
      case 'book_slot':               return out_(withLock_(() => bookSlot_(req)));
      case 'cancel_slot':             return out_(withLock_(() => cancelSlot_(req)));
      case 'confirm_booking':         return out_(withLock_(() => confirmBooking_(req)));
      case 'set_reminded':            return out_(withLock_(() => setReminded_(req)));
      case 'get_upcoming_reminders':  return out_(getUpcomingReminders_(req));
      case 'find_booking_by_phone':   return out_(findBookingByPhone_(req));
      // ... ваши остальные action: check_slot, get_schedule, update_master_schedule
    }
    return out_({ ok: false, error: 'unknown_action' });
  } catch (err) {
    return out_({ ok: false, error: String(err) });
  }
}

// ---------- Утилиты ----------
function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Блокировка: пока один запрос «проверяет и пишет», второй ждёт. Это и убирает race condition.
function withLock_(fn) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000); // бросит исключение, если не дождались 20 сек
  try {
    return fn();
  } finally {
    SpreadsheetApp.flush(); // гарантируем, что запись видна следующему запросу
    lock.releaseLock();
  }
}

// Если скрипт НЕ привязан к таблице, замените getActive() на SpreadsheetApp.openById('ID_ТАБЛИЦЫ')
function sheet_() {
  return SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
}

function readAll_() {
  const sh = sheet_();
  const n = sh.getLastRow() - 1;
  const rows = n > 0 ? sh.getRange(2, 1, n, 10).getDisplayValues() : [];
  return { sh, rows };
}

function digits_(s) { return String(s || '').replace(/\D/g, ''); }

function samePhone_(a, b) {
  a = digits_(a); b = digits_(b);
  return !!a && !!b && a.slice(-10) === b.slice(-10);
}

function toBooking_(r) {
  return {
    id: r[COL.ID], date: r[COL.DATE], time: r[COL.TIME], master: r[COL.MASTER],
    name: r[COL.NAME], phone: r[COL.PHONE], service: r[COL.SERVICE],
    status: r[COL.STATUS], reminded: r[COL.REMINDED] === REMINDED_MARK,
  };
}

function minutesUntil_(r) {
  const start = Utilities.parseDate(r[COL.DATE] + ' ' + r[COL.TIME], TZ, 'yyyy-MM-dd HH:mm');
  return (start.getTime() - Date.now()) / 60000;
}

function findRowIndex_(rows, req) {
  if (req.id) return rows.findIndex(r => r[COL.ID] === req.id);
  return rows.findIndex(r =>
    r[COL.DATE] === req.date && r[COL.TIME] === req.time && r[COL.MASTER] === req.master &&
    ACTIVE.includes(r[COL.STATUS]));
}

// ---------- Действия ----------

// Проверка + запись одним атомарным блоком (внутри withLock_)
function bookSlot_(req) {
  const { sh, rows } = readAll_();

  const busy = rows.find(r =>
    r[COL.DATE] === req.date && r[COL.TIME] === req.time && r[COL.MASTER] === req.master &&
    ACTIVE.includes(r[COL.STATUS]));
  if (busy) {
    return { ok: false, error: 'slot_taken', occupied_by: busy[COL.NAME], occupied_phone: busy[COL.PHONE] };
  }

  // TODO: если у вас уже есть проверка «мастер работает в это время» по листу
  // «График мастеров» — оставьте её ЗДЕСЬ, внутри блокировки.

  const id = Utilities.getUuid();
  const created = Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd HH:mm:ss');
  sh.appendRow([id, req.date, req.time, req.master, req.client_name, req.phone, req.service || '', ST.BOOKED, '', created]);
  return { ok: true, id: id };
}

// Отмена по id ИЛИ по (date, time, master). Строка остаётся со статусом «Отменено» — слот свободен.
function cancelSlot_(req) {
  const { sh, rows } = readAll_();
  const i = findRowIndex_(rows, req);
  if (i < 0) return { ok: false, error: 'not_found' };
  if (rows[i][COL.STATUS] === ST.CANCELED) return { ok: false, error: 'already_canceled' };
  sh.getRange(i + 2, COL.STATUS + 1).setValue(ST.CANCELED);
  const b = toBooking_(rows[i]);
  b.status = ST.CANCELED;
  return { ok: true, booking: b };
}

function confirmBooking_(req) {
  const { sh, rows } = readAll_();
  const i = findRowIndex_(rows, req);
  if (i < 0) return { ok: false, error: 'not_found' };
  if (rows[i][COL.STATUS] === ST.CANCELED) return { ok: false, error: 'already_canceled' };
  sh.getRange(i + 2, COL.STATUS + 1).setValue(ST.CONFIRMED);
  return { ok: true };
}

// Ставит/снимает отметку «напоминание отправлено». changed=false, если отметка уже стояла —
// так два параллельных запуска задачи не отправят напоминание дважды.
function setReminded_(req) {
  const { sh, rows } = readAll_();
  const i = rows.findIndex(r => r[COL.ID] === req.id);
  if (i < 0) return { ok: false, error: 'not_found' };
  const already = rows[i][COL.REMINDED] === REMINDED_MARK;
  if (req.value === already) return { ok: true, changed: false };
  sh.getRange(i + 2, COL.REMINDED + 1).setValue(req.value ? REMINDED_MARK : '');
  return { ok: true, changed: true };
}

// Записи со статусом «Записан», у которых до начала от from_min до to_min минут
// и напоминание ещё не отправлялось.
function getUpcomingReminders_(req) {
  const { rows } = readAll_();
  const result = [];
  rows.forEach(r => {
    if (r[COL.STATUS] !== ST.BOOKED || r[COL.REMINDED] === REMINDED_MARK) return;
    if (!r[COL.DATE] || !r[COL.TIME]) return;
    const m = minutesUntil_(r);
    if (m >= req.from_min && m < req.to_min) result.push(toBooking_(r));
  });
  return { ok: true, bookings: result };
}

// Ближайшая активная запись клиента, по которой мы уже слали напоминание
// (от -15 минут до within_min минут до начала). Нужна, чтобы понять, к какой записи относится «Да»/«Отменяю».
function findBookingByPhone_(req) {
  const { rows } = readAll_();
  let best = null, bestM = Infinity;
  rows.forEach(r => {
    if (!ACTIVE.includes(r[COL.STATUS]) || r[COL.REMINDED] !== REMINDED_MARK) return;
    if (!samePhone_(r[COL.PHONE], req.phone)) return;
    const m = minutesUntil_(r);
    if (m < -15 || m > req.within_min) return;
    if (m < bestM) { best = toBooking_(r); bestM = m; }
  });
  return { ok: true, booking: best };
}
