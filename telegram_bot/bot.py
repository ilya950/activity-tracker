"""
Telegram-бот для учёта активности.
Пользователь нажимает кнопку «Зашёл в войс» — бот записывает время начала.
Нажимает «Вышел из войса» — бот записывает время окончания.
Все данные автоматически отправляются в Google Sheets.
"""

import os
import re
import sys
import json
import logging
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# Добавляем родительскую папку в PATH для импорта shared
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from shared.google_sheets import (
    append_activity_record,
    update_end_time,
    verify_access,
    is_nickname_allowed,
    load_users,
    save_user,
    delete_user,
)
from shared.google_drive import is_configured as drive_configured, upload_image

# ──────────────── Настройки ────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path: str) -> None:
    """Простой загрузчик .env без внешних зависимостей.

    Значения из файла подставляются только для тех переменных, которых нет
    в окружении: переменные хостинга всегда главнее файла.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except Exception as exc:  # .env не должен ломать запуск
        print(f"Предупреждение: не удалось прочитать .env ({exc})")


_load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Секретов в коде нет: токен берётся только из переменных окружения или .env.
# Так файлы проекта можно безопасно передавать и выкладывать.
TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")  # так токен называют некоторые хостинги
    or ""
).strip()
# ID таблицы не секрет (без ключа доступа он бесполезен), поэтому оставлен по умолчанию.
SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID",
    "19HDqMFo2Ysin9Bl1fIC9gZnc0qMnsVjmT0bgvZ6KXo0",
)
SHEET_NAME = os.getenv("SHEET_NAME", "Лист1")

# Необязательно: канал/группа, куда бот пересылает скриншоты, если Drive не настроен.
# Например -1001234567890 или @my_photo_channel
PHOTO_CHANNEL_ID = os.getenv("PHOTO_CHANNEL_ID", "").strip()

# Белый список Telegram ID: если задан, бот пускает только этих людей.
# Пусто — пускает всех (как раньше). Ушедшему модератору достаточно убрать его ID.
ALLOWED_IDS = {
    int(part)
    for part in re.split(r"[,;\s]+", os.getenv("ALLOWED_IDS", ""))
    if part.strip().isdigit()
}

# ──────────────── Логирование ────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def md(text) -> str:
    """Экранирует пользовательский текст для parse_mode="Markdown".

    Без этого никнейм с символами _ * ` [ ломает отправку сообщения
    (BadRequest: Can't parse entities).
    """
    return escape_markdown(str(text), version=1)


# ──────────────── Состояния бота ────────────────
REGISTER_NICKNAME, REGISTER_POSITION, MAIN_MENU, WAIT_JOIN_PHOTO, WAIT_LEAVE_PHOTO = range(5)

# Данные модераторов: user_id -> {"nickname", "position", "active_session"}
# Основной источник — лист «Пользователи» в Google-таблице, а user_data —
# только кэш в памяти. На бесплатных хостингах диск живёт до первой
# перезагрузки, и прошлогодний вариант с user_data.json терял все регистрации
# после каждого рестарта: людям приходилось заново вводить ник и должность.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_data.json")

user_data: dict = {}


def _load_from_file() -> dict:
    """Читает старый файл user_data.json — нужен для разовой миграции."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            raw = json.load(file)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Не удалось прочитать %s: %s", STATE_FILE, exc)
        return {}

    restored = {}
    for key, value in raw.items():
        try:
            restored[int(key)] = value
        except (TypeError, ValueError):
            continue
    return restored


def load_state() -> dict:
    """Загружает регистрации из таблицы; при пустом листе — из старого файла."""
    try:
        users = load_users(SPREADSHEET_ID)
    except Exception as exc:
        logger.error("Не удалось прочитать лист «Пользователи»: %s", exc)
        users = {}

    if users:
        logger.info("Загружено регистраций из таблицы: %s", len(users))
        return users

    legacy = _load_from_file()
    if legacy:
        logger.info("Лист «Пользователи» пуст — мигрируем %s записей из user_data.json",
                    len(legacy))
        for user_id, info in legacy.items():
            try:
                save_user(
                    SPREADSHEET_ID,
                    user_id,
                    info.get("nickname", ""),
                    info.get("position", ""),
                    bool(info.get("active_session")),
                )
            except Exception as exc:
                logger.error("Миграция %s не удалась: %s", user_id, exc)
        return legacy
    return {}


def _persist(user_id: int) -> None:
    """Пишет текущего пользователя в таблицу (кэш уже обновлён вызывающим кодом).

    Ошибка записи не роняет бота: нажатая кнопка уже обработана, а следующая
    операция допишет состояние заново.
    """
    info = user_data.get(user_id)
    if not info:
        return
    try:
        save_user(
            SPREADSHEET_ID,
            user_id,
            info["nickname"],
            info["position"],
            info["active_session"],
        )
    except Exception as exc:
        logger.warning("Не удалось сохранить пользователя %s в таблицу: %s", user_id, exc)


def _forget(user_id: int) -> None:
    """Убирает регистрацию из таблицы и из кэша."""
    user_data.pop(user_id, None)
    try:
        delete_user(SPREADSHEET_ID, user_id)
    except Exception as exc:
        logger.warning("Не удалось удалить пользователя %s из таблицы: %s", user_id, exc)


ACCESS_DENIED_MSG = (
    "🚫 **Доступ закрыт**\n\n"
    "Ваш никнейм не найден в списке разрешённых модераторов.\n"
    "Обратитесь к администратору для восстановления доступа."
)


async def _check_access(nickname: str) -> bool:
    """Проверяет, разрешён ли никнейму доступ (лист «Доступ» в таблице)."""
    try:
        return is_nickname_allowed(SPREADSHEET_ID, nickname)
    except Exception as exc:
        logger.error("Ошибка проверки доступа: %s", exc)
        return True  # при ошибке не блокируем


def get_main_keyboard():
    """Главная клавиатура с кнопками."""
    keyboard = [
        [KeyboardButton("🟢 Зашёл в войс"), KeyboardButton("🔴 Вышел из войса")],
        [KeyboardButton("📊 Мой статус"), KeyboardButton("⚙️ Изменить данные")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_register_keyboard():
    """Клавиатура при регистрации."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Отмена")]],
        resize_keyboard=True,
    )


# ──────────────── Команды ────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — начало работы с ботом."""
    user_id = update.effective_user.id
    
    if user_id in user_data:
        # Проверяем, не забрали ли доступ
        if not await _check_access(user_data[user_id]["nickname"]):
            await update.message.reply_text(
                ACCESS_DENIED_MSG,
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        await update.message.reply_text(
            f"👋 С возвращением, **{md(user_data[user_id]['nickname'])}**!\n"
            f"Должность: {md(user_data[user_id]['position'])}\n\n"
            "Используйте кнопки ниже для отметки активности.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
        return MAIN_MENU
    
    await update.message.reply_text(
        "👋 Добро пожаловать в бот учёта активности!\n\n"
        "Для начала давайте зарегистрируемся.\n"
        "Введите ваш **никнейм**:",
        reply_markup=get_register_keyboard(),
        parse_mode="Markdown",
    )
    return REGISTER_NICKNAME


async def register_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение никнейма при регистрации."""
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена. Напишите /start для начала.")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    context.user_data["nickname"] = text
    
    await update.message.reply_text(
        f"✅ Никнейм: **{md(text)}**\n\n"
        "Теперь введите вашу **должность**:",
        reply_markup=get_register_keyboard(),
        parse_mode="Markdown",
    )
    return REGISTER_POSITION


async def register_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение должности при регистрации."""
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await update.message.reply_text("Регистрация отменена. Напишите /start для начала.")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    nickname = context.user_data.get("nickname")
    if not nickname:
        # состояние потерялось (например, после перезапуска) — просим никнейм заново
        await update.message.reply_text(
            "⚠️ Давайте начнём заново. Введите ваш **никнейм**:",
            reply_markup=get_register_keyboard(),
            parse_mode="Markdown",
        )
        return REGISTER_NICKNAME
    
    user_data[user_id] = {
        "nickname": nickname,
        "position": text,
        "active_session": False,
    }
    _persist(user_id)

    await update.message.reply_text(
        f"✅ Регистрация завершена!\n\n"
        f"👤 Никнейм: **{md(nickname)}**\n"
        f"💼 Должность: **{md(text)}**\n\n"
        "Теперь вы можете отмечать вход/выход из войса.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )
    return MAIN_MENU


PHOTO_PROMPT_JOIN = (
    "📸 **Вход в войс**\n\n"
    "Пришлите скриншот, на котором видно, что вы **в голосовом канале**.\n"
    "Время входа запишется по моменту нажатия кнопки."
)
PHOTO_PROMPT_LEAVE = (
    "📸 **Выход из войса**\n\n"
    "Пришлите скриншот, на котором видно, что вы **вышли из голосового канала**.\n"
    "Время выхода запишется по моменту нажатия кнопки."
)


async def _save_photo(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      filename: str) -> str:
    """Сохраняет скриншот и возвращает ссылку на него.

    Варианты (по приоритету):
    1. Google Drive через Apps Script или сервисный аккаунт (если настроено).
    2. Пересылка в Telegram-канал PHOTO_CHANNEL_ID и ссылка на сообщение.

    Ошибки не мешают записи времени — фото в любом случае остаётся в чате.
    """
    photo = update.message.photo[-1]

    if drive_configured():
        try:
            tg_file = await photo.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            if data:
                link = upload_image(data, filename)
                if link:
                    return link
        except Exception as exc:
            logger.error("Не удалось сохранить скриншот в Drive: %s: %s",
                         type(exc).__name__, exc)

    if PHOTO_CHANNEL_ID:
        try:
            sent = await context.bot.send_photo(
                chat_id=PHOTO_CHANNEL_ID,
                photo=photo.file_id,
                caption=filename,
            )
            chat = await context.bot.get_chat(PHOTO_CHANNEL_ID)
            if chat.username:
                return f"https://t.me/{chat.username}/{sent.message_id}"
            internal = str(chat.id).lstrip("-").replace("100", "", 1)
            return f"https://t.me/c/{internal}/{sent.message_id}"
        except Exception as exc:
            logger.error("Не удалось переслать скриншот в канал: %s: %s",
                         type(exc).__name__, exc)

    logger.warning(
        "Скриншот не сохранён: не настроено хранилище. "
        "Нужен proxy.json / DRIVE_FOLDER_ID (Google Drive) либо PHOTO_CHANNEL_ID (канал)."
    )
    return ""


async def voice_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Зашёл в войс» — сначала просим скриншот."""
    user_id = update.effective_user.id

    if user_id not in user_data:
        await update.message.reply_text("⚠️ Сначала зарегистрируйтесь: /start")
        return MAIN_MENU

    # Проверка доступа (только для входа, не для выхода)
    if not await _check_access(user_data[user_id]["nickname"]):
        await update.message.reply_text(ACCESS_DENIED_MSG, parse_mode="Markdown")
        return MAIN_MENU

    if user_data[user_id]["active_session"]:
        await update.message.reply_text(
            "⚠️ У вас уже есть активная сессия!\n"
            "Сначала нажмите «🔴 Вышел из войса».",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU

    # Время фиксируем в момент нажатия, а запись делаем после скриншота
    context.user_data["pending_join_time"] = datetime.now()

    await update.message.reply_text(
        PHOTO_PROMPT_JOIN,
        reply_markup=get_register_keyboard(),
        parse_mode="Markdown",
    )
    return WAIT_JOIN_PHOTO


async def join_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получен скриншот входа — записываем строку в таблицу."""
    user_id = update.effective_user.id
    pending = context.user_data.get("pending_join_time")

    if user_id not in user_data:
        await update.message.reply_text("⚠️ Сначала зарегистрируйтесь: /start")
        return MAIN_MENU

    if not pending:
        await update.message.reply_text(
            "⚠️ Не понял, к чему скриншот. Нажмите «🟢 Зашёл в войс» и пришлите фото заново.",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU

    nickname = user_data[user_id]["nickname"]
    position = user_data[user_id]["position"]
    start_time = pending.strftime("%H:%M")
    date_str = pending.strftime("%d.%m.%Y")

    photo_url = await _save_photo(
        update, context, f"{date_str}_{start_time}_{nickname}_вход.jpg"
    )

    try:
        append_activity_record(
            spreadsheet_id=SPREADSHEET_ID,
            nickname=nickname,
            position=position,
            start_time=start_time,
            end_time="",  # заполнится при выходе
            date_str=date_str,
            sheet_name=SHEET_NAME,
            photo_url=photo_url,
        )
    except Exception as exc:
        logger.error("Ошибка записи в Google Sheets: %s", exc)
        await update.message.reply_text(
            f"❌ Ошибка при записи в таблицу:\n{exc}",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU

    user_data[user_id]["active_session"] = True
    _persist(user_id)
    context.user_data.pop("pending_join_time", None)

    await update.message.reply_text(
        f"🟢 **Вход зафиксирован!**\n\n"
        f"👤 {md(nickname)}\n"
        f"⏰ Время входа: {start_time}\n"
        f"📅 Дата: {date_str}\n"
        f"📸 Скриншот получен" + (" и сохранён в Drive" if photo_url else "") + "\n\n"
        "Не забудьте нажать «🔴 Вышел из войса», когда закончите!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )
    return MAIN_MENU


async def voice_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Вышел из войса» — сначала просим скриншот.

    Доступ здесь НЕ проверяется: ушедшему модератору нужно дать возможность
    закрыть начатую сессию, иначе в таблице навсегда останется строка
    без времени выхода.
    """
    user_id = update.effective_user.id

    if user_id not in user_data:
        await update.message.reply_text("⚠️ Сначала зарегистрируйтесь: /start")
        return MAIN_MENU

    if not user_data[user_id]["active_session"]:
        await update.message.reply_text(
            "⚠️ У вас нет активной сессии!\n"
            "Сначала нажмите «🟢 Зашёл в войс».",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU

    context.user_data["pending_leave_time"] = datetime.now()

    await update.message.reply_text(
        PHOTO_PROMPT_LEAVE,
        reply_markup=get_register_keyboard(),
        parse_mode="Markdown",
    )
    return WAIT_LEAVE_PHOTO


async def leave_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получен скриншот выхода — заполняем время выхода."""
    user_id = update.effective_user.id
    pending = context.user_data.get("pending_leave_time")

    if user_id not in user_data:
        await update.message.reply_text("⚠️ Сначала зарегистрируйтесь: /start")
        return MAIN_MENU

    if not pending:
        await update.message.reply_text(
            "⚠️ Не понял, к чему скриншот. Нажмите «🔴 Вышел из войса» и пришлите фото заново.",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU

    nickname = user_data[user_id]["nickname"]
    end_time = pending.strftime("%H:%M")
    date_str = pending.strftime("%d.%m.%Y")

    photo_url = await _save_photo(
        update, context, f"{date_str}_{end_time}_{nickname}_выход.jpg"
    )

    try:
        updated = update_end_time(
            spreadsheet_id=SPREADSHEET_ID,
            nickname=nickname,
            end_time=end_time,
            sheet_name=SHEET_NAME,
            photo_url=photo_url,
            date_str=date_str,
        )
    except Exception as exc:
        logger.error("Ошибка обновления Google Sheets: %s", exc)
        await update.message.reply_text(
            f"❌ Ошибка при обновлении таблицы:\n{exc}",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU

    user_data[user_id]["active_session"] = False
    _persist(user_id)
    context.user_data.pop("pending_leave_time", None)

    if updated:
        await update.message.reply_text(
            f"🔴 **Выход зафиксирован!**\n\n"
            f"👤 {md(nickname)}\n"
            f"⏰ Время выхода: {end_time}\n"
            f"📸 Скриншот получен" + (" и сохранён в Drive" if photo_url else "") + "\n\n"
            "Данные записаны в таблицу ✅",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "⚠️ Не удалось найти открытую сессию в таблице.\n"
            "Возможно, запись была удалена вручную.",
            reply_markup=get_main_keyboard(),
        )

    return MAIN_MENU


async def waiting_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вместо скриншота прислали текст — напоминаем, что нужно фото."""
    await update.message.reply_text(
        "❗ Нужен именно **скриншот** (фото). Пришлите картинку или нажмите «❌ Отмена».",
        reply_markup=get_register_keyboard(),
        parse_mode="Markdown",
    )
    return WAIT_JOIN_PHOTO if "pending_join_time" in context.user_data else WAIT_LEAVE_PHOTO


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий статус пользователя."""
    user_id = update.effective_user.id
    
    if user_id not in user_data:
        await update.message.reply_text("⚠️ Сначала зарегистрируйтесь: /start")
        return MAIN_MENU
    
    data = user_data[user_id]
    session_status = "🟢 В войсе" if data["active_session"] else "🔴 Не в войсе"
    
    await update.message.reply_text(
        f"📊 **Ваш статус:**\n\n"
        f"👤 Никнейм: {md(data['nickname'])}\n"
        f"💼 Должность: {md(data['position'])}\n"
        f"📡 Статус: {session_status}",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )
    return MAIN_MENU


async def change_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск изменения данных пользователя."""
    user_id = update.effective_user.id
    
    if user_id in user_data and user_data[user_id]["active_session"]:
        await update.message.reply_text(
            "⚠️ Сначала завершите текущую сессию (нажмите «🔴 Вышел из войса»).",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU
    
    # Удаляем старые данные и начинаем регистрацию заново
    if user_id in user_data:
        _forget(user_id)
    
    await update.message.reply_text(
        "⚙️ Давайте обновим ваши данные.\n"
        "Введите новый **никнейм**:",
        reply_markup=get_register_keyboard(),
        parse_mode="Markdown",
    )
    return REGISTER_NICKNAME


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия (в том числе ожидания скриншота)."""
    user_id = update.effective_user.id

    context.user_data.pop("pending_join_time", None)
    context.user_data.pop("pending_leave_time", None)

    if user_id in user_data:
        await update.message.reply_text(
            "Действие отменено. Ничего не записано.",
            reply_markup=get_main_keyboard(),
        )
        return MAIN_MENU

    await update.message.reply_text("Действие отменено. Напишите /start для начала.")
    return ConversationHandler.END


def build_application() -> Application:
    """Собирает приложение и все обработчики (без запуска сети).

    Вынесено отдельно, чтобы это можно было проверить тестом.
    """
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)

    # Необязательный прокси — пригодится, если Telegram недоступен напрямую:
    # TELEGRAM_PROXY=http://host:port  или  socks5://user:pass@host:port
    tg_proxy = os.getenv("TELEGRAM_PROXY", "").strip()
    if tg_proxy:
        builder = builder.proxy(tg_proxy).get_updates_proxy(tg_proxy)
        logger.info("Используется прокси: %s", tg_proxy.split("@")[-1])

    application = builder.build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTER_NICKNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_nickname),
            ],
            REGISTER_POSITION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_position),
            ],
            MAIN_MENU: [
                MessageHandler(filters.Regex("^🟢 Зашёл в войс$"), voice_join),
                MessageHandler(filters.Regex("^🔴 Вышел из войса$"), voice_leave),
                MessageHandler(filters.Regex("^📊 Мой статус$"), status),
                MessageHandler(filters.Regex("^⚙️ Изменить данные$"), change_data),
            ],
            WAIT_JOIN_PHOTO: [
                MessageHandler(filters.PHOTO, join_photo),
                MessageHandler(filters.ALL, waiting_photo),
            ],
            WAIT_LEAVE_PHOTO: [
                MessageHandler(filters.PHOTO, leave_photo),
                MessageHandler(filters.ALL, waiting_photo),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    application.add_handler(conv_handler)
    return application


def main():
    """Запуск бота."""
    if not TELEGRAM_BOT_TOKEN:
        print("=" * 60)
        print("ОШИБКА: Не указан токен Telegram-бота!")
        print("Задайте переменную окружения TELEGRAM_BOT_TOKEN")
        print("(на хостинге — в Variables, локально — в файле .env)")
        print("=" * 60)
        sys.exit(1)

    if not SPREADSHEET_ID:
        print("=" * 60)
        print("ОШИБКА: Не указан ID Google-таблицы!")
        print("Установите переменную окружения SPREADSHEET_ID")
        print("или укажите значение по умолчанию в начале файла bot.py")
        print("=" * 60)
        sys.exit(1)

    # Проверяем доступ к таблице до запуска, чтобы ошибка была понятной
    # и на месте, а не при первом нажатии кнопки пользователем.
    ok, message = verify_access(SPREADSHEET_ID, SHEET_NAME)
    if not ok:
        print("=" * 60)
        print("ОШИБКА ДОСТУПА К GOOGLE-ТАБЛИЦЕ:")
        print(message)
        print("=" * 60)
        sys.exit(1)
    logger.info("Проверка таблицы: %s", message)

    # Регистрации читаем из листа «Пользователи»: на хостингах диск обнуляется
    # при каждом деплое, и файл user_data.json терял все учётные записи.
    global user_data
    user_data = load_state()

    application = build_application()

    print("=" * 60)
    print("🤖 Telegram-бот учёта активности запущен!")
    print(f"📊 Таблица: {SPREADSHEET_ID}")
    print(f"📄 Лист: {SHEET_NAME}")
    print("⏹  Остановить: Ctrl+C")
    print("=" * 60)

    # Запуск бота
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as exc:
        print("=" * 60)
        print(f"Бот остановлен из-за ошибки: {type(exc).__name__}")
        print("Если это NetworkError / TimedOut — скорее всего api.telegram.org")
        print("недоступен из этой сети. Загрузите бота на бесплатный хостинг")
        print("(раздел 3 гайда) либо включите VPN.")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
