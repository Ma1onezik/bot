import asyncio
import hashlib
import html
import logging
import os
import re
import secrets
import sqlite3
from collections import OrderedDict
from datetime import datetime, timedelta

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Файл с аналитикой (main.py).
import main as mz

# === НАСТРОЙКА ЛОГГИРОВАНИЯ ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Токен бота берём из переменной окружения, а не из кода.
BOT_TOKEN = "8955041911:AAH89wadY5uuSWVExQ681Vn3-tTaeUEHlno"

DB_PATH = "users.db"
TOKEN_TTL = timedelta(minutes=50)   # access-токен считаем действующим 50 минут

REPORTS = {
    "day": "Отчет за вчера",
    "week": "Отчет за неделю",
    "waiters": "Официанты",
    "plan": "План на сегодня",
}
KIND_BY_TEXT = {title: kind for kind, title in REPORTS.items()}
PERIOD_BUTTON = "Отчет за период"


# === БАЗА ДАННЫХ ===
def _db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            login TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            org_id INTEGER,
            refresh_token TEXT,
            access_token TEXT,
            token_expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Привязка чатов к ресторанам: в какой чат отправлять отчёт ресторана
    cur.execute("""
        CREATE TABLE IF NOT EXISTS restaurant_chats (
            org_id INTEGER NOT NULL,
            rest_key TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            PRIMARY KEY (org_id, rest_key, chat_id)
        )
    """)
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")


def save_user(user_id, login, password, org_id, refresh_token, access_token):
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    expires_at = (datetime.now() + TOKEN_TTL).isoformat()
    conn = _db()
    conn.execute("""
        INSERT OR REPLACE INTO users
        (user_id, login, password_hash, org_id, refresh_token, access_token, token_expires_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (user_id, login, password_hash, org_id, refresh_token, access_token, expires_at))
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = _db()
    row = conn.execute("""
        SELECT login, password_hash, org_id, refresh_token, access_token, token_expires_at
        FROM users WHERE user_id = ?
    """, (user_id,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row[5]) if row[5] else None
    except ValueError:
        expires = None
    return {
        "login": row[0], "password_hash": row[1], "org_id": row[2],
        "refresh_token": row[3], "access_token": row[4], "token_expires_at": expires,
    }


def update_tokens(user_id, access_token, refresh_token=None):
    expires_at = (datetime.now() + TOKEN_TTL).isoformat()
    conn = _db()
    if refresh_token:
        conn.execute("""
            UPDATE users SET access_token = ?, refresh_token = ?, token_expires_at = ?,
            updated_at = CURRENT_TIMESTAMP WHERE user_id = ?
        """, (access_token, refresh_token, expires_at, user_id))
    else:
        conn.execute("""
            UPDATE users SET access_token = ?, token_expires_at = ?,
            updated_at = CURRENT_TIMESTAMP WHERE user_id = ?
        """, (access_token, expires_at, user_id))
    conn.commit()
    conn.close()


def add_chat_link(org_id, rest_key, chat_id):
    conn = _db()
    conn.execute("INSERT OR IGNORE INTO restaurant_chats (org_id, rest_key, chat_id) VALUES (?, ?, ?)",
                 (org_id, rest_key, chat_id))
    conn.commit()
    conn.close()


def get_chat_links(org_id, rest_key):
    conn = _db()
    rows = conn.execute("SELECT chat_id FROM restaurant_chats WHERE org_id = ? AND rest_key = ?",
                        (org_id, rest_key)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def remove_chat_links(org_id, chat_id):
    conn = _db()
    cur = conn.execute("DELETE FROM restaurant_chats WHERE org_id = ? AND chat_id = ?", (org_id, chat_id))
    conn.commit()
    conn.close()
    return cur.rowcount


init_db()

# === НАСТРОЙКА БОТА ===
storage = MemoryStorage()
session = AiohttpSession(timeout=30)
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=storage)


class RegistrationStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()


class PeriodStates(StatesGroup):
    waiting_for_period = State()


user_cache = {}        # user_id -> данные пользователя
rest_cache = {}        # user_id -> (время, список ресторанов)
SEND_CACHE = OrderedDict()   # id -> отчёт, который можно отправить в чат ресторана
SEND_CACHE_LIMIT = 300


# === КЛАВИАТУРЫ ===
def get_main_keyboard():
    keyboard = [
        [KeyboardButton(text=REPORTS["day"]), KeyboardButton(text=REPORTS["week"])],
        [KeyboardButton(text=PERIOD_BUTTON), KeyboardButton(text=REPORTS["waiters"])],
        [KeyboardButton(text=REPORTS["plan"]), KeyboardButton(text="Сменить аккаунт")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


# === ПОЛЬЗОВАТЕЛИ И ТОКЕНЫ ===
class NotAuthorized(Exception):
    pass


async def get_user_data(user_id):
    if user_id in user_cache:
        return user_cache[user_id]
    data = get_user(user_id)
    if data:
        user_cache[user_id] = data
    return data


def is_token_valid(user_data):
    exp = user_data.get("token_expires_at") if user_data else None
    return bool(exp and exp > datetime.now() and user_data.get("access_token"))


def _apply_tokens(user_id, user_data, access, refresh=None):
    update_tokens(user_id, access, refresh)
    user_data["access_token"] = access
    if refresh:
        user_data["refresh_token"] = refresh
    user_data["token_expires_at"] = datetime.now() + TOKEN_TTL
    user_cache[user_id] = user_data


async def refresh_user_token(user_id, user_data):
    """
    Обновляет токены. Mozg при обмене выдаёт НОВУЮ пару, поэтому новый
    refresh-токен сохраняем: старый после обмена перестаёт работать.
    """
    logger.info(f"Обновляем токен для {user_id}")
    if user_data.get("refresh_token"):
        try:
            access, refresh = await asyncio.to_thread(mz.refresh_tokens, user_data["refresh_token"])
            _apply_tokens(user_id, user_data, access, refresh)
            return True
        except Exception as e:
            logger.warning(f"Не удалось обновить через refresh_token: {e}")

    # пароль есть только в памяти (в БД хранится его хэш), пока бот не перезапускали
    if user_data.get("password"):
        try:
            _, access, refresh = await asyncio.to_thread(
                mz.authenticate_and_get_org_id, user_data["login"], user_data["password"]
            )
            _apply_tokens(user_id, user_data, access, refresh)
            return True
        except Exception as e:
            logger.error(f"Не удалось войти по паролю: {e}")
    return False


async def get_valid_user(user_id):
    user_data = await get_user_data(user_id)
    if not user_data:
        raise NotAuthorized()
    if not is_token_valid(user_data):
        if not await refresh_user_token(user_id, user_data):
            raise NotAuthorized()
    return user_data


def _is_auth_error(e):
    if isinstance(e, mz.MozgAuthError):
        return True
    return isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 401


async def call_mozg(user_id, func, **kwargs):
    """
    Запускает функцию из main.py в отдельном потоке (чтобы бот не зависал),
    подставляя org_id и токен. При 401 обновляет токен и повторяет один раз.
    """
    for attempt in (1, 2):
        user_data = await get_valid_user(user_id)
        try:
            return await asyncio.to_thread(
                func, org_id=user_data["org_id"], token=user_data["access_token"], **kwargs
            )
        except Exception as e:
            if attempt == 1 and _is_auth_error(e):
                user_data["token_expires_at"] = None   # форсируем обновление
                continue
            if _is_auth_error(e):
                raise NotAuthorized()
            raise


async def get_restaurants_cached(user_id):
    cached = rest_cache.get(user_id)
    if cached and datetime.now() - cached[0] < timedelta(hours=1):
        return cached[1]
    rests = await call_mozg(user_id, mz.get_restaurants)
    rests = sorted(rests, key=lambda r: r["name"])
    rest_cache[user_id] = (datetime.now(), rests)
    return rests


NOT_AUTH_TEXT = "<b>Вы не авторизованы.</b>\nОтправьте /start для входа."


# === РЕГИСТРАЦИЯ ===
@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"/start от {user_id}")

    user_data = await get_user_data(user_id)
    if user_data:
        await message.answer(
            "<b>Добро пожаловать обратно!</b>\n\n"
            f"Пользователь: {html.escape(user_data['login'])}\n"
            f"Организация: {user_data['org_id']}\n\n"
            "Выберите отчет кнопками ниже.",
            reply_markup=get_main_keyboard(),
        )
        return

    await message.answer(
        "<b>Для начала работы нужно войти в Mozg.</b>\n\n"
        "Введите ваш логин (email) от аккаунта Mozg:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(RegistrationStates.waiting_for_login)


@dp.message(RegistrationStates.waiting_for_login)
async def process_login(message: Message, state: FSMContext):
    login = (message.text or "").strip()
    if "@" not in login or "." not in login:
        await message.answer("Похоже, это не email. Введите корректный email:")
        return
    await state.update_data(login=login)
    await message.answer("<b>Логин принят.</b>\n\nТеперь введите пароль от аккаунта Mozg:")
    await state.set_state(RegistrationStates.waiting_for_password)


@dp.message(RegistrationStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    user_id = message.from_user.id
    password = (message.text or "").strip()
    login = (await state.get_data()).get("login")

    # удаляем сообщение с паролем из чата
    try:
        await message.delete()
    except Exception:
        pass

    status = await message.answer("Проверяю данные и подключаюсь к Mozg...")
    try:
        org_id, token, refresh_token = await asyncio.to_thread(mz.authenticate_and_get_org_id, login, password)
        save_user(user_id, login, password, org_id, refresh_token, token)
        user_cache[user_id] = {
            "login": login, "password": password,
            "password_hash": hashlib.sha256(password.encode()).hexdigest(),
            "org_id": org_id, "access_token": token, "refresh_token": refresh_token,
            "token_expires_at": datetime.now() + TOKEN_TTL,
        }
        rest_cache.pop(user_id, None)
        await state.clear()
        await status.edit_text(
            "<b>Авторизация успешна.</b>\n\n"
            f"Пользователь: {html.escape(login)}\n"
            f"Организация: {org_id}"
        )
        await message.answer("Выберите отчет кнопками ниже.", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Ошибка авторизации для {user_id}: {e}")
        await status.edit_text(
            f"<b>Ошибка авторизации:</b>\n<pre>{html.escape(str(e))}</pre>\n"
            "Проверьте логин и пароль и отправьте /start."
        )
        await state.clear()


@dp.message(F.chat.type == "private", F.text == "Сменить аккаунт")
async def switch_account(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_cache.pop(user_id, None)
    rest_cache.pop(user_id, None)
    await message.answer(
        "<b>Сессия сброшена.</b>\n\nВведите логин (email) от Mozg:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(RegistrationStates.waiting_for_login)


# === ВЫБОР ОТЧЁТА И РЕСТОРАНА ===
@dp.message(F.chat.type == "private", F.text.in_(set(KIND_BY_TEXT)))
async def choose_restaurant(message: Message, state: FSMContext):
    await state.clear()   # если до этого ждали ввод периода - отменяем
    user_id = message.from_user.id
    kind = KIND_BY_TEXT[message.text]
    try:
        rests = await get_restaurants_cached(user_id)
    except NotAuthorized:
        return await message.answer(NOT_AUTH_TEXT)
    except Exception as e:
        logger.exception("Ошибка загрузки ресторанов")
        return await message.answer(f"<b>Не удалось получить список ресторанов:</b>\n<pre>{html.escape(str(e))}</pre>")

    if not rests:
        return await message.answer("Нет доступных ресторанов.")

    # Один ресторан - сразу показываем отчёт
    if len(rests) == 1:
        return await run_report(message, user_id, kind, "0")

    rows = [[InlineKeyboardButton(text=r["name"], callback_data=f"rep:{kind}:{i}")] for i, r in enumerate(rests)]
    rows.append([InlineKeyboardButton(text="Все рестораны", callback_data=f"rep:{kind}:all")])
    await message.answer(f"<b>{REPORTS[kind]}</b>\nВыберите ресторан:", reply_markup=inline(rows))


@dp.callback_query(F.data.startswith("rep:"))
async def cb_report(call: CallbackQuery):
    await call.answer()
    _, kind, target = call.data.split(":")
    await run_report(call.message, call.from_user.id, kind, target)


async def build_items(user_id, kind, selected, all_selected):
    """Возвращает список (ключ_ресторана, название, [сообщения])."""
    keys = None if all_selected else [r["key"] for r in selected]

    if kind == "day":
        data = await call_mozg(user_id, mz.fetch_full_analytics, rest_keys=keys)
        parts = mz.format_report_parts(data)
        return [(r.get("key"), r["name"], [p]) for r, p in zip(data["restaurants"], parts)]

    if kind == "week":
        data = await call_mozg(user_id, mz.fetch_weekly_analytics, rest_keys=keys)
        parts = mz.format_weekly_report_parts(data)
        return [(r.get("key"), r["name"], [p]) for r, p in zip(data["restaurants"], parts)]

    if kind == "plan":
        data = await call_mozg(user_id, mz.fetch_today_plan, rest_keys=keys)
        parts = mz.format_today_plan_parts(data)
        return [(r.get("key"), r["name"], [p]) for r, p in zip(data["restaurants"], parts)]

    if kind == "waiters":
        items = []
        for r in selected:
            report = await call_mozg(user_id, mz.fetch_waiters_report, rest_key=r["key"])
            items.append((r["key"], r["name"], mz.format_waiters_parts(report, r["name"])))
        return items

    return []


def remember_report(org_id, key, name, parts):
    sid = secrets.token_hex(4)
    SEND_CACHE[sid] = {"org_id": org_id, "key": key, "name": name, "parts": parts}
    while len(SEND_CACHE) > SEND_CACHE_LIMIT:
        SEND_CACHE.popitem(last=False)
    return sid


def error_text(e):
    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return (
            "<b>Сервер Mozg не отвечает</b> (обрыв соединения).\n"
            "Бот уже сделал несколько попыток. Повторите запрос через минуту."
        )
    return f"<b>Ошибка при получении данных:</b>\n<pre>{html.escape(str(e))}</pre>"


async def resolve_selected(user_id, target):
    """Возвращает (выбранные рестораны, выбраны_все) или None, если список изменился."""
    rests = await get_restaurants_cached(user_id)
    if target == "all":
        return rests, True
    idx = int(target)
    if idx >= len(rests):
        return None
    return [rests[idx]], False


async def deliver_items(message: Message, user_id, status, items):
    """Удаляет статус и отправляет каждый ресторан отдельным сообщением с кнопкой отправки в чат."""
    try:
        await status.delete()
    except Exception:
        pass

    if not items:
        return await message.answer("Нет данных за этот период.")

    user_data = await get_user_data(user_id)
    for key, name, parts in items:
        for i, part in enumerate(parts):
            markup = None
            if i == len(parts) - 1 and key:
                sid = remember_report(user_data["org_id"], key, name, parts)
                markup = inline([[InlineKeyboardButton(text="Отправить в чат ресторана", callback_data=f"send:{sid}")]])
            await message.answer(part, reply_markup=markup)
            await asyncio.sleep(0.05)


async def run_report(message: Message, user_id, kind, target):
    status = await message.answer("Собираю данные, это может занять до минуты...")
    try:
        resolved = await resolve_selected(user_id, target)
        if resolved is None:
            await status.delete()
            return await message.answer("Список ресторанов изменился. Выберите отчет заново.")
        selected, all_selected = resolved
        items = await build_items(user_id, kind, selected, all_selected)
    except NotAuthorized:
        await status.delete()
        return await message.answer(NOT_AUTH_TEXT)
    except Exception as e:
        logger.exception(f"Ошибка отчета {kind} для {user_id}")
        await status.delete()
        return await message.answer(error_text(e))

    await deliver_items(message, user_id, status, items)


# === ОТПРАВКА ОТЧЁТА В ЧАТ РЕСТОРАНА ===
@dp.callback_query(F.data.startswith("send:"))
async def cb_send(call: CallbackQuery):
    user_data = await get_user_data(call.from_user.id)
    if not user_data:
        return await call.answer("Сначала войдите: отправьте боту /start", show_alert=True)

    entry = SEND_CACHE.get(call.data.split(":", 1)[1])
    if not entry or entry["org_id"] != user_data["org_id"]:
        return await call.answer("Отчет устарел, сформируйте его заново", show_alert=True)

    chats = get_chat_links(entry["org_id"], entry["key"])
    if not chats:
        return await call.answer(
            "Чат этого ресторана не привязан. Добавьте бота в чат ресторана "
            "и отправьте там команду /link",
            show_alert=True,
        )

    sent = 0
    for chat_id in chats:
        try:
            for part in entry["parts"]:
                await bot.send_message(chat_id, part)
            sent += 1
        except Exception as e:
            logger.error(f"Не удалось отправить в чат {chat_id}: {e}")

    if sent:
        await call.answer(f"Отправлено в чат ресторана: {entry['name']}", show_alert=True)
    else:
        await call.answer("Не удалось отправить. Проверьте, что бот есть в чате ресторана.", show_alert=True)


# === ПРИВЯЗКА ЧАТА К РЕСТОРАНУ (команды отправляются в чате ресторана) ===
@dp.message(Command("link"))
async def cmd_link(message: Message):
    if message.chat.type == "private":
        return await message.answer(
            "Команду /link нужно отправить в групповом чате ресторана, "
            "куда добавлен бот."
        )
    user_id = message.from_user.id
    try:
        rests = await get_restaurants_cached(user_id)
    except NotAuthorized:
        return await message.answer("Сначала войдите в Mozg: напишите боту в личные сообщения /start")
    except Exception as e:
        logger.exception("Ошибка /link")
        return await message.answer(f"Не удалось получить рестораны: {html.escape(str(e))}")

    rows = [[InlineKeyboardButton(text=r["name"], callback_data=f"lnk:{r['key']}")] for r in rests]
    await message.answer("<b>Какой ресторан привязать к этому чату?</b>", reply_markup=inline(rows))


@dp.callback_query(F.data.startswith("lnk:"))
async def cb_link(call: CallbackQuery):
    user_data = await get_user_data(call.from_user.id)
    if not user_data:
        return await call.answer("Сначала войдите: напишите боту в личные сообщения /start", show_alert=True)

    key = call.data.split(":", 1)[1]
    try:
        rests = await get_restaurants_cached(call.from_user.id)
    except Exception:
        rests = []
    name = next((r["name"] for r in rests if r["key"] == key), key)

    add_chat_link(user_data["org_id"], key, call.message.chat.id)
    await call.answer("Готово")
    await call.message.edit_text(f"<b>Чат привязан к ресторану:</b> {html.escape(name)}")


@dp.message(Command("unlink"))
async def cmd_unlink(message: Message):
    user_data = await get_user_data(message.from_user.id)
    if not user_data:
        return await message.answer("Сначала войдите в Mozg: напишите боту в личные сообщения /start")
    removed = remove_chat_links(user_data["org_id"], message.chat.id)
    await message.answer("Привязка этого чата снята." if removed else "К этому чату ничего не привязано.")


# === ОТЧЁТ ЗА ПРОИЗВОЛЬНЫЙ ПЕРИОД ===
DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})")
MAX_PERIOD_DAYS = 366


def parse_period(text):
    """'15.10.26 - 20.10.26' -> (datetime, datetime). Одна дата = один день."""
    found = DATE_RE.findall(text or "")
    if not found:
        raise ValueError("Не нашёл даты в сообщении.")
    if len(found) > 2:
        raise ValueError("Укажите две даты: начало и конец периода.")

    dates = []
    for d, m, y in found:
        year = int(y)
        if year < 100:
            year += 2000
        try:
            dates.append(datetime(year, int(m), int(d)))
        except ValueError:
            raise ValueError(f"Такой даты не существует: {d}.{m}.{y}")

    d1, d2 = dates[0], dates[-1]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if d1 > d2:
        raise ValueError("Начало периода позже его конца.")
    if d2 > today:
        raise ValueError("Конец периода не может быть в будущем.")
    if (d2 - d1).days + 1 > MAX_PERIOD_DAYS:
        raise ValueError(f"Период слишком длинный (максимум {MAX_PERIOD_DAYS} дн.).")
    return d1, d2


def period_label(d1, d2):
    return f"{d1:%d.%m.%Y}" if d1 == d2 else f"{d1:%d.%m.%Y} - {d2:%d.%m.%Y}"


@dp.message(F.chat.type == "private", F.text == PERIOD_BUTTON)
async def ask_period(message: Message, state: FSMContext):
    if not await get_user_data(message.from_user.id):
        return await message.answer(NOT_AUTH_TEXT)
    await state.set_state(PeriodStates.waiting_for_period)
    await message.answer(
        "<b>Отчет за период</b>\n\n"
        "Введите даты в формате:\n"
        "<code>15.10.26 - 20.10.26</code>\n\n"
        "Одна дата - отчет за один день.\n"
        "Чтобы отменить, нажмите любую кнопку меню."
    )


@dp.message(PeriodStates.waiting_for_period, F.chat.type == "private")
async def process_period(message: Message, state: FSMContext):
    try:
        d1, d2 = parse_period(message.text)
    except ValueError as e:
        return await message.answer(
            f"{html.escape(str(e))}\nПример: <code>15.10.26 - 20.10.26</code>"
        )
    await state.clear()

    user_id = message.from_user.id
    try:
        rests = await get_restaurants_cached(user_id)
    except NotAuthorized:
        return await message.answer(NOT_AUTH_TEXT)
    except Exception as e:
        logger.exception("Ошибка загрузки ресторанов")
        return await message.answer(error_text(e))
    if not rests:
        return await message.answer("Нет доступных ресторанов.")

    f, t = d1.strftime("%Y%m%d"), d2.strftime("%Y%m%d")
    if len(rests) == 1:
        return await run_period_report(message, user_id, f, t, "0")

    rows = [[InlineKeyboardButton(text=r["name"], callback_data=f"per:{f}:{t}:{i}")] for i, r in enumerate(rests)]
    rows.append([InlineKeyboardButton(text="Все рестораны", callback_data=f"per:{f}:{t}:all")])
    await message.answer(
        f"<b>Отчет за период {period_label(d1, d2)}</b>\nВыберите ресторан:",
        reply_markup=inline(rows),
    )


@dp.callback_query(F.data.startswith("per:"))
async def cb_period(call: CallbackQuery):
    await call.answer()
    _, f, t, target = call.data.split(":")
    await run_period_report(call.message, call.from_user.id, f, t, target)


async def run_period_report(message: Message, user_id, f, t, target):
    d1, d2 = datetime.strptime(f, "%Y%m%d"), datetime.strptime(t, "%Y%m%d")
    status = await message.answer(f"Собираю данные за {period_label(d1, d2)}, это может занять до минуты...")
    try:
        resolved = await resolve_selected(user_id, target)
        if resolved is None:
            await status.delete()
            return await message.answer("Список ресторанов изменился. Выберите отчет заново.")
        selected, all_selected = resolved

        data = await call_mozg(
            user_id, mz.fetch_period_analytics,
            date_from=d1.strftime("%Y-%m-%d"), date_to=d2.strftime("%Y-%m-%d"),
            rest_keys=None if all_selected else [r["key"] for r in selected],
        )
        parts = mz.format_period_report_parts(data)
        items = [(r.get("key"), r["name"], [p]) for r, p in zip(data["restaurants"], parts)]
    except NotAuthorized:
        await status.delete()
        return await message.answer(NOT_AUTH_TEXT)
    except Exception as e:
        logger.exception(f"Ошибка отчета за период для {user_id}")
        await status.delete()
        return await message.answer(error_text(e))

    await deliver_items(message, user_id, status, items)


@dp.message(F.chat.type == "private")
async def handle_unknown(message: Message):
    if await get_user_data(message.from_user.id):
        await message.answer("Используйте кнопки меню.", reply_markup=get_main_keyboard())
    else:
        await message.answer("Отправьте /start для начала работы.")


# === ЗАПУСК БОТА ===
async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot, polling_timeout=20)


if __name__ == "__main__":
    asyncio.run(main())

    # prod by D.Pankratov and M.Buanov