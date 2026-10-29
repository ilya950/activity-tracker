"""
Общий модуль для работы с Google Sheets API.
Используется и Telegram-ботом, и PC-приложением.
"""

import os
import sys
import json
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

# Имя листа с белым списком модераторов
ACCESS_SHEET_NAME = "Доступ"


# Области доступа для Google Sheets API
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ──────────────── Режим «без ключа» (через Apps Script) ────────────────
# Если задан SHEETS_PROXY_URL (переменная окружения или файл proxy.json рядом
# с программой), запись идёт через веб-приложение Google Apps Script.
# Тогда ключ Google на компьютерах пользователей не нужен вообще.

def proxy_config() -> tuple[str, str]:
    """Возвращает (url, secret) для режима без ключа. Пустые строки — режим выключен."""
    url = os.getenv("SHEETS_PROXY_URL", "").strip()
    secret = os.getenv("SHEETS_PROXY_SECRET", "").strip()
    if url:
        return url, secret

    path = os.path.join(_project_root(), "proxy.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as file:
                data = json.load(file)
            return str(data.get("url", "")).strip(), str(data.get("secret", "")).strip()
        except Exception as exc:
            logger.error("Не удалось прочитать proxy.json: %s", exc)
    return "", ""


def _post_to_proxy(payload: dict) -> dict:
    """Отправляет данные в Apps Script. Возвращает ответ сервера."""
    import requests

    url, secret = proxy_config()
    if not url:
        raise RuntimeError("Не задан адрес веб-приложения (SHEETS_PROXY_URL)")

    body = dict(payload)
    body["secret"] = secret
    response = requests.post(url, json=body, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Сервер записи ответил {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json()
    except Exception:
        return {}


def _project_root() -> str:
    """Корень проекта.

    Для обычного запуска — папка с исходниками (на уровень выше shared/).
    Для собранного PyInstaller .exe — папка, где лежит сам .exe,
    чтобы credentials.json можно было положить рядом и не «запекать» в программу.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def credentials_candidates():
    """Где искать credentials.json, по порядку приоритета.

    1. Рядом с .exe (можно заменить ключ, не пересобирая программу).
    2. Внутри однофайлового .exe (_MEIPASS) — если ключ упакован в сборку.
    3. Корень проекта при запуске из исходников.
    """
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "credentials.json"))
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "credentials.json"))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials.json"))
    return candidates


# Путь к файлу сервисного аккаунта (для обратной совместимости)
CREDENTIALS_FILE = credentials_candidates()[0]


def get_credentials() -> Credentials:
    """Возвращает учётные данные сервисного аккаунта.

    Поддерживает два способа передачи credentials:
    1. Переменная окружения GOOGLE_CREDENTIALS_JSON (для хостинга/сервера)
    2. Файл credentials.json (для локального запуска)
    """
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_dict = json.loads(creds_json)
        return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

    tried = []
    for candidate in credentials_candidates():
        tried.append(candidate)
        if os.path.exists(candidate):
            return Credentials.from_service_account_file(candidate, scopes=SCOPES)

    raise FileNotFoundError(
        "Не найдены credentials! Искал в:\n"
        + "\n".join(f"  {path}" for path in tried)
        + "\nЛибо положите credentials.json рядом с программой,\n"
        "либо установите переменную окружения GOOGLE_CREDENTIALS_JSON"
    )


def get_sheets_client() -> gspread.Client:
    """Создаёт и возвращает авторизованный клиент Google Sheets."""
    return gspread.authorize(get_credentials())


def get_spreadsheet(spreadsheet_id: str) -> gspread.Spreadsheet:
    """Открывает таблицу по ID."""
    client = get_sheets_client()
    return client.open_by_key(spreadsheet_id)


def verify_access(spreadsheet_id: str, sheet_name: str = "Лист1"):
    """Проверяет ключ, доступ к таблице и наличие листа. Ничего не записывает.

    Возвращает (успех, текст сообщения для человека).
    """
    try:
        book = get_spreadsheet(spreadsheet_id)
    except FileNotFoundError as exc:
        return False, str(exc)
    except Exception as exc:
        if "SpreadsheetNotFound" in type(exc).__name__:
            return False, (
                "Таблица недоступна сервисному аккаунту (404 SpreadsheetNotFound).\n"
                "Откройте таблицу -> «Настройки доступа» и добавьте client_email из\n"
                "credentials.json с правами «Редактор». Либо проверьте SPREADSHEET_ID."
            )
        return False, f"Ошибка доступа к Google Sheets: {type(exc).__name__}: {exc}"

    titles = [ws.title for ws in book.worksheets()]
    if sheet_name not in titles:
        return False, (
            f"В таблице «{book.title}» нет листа «{sheet_name}».\n"
            f"Есть листы: {', '.join(titles) if titles else '—'}.\n"
            "Переименуйте лист или задайте переменную SHEET_NAME."
        )
    return True, f"Таблица «{book.title}», лист «{sheet_name}» — доступ есть."


# Колонки со скриншотами: J — «Фото входа», K — «Фото выхода»
# (после того как пользователь удалил столбец G, всё сдвинулось влево)
PHOTO_IN_COLUMN = 10
PHOTO_OUT_COLUMN = 11
PHOTO_IN_LETTER = "J"
PHOTO_OUT_LETTER = "K"


def _ensure_photo_headers(worksheet) -> None:
    """Пишет заголовки колонок со скриншотами (J и K), если их там ещё нет."""
    try:
        current = worksheet.get("J3:K3")
        row = current[0] if current else []
        left = row[0] if len(row) > 0 else ""
        right = row[1] if len(row) > 1 else ""
        if not left and not right:
            worksheet.update(
                values=[["Фото входа", "Фото выхода"]], range_name="J3:K3", raw=True
            )
    except Exception as exc:
        logger.warning("Не удалось записать заголовки фото: %s", exc)


def _next_free_row(worksheet) -> int:
    """Первая свободная строка после последней записи (по колонке «Никнейм»).

    Нельзя полагаться на len(get_all_values()): в листе есть формулы,
    которые тянутся вниз и возвращают пустые строки.
    """
    nicknames = worksheet.col_values(2)  # колонка B
    last = 0
    for index, value in enumerate(nicknames, start=1):
        if str(value).strip():
            last = index
    return max(last + 1, 4)


def append_activity_record(
    spreadsheet_id: str,
    nickname: str,
    position: str,
    start_time: str,
    end_time: str,
    date_str: str,
    sheet_name: str = "Лист1",
    photo_url: str = "",
):
    """
    Добавляет запись об активности в Google-таблицу.

    Колонки: Никнейм | Должность | Начал | Закончил | Число | (Фото входа в L)

    Args:
        spreadsheet_id: ID Google-таблицы из URL
        nickname: Никнейм пользователя
        position: Должность
        start_time: Время начала (формат "ЧЧ:ММ")
        end_time: Время окончания (формат "ЧЧ:ММ")
        date_str: Дата (формат "ДД.ММ.ГГГГ")
        sheet_name: Название листа (по умолчанию "Лист1")
        photo_url: ссылка на скриншот входа (Google Drive), если есть
    """
    url, _ = proxy_config()
    if url:
        result = _post_to_proxy({
            "action": "join",
            "nickname": nickname,
            "position": position,
            "start": start_time,
            "date": date_str,
            "photo_url": photo_url,
        })
        if not result.get("ok", False):
            raise RuntimeError(result.get("error", "сервер записи вернул ошибку"))
        logger.info("Запись добавлена через веб-приложение: %s", nickname)
        return True

    spreadsheet = get_spreadsheet(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.sheet1

    # Данные для записи (колонки B-F, т.к. A пустая)
    row = [nickname, position, start_time, end_time, date_str]

    next_row = _next_free_row(worksheet)

    # Записываем в колонки B-F
    worksheet.update(
        values=[row], range_name=f"B{next_row}:F{next_row}", raw=True
    )

    # Ссылка на скриншот входа — в колонку K («Фото входа»)
    if photo_url:
        _ensure_photo_headers(worksheet)
        worksheet.update(
            values=[[photo_url]],
            range_name=f"{PHOTO_IN_LETTER}{next_row}",
            raw=True,
        )

    logger.info(
        "Запись добавлена: %s | %s | %s - %s | %s",
        nickname,
        position,
        start_time,
        end_time,
        date_str,
    )
    return True


def update_end_time(
    spreadsheet_id: str,
    nickname: str,
    end_time: str,
    sheet_name: str = "Лист1",
    photo_url: str = "",
):
    """
    Обновляет время окончания для последней записи пользователя,
    у которой ещё нет времени выхода.

    Args:
        spreadsheet_id: ID Google-таблицы
        nickname: Никнейм пользователя
        end_time: Время окончания
        sheet_name: Название листа
        photo_url: ссылка на скриншот выхода (Google Drive), если есть

    Returns:
        True если запись найдена и обновлена, False если нет открытой сессии
    """
    url, _ = proxy_config()
    if url:
        result = _post_to_proxy({
            "action": "leave",
            "nickname": nickname,
            "end": end_time,
            "photo_url": photo_url,
        })
        if not result.get("ok", False):
            raise RuntimeError(result.get("error", "сервер записи вернул ошибку"))
        return bool(result.get("updated", False))

    spreadsheet = get_spreadsheet(spreadsheet_id)
    
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.sheet1
    
    all_values = worksheet.get_all_values()
    
    # Ищем последнюю строку с этим никнеймом и пустым "Закончил"
    for i in range(len(all_values) - 1, 0, -1):
        row = all_values[i]
        # Колонка B (индекс 1) = Никнейм, Колонка E (индекс 4) = Закончил
        if len(row) > 4 and row[1].strip() == nickname.strip() and row[4].strip() == "":
            # Нашли — обновляем колонку E (Закончил).
            # raw=True: пишем именно текст "09:05", чтобы Sheets не превращал его
            # в значение времени и не терял ведущий ноль.
            row_num = i + 1  # +1 т.к. индексация gspread с 1
            worksheet.update(
                values=[[end_time]], range_name=f"E{row_num}", raw=True
            )

            # Ссылка на скриншот выхода — в колонку L («Фото выхода»)
            if photo_url:
                _ensure_photo_headers(worksheet)
                worksheet.update(
                    values=[[photo_url]],
                    range_name=f"{PHOTO_OUT_LETTER}{row_num}",
                    raw=True,
                )

            logger.info("Обновлено время выхода для %s: %s", nickname, end_time)
            return True
    
    return False



# ──────────────── Управление доступом (белый список) ────────────────

def _get_access_list_direct(spreadsheet_id: str) -> list[str]:
    """Читает список разрешённых никнеймов из листа «Доступ» через Google API.

    Лист «Доступ» содержит одну колонку (A) с никнеймами, начиная со строки 2
    (строка 1 — заголовок). Пустые строки игнорируются.

    Если листа нет — создаёт его с заголовком и возвращает пустой список
    (пустой список = доступ не ограничен, чтобы не блокировать всех при первом запуске).
    """
    try:
        spreadsheet = get_spreadsheet(spreadsheet_id)
    except Exception as exc:
        logger.error("Не удалось открыть таблицу для проверки доступа: %s", exc)
        return []

    try:
        ws = spreadsheet.worksheet(ACCESS_SHEET_NAME)
    except Exception:
        # Листа ещё нет — создаём его
        try:
            ws = spreadsheet.add_worksheet(title=ACCESS_SHEET_NAME, rows=50, cols=2)
            ws.update(values=[["Никнейм"]], range_name="A1", raw=True)
            logger.info("Создан лист «%s» в таблице", ACCESS_SHEET_NAME)
        except Exception as exc:
            logger.error("Не удалось создать лист «%s»: %s", ACCESS_SHEET_NAME, exc)
        return []  # лист пустой — пускаем всех

    try:
        values = ws.col_values(1)  # колонка A
        # Пропускаем заголовок (строка 1)
        nicknames = [v.strip() for v in values[1:] if v.strip()]
        return nicknames
    except Exception as exc:
        logger.error("Не удалось прочитать лист «%s»: %s", ACCESS_SHEET_NAME, exc)
        return []


def _get_access_list_proxy(nickname: str) -> bool:
    """Проверяет доступ через Apps Script прокси.

    Отправляет action=check_access, ожидает {ok: true, allowed: true/false}.
    Если прокси не поддерживает эту команду — пускает всех (обратная совместимость).
    """
    try:
        result = _post_to_proxy({
            "action": "check_access",
            "nickname": nickname,
        })
        if "allowed" in result:
            return bool(result["allowed"])
        # Старая версия Apps Script не знает эту команду — пускаем
        return True
    except Exception as exc:
        logger.error("Ошибка проверки доступа через прокси: %s", exc)
        return True  # при ошибке не блокируем


def is_nickname_allowed(spreadsheet_id: str, nickname: str) -> bool:
    """Проверяет, есть ли никнейм в белом списке на листе «Доступ».

    Логика:
    - Если лист «Доступ» пуст или не существует → пускаем всех (нет ограничений).
    - Если на листе есть хоть один никнейм → пускаем только тех, кто в списке.
    - Сравнение регистронезависимое.
    """
    url, _ = proxy_config()
    if url:
        return _get_access_list_proxy(nickname)

    allowed = _get_access_list_direct(spreadsheet_id)
    if not allowed:
        # Лист пустой или не удалось прочитать — не ограничиваем
        return True
    return nickname.strip().lower() in [n.lower() for n in allowed]


def get_allowed_nicknames(spreadsheet_id: str) -> list[str]:
    """Возвращает список разрешённых никнеймов (для отображения).

    Пустой список = ограничений нет.
    """
    url, _ = proxy_config()
    if url:
        # Через прокси нельзя получить весь список — возвращаем пустой
        return []
    return _get_access_list_direct(spreadsheet_id)


# ──────────────── Хранилище регистраций (лист «Пользователи») ────────────────
# Бот держит регистрации модераторов в таблице, а не в файле на диске:
# у бесплатных хостингов диск живёт до первой перезагрузки, и после рестарта
# всем пришлось бы вводить ник и должность заново.
# Колонки: A — Telegram ID, B — Никнейм, C — Должность, D — Активная сессия.

USERS_SHEET_NAME = "Пользователи"


def _users_sheet(spreadsheet_id: str):
    """Лист «Пользователи»; при первом запуске создаётся с заголовками."""
    spreadsheet = get_spreadsheet(spreadsheet_id)
    try:
        return spreadsheet.worksheet(USERS_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=USERS_SHEET_NAME, rows=200, cols=4)
        worksheet.update(
            values=[["Telegram ID", "Никнейм", "Должность", "Активная сессия"]],
            range_name="A1:D1",
            raw=True,
        )
        logger.info("Создан лист «%s» в таблице", USERS_SHEET_NAME)
        return worksheet


def _as_user_id(value) -> int:
    """Приводит значение из таблицы к Telegram ID, иначе 0.

    Sheets хранит длинные числа как float и иногда отдаёт «6.436178546E+09»,
    поэтому идём через float — int() на такую строку падает.
    """
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _user_row_number(worksheet, user_id: int):
    """Номер строки пользователя или None, если его ещё нет."""
    rows = worksheet.get("A2:A", value_render_option="UNFORMATTED_VALUE")
    for index, row in enumerate(rows, start=2):
        if row and _as_user_id(row[0]) == user_id:
            return index
    return None


def load_users(spreadsheet_id: str) -> dict:
    """Все регистрации: {user_id: {nickname, position, active_session}}.

    Пустой лист или ошибка доступа — пустой словарь (бот стартует,
    модераторы зарегистрируются заново; данные не теряются безвозвратно).
    """
    try:
        worksheet = _users_sheet(spreadsheet_id)
        rows = worksheet.get("A2:D", value_render_option="UNFORMATTED_VALUE")
    except Exception as exc:
        logger.error("Не удалось прочитать лист «%s»: %s", USERS_SHEET_NAME, exc)
        return {}

    users = {}
    for row in rows:
        user_id = _as_user_id(row[0]) if row else 0
        nickname = str(row[1]).strip() if len(row) > 1 else ""
        if not user_id or not nickname:
            continue
        position = str(row[2]).strip() if len(row) > 2 else ""
        active = str(row[3]).strip().lower() in ("true", "1", "да", "yes") if len(row) > 3 else False
        users[user_id] = {
            "nickname": nickname,
            "position": position,
            "active_session": active,
        }
    return users


def save_user(spreadsheet_id: str, user_id: int, nickname: str,
              position: str, active_session: bool) -> None:
    """Создаёт или обновляет регистрацию пользователя."""
    worksheet = _users_sheet(spreadsheet_id)
    row = [user_id, nickname, position, "TRUE" if active_session else "FALSE"]
    row_number = _user_row_number(worksheet, user_id)
    if row_number:
        worksheet.update(values=[row], range_name=f"A{row_number}:D{row_number}", raw=True)
    else:
        # append_row ждёт одну плоскую строку, а не список из одной строки
        worksheet.append_row(row, value_input_option="RAW")
    logger.info("Сохранён пользователь %s (%s)", user_id, nickname)


def delete_user(spreadsheet_id: str, user_id: int) -> bool:
    """Убирает регистрацию (нужно для «Изменить данные»)."""
    worksheet = _users_sheet(spreadsheet_id)
    row_number = _user_row_number(worksheet, user_id)
    if not row_number:
        return False
    # В gspread 6.x нет delete_row; у delete_rows второй аргумент —
    # ПОСЛЕДНИЙ индекс диапазона, а не количество строк (1 вместо row
    # молча не удалила бы ничего).
    worksheet.delete_rows(row_number, row_number)
    logger.info("Удалён пользователь %s", user_id)
    return True
