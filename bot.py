import asyncio
import sqlite3
import hashlib
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from main import (
    fetch_full_analytics, 
    format_report, 
    fetch_weekly_analytics,
    format_weekly_report,
    authenticate_and_get_org_id, 
    get_access_token,
    refresh_access_token
)

# === НАСТРОЙКА ЛОГГИРОВАНИЯ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8955041911:AAE2-O49ENIhXPuC8-3datH-yyJyuJ2bZMw"

# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ===
def init_db():
    """Создает таблицу пользователей, если ее нет."""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
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
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def save_user(user_id, login, password, org_id, refresh_token, access_token):
    """Сохраняет или обновляет пользователя в БД."""
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    expires_at = datetime.now() + timedelta(hours=1)
    
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users 
        (user_id, login, password_hash, org_id, refresh_token, access_token, token_expires_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, login, password_hash, org_id, refresh_token, access_token, expires_at))
    conn.commit()
    conn.close()
    logger.info(f"✅ Пользователь {user_id} сохранен в БД")

def get_user(user_id):
    """Получает данные пользователя из БД."""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT login, password_hash, org_id, refresh_token, access_token, token_expires_at 
        FROM users WHERE user_id = ?
    ''', (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "login": row[0],
            "password_hash": row[1],
            "org_id": row[2],
            "refresh_token": row[3],
            "access_token": row[4],
            "token_expires_at": datetime.fromisoformat(row[5]) if row[5] else None
        }
    return None

def update_access_token(user_id, new_token):
    """Обновляет access_token в БД."""
    expires_at = datetime.now() + timedelta(hours=1)
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET access_token = ?, token_expires_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    ''', (new_token, expires_at, user_id))
    conn.commit()
    conn.close()
    logger.info(f"✅ Access token обновлен для пользователя {user_id}")

# Инициализируем БД
init_db()

# === НАСТРОЙКА БОТА ===
storage = MemoryStorage()
session = AiohttpSession(timeout=30)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=storage)

# === Класс состояний ===
class RegistrationStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

# === Кэш пользователей ===
user_cache = {}

# === КЛАВИАТУРЫ ===
def get_main_keyboard():
    keyboard = [
        [KeyboardButton(text="📊 Отчет за сегодня")],
        [KeyboardButton(text="📈 Отчет за неделю")],
        [KeyboardButton(text="🔄 Сменить аккаунт")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
async def ping_user(message: Message, text: str, emoji: str = "⏳"):
    """Отправляет пользователю уведомление о действии."""
    await message.answer(f"{emoji} {text}")

async def get_user_data(user_id):
    """Получает данные пользователя из кэша или БД."""
    if user_id in user_cache:
        return user_cache[user_id]
    
    user_data = get_user(user_id)
    if user_data:
        user_cache[user_id] = user_data
    return user_data

def is_token_valid(user_data):
    """Проверяет, истек ли access_token."""
    if not user_data or not user_data.get("token_expires_at"):
        return False
    return user_data["token_expires_at"] > datetime.now()

async def refresh_user_token(user_id, user_data):
    """Обновляет токен пользователя."""
    logger.info(f"🔄 Токен для {user_id} истек, обновляем...")
    
    try:
        # Пытаемся обновить через refresh_token
        if user_data.get("refresh_token"):
            try:
                new_token = refresh_access_token(user_data["refresh_token"])
                update_access_token(user_id, new_token)
                user_data["access_token"] = new_token
                user_cache[user_id] = user_data
                logger.info(f"✅ Токен обновлен через refresh_token для {user_id}")
                return True
            except Exception as e:
                logger.warning(f"⚠️ Не удалось обновить через refresh_token: {e}")
        
        # Если refresh_token не работает, используем пароль
        if user_data.get("password"):
            new_token = get_access_token(user_data["login"], user_data["password"])
            update_access_token(user_id, new_token)
            user_data["access_token"] = new_token
            user_cache[user_id] = user_data
            logger.info(f"✅ Токен обновлен через пароль для {user_id}")
            return True
        
        logger.error(f"❌ Нет способа обновить токен для {user_id}")
        return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка обновления токена для {user_id}: {e}")
        return False

# === ОБРАБОТЧИКИ КОМАНД ===
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработка команды /start."""
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    
    logger.info(f"📩 Команда /start от {user_id} (@{username})")
    await ping_user(message, "Запускаю бота...", "🚀")
    
    user_data = await get_user_data(user_id)
    
    if user_data:
        logger.info(f"✅ Пользователь {user_id} уже зарегистрирован")
        await message.answer(
            "👋 **Добро пожаловать обратно!**\n\n"
            f"👤 Пользователь: {user_data['login']}\n"
            f"🏢 Организация: {user_data['org_id']}\n\n"
            "Используйте кнопки ниже для получения отчетов.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    await message.answer(
        "🔐 **Для начала работы необходимо авторизоваться в Mozg.**\n\n"
        "Пожалуйста, введите ваш **логин (email)** от аккаунта Mozg:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_for_login)

@dp.message(RegistrationStates.waiting_for_login)
async def process_login(message: Message, state: FSMContext):
    """Обработка введенного логина."""
    user_id = message.from_user.id
    login = message.text.strip()
    
    logger.info(f"📩 Пользователь {user_id} ввел логин: {login}")
    await ping_user(message, "Проверяю логин...", "🔍")
    
    if "@" not in login or "." not in login:
        await message.answer(
            "❌ Похоже, это не email. Пожалуйста, введите корректный email:"
        )
        return
    
    await state.update_data(login=login)
    await message.answer(
        "✅ **Логин принят!**\n\n"
        "Теперь введите ваш **пароль** от аккаунта Mozg:",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_for_password)

@dp.message(RegistrationStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    """Обработка введенного пароля и попытка авторизации."""
    user_id = message.from_user.id
    password = message.text.strip()
    user_data = await state.get_data()
    login = user_data.get("login")
    
    logger.info(f"📩 Пользователь {user_id} ввел пароль (попытка авторизации)")
    await ping_user(message, "Проверяю данные и подключаюсь к Mozg...", "⏳")
    
    try:
        org_id, token, refresh_token = authenticate_and_get_org_id(login, password)
        
        logger.info(f"✅ Пользователь {user_id} успешно авторизован. Org ID: {org_id}")
        
        save_user(user_id, login, password, org_id, refresh_token, token)
        
        user_cache[user_id] = {
            "login": login,
            "password": password,
            "password_hash": hashlib.sha256(password.encode()).hexdigest(),
            "org_id": org_id,
            "access_token": token,
            "refresh_token": refresh_token,
            "token_expires_at": datetime.now() + timedelta(hours=1)
        }
        
        await state.clear()
        await message.answer(
            f"✅ **Авторизация успешна!**\n\n"
            f"👤 Пользователь: {login}\n"
            f"🏢 Организация ID: {org_id}\n"
            f"🔑 Токен получен и сохранен\n\n"
            f"Теперь вы можете использовать бота для получения аналитики.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown"
        )
        
        logger.info(f"✅ Пользователь {user_id} завершил регистрацию")
        
    except Exception as e:
        logger.error(f"❌ Ошибка авторизации для {user_id}: {e}")
        await message.answer(
            f"❌ **Ошибка авторизации:**\n"
            f"```\n{str(e)}\n```\n\n"
            f"Пожалуйста, проверьте логин и пароль и попробуйте снова.\n"
            f"Для повторной регистрации отправьте команду /start",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(F.text == "📊 Отчет за сегодня")
async def send_report(message: Message):
    """Отправка отчета за актуальную дату."""
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    
    logger.info(f"📩 Запрос ежедневного отчета от {user_id} (@{username})")
    await ping_user(message, "Начинаю сбор данных за сегодня...", "📊")
    
    user_data = await get_user_data(user_id)
    if not user_data:
        logger.warning(f"⚠️ Пользователь {user_id} не авторизован")
        await message.answer(
            "❌ **Вы не авторизованы.**\n"
            "Отправьте /start для входа."
        )
        return
    
    # Проверяем и обновляем токен
    if not is_token_valid(user_data):
        await ping_user(message, "Токен истек, обновляю...", "🔄")
        success = await refresh_user_token(user_id, user_data)
        if not success:
            await message.answer(
                "❌ **Ошибка обновления токена.**\n"
                "Пожалуйста, выполните повторную регистрацию через /start"
            )
            return
        user_data = await get_user_data(user_id)
    
    await ping_user(message, "Получаю данные из Mozg...", "⏳")
    
    try:
        analytics = fetch_full_analytics(
            login=user_data["login"],
            password=user_data.get("password", ""),
            org_id=user_data["org_id"],
            token=user_data["access_token"]
        )
        
        report_text = format_report(analytics)
        
        logger.info(f"✅ Ежедневный отчет получен для {user_id} ({len(report_text)} символов)")
        
        await ping_user(message, "Отчет готов! Отправляю...", "📨")
        
        if len(report_text) > 4000:
            parts = [report_text[i:i+4000] for i in range(0, len(report_text), 4000)]
            for part in parts:
                await message.answer(part, parse_mode="Markdown")
        else:
            await message.answer(report_text, parse_mode="Markdown")
        
        await ping_user(message, "Отчет успешно доставлен! ✅", "✅")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении данных для {user_id}: {e}")
        await message.answer(f"❌ **Ошибка при получении данных:**\n```\n{str(e)}\n```", parse_mode="Markdown")

@dp.message(F.text == "📈 Отчет за неделю")
async def send_week_report(message: Message):
    """Отправка отчета за последние 7 дней в сравнении с прошлым годом."""
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    
    logger.info(f"📩 Запрос недельного отчета от {user_id} (@{username})")
    await ping_user(message, "Начинаю сбор данных за неделю...", "📊")
    
    user_data = await get_user_data(user_id)
    if not user_data:
        logger.warning(f"⚠️ Пользователь {user_id} не авторизован")
        await message.answer(
            "❌ **Вы не авторизованы.**\n"
            "Отправьте /start для входа."
        )
        return
    
    # Проверяем и обновляем токен
    if not is_token_valid(user_data):
        await ping_user(message, "Токен истек, обновляю...", "🔄")
        success = await refresh_user_token(user_id, user_data)
        if not success:
            await message.answer(
                "❌ **Ошибка обновления токена.**\n"
                "Пожалуйста, выполните повторную регистрацию через /start"
            )
            return
        user_data = await get_user_data(user_id)
    
    await ping_user(message, "Получаю данные из Mozg...", "⏳")
    
    try:
        analytics = fetch_weekly_analytics(
            login=user_data["login"],
            password=user_data.get("password", ""),
            org_id=user_data["org_id"],
            token=user_data["access_token"]
        )
        
        report_text = format_weekly_report(analytics)
        
        logger.info(f"✅ Недельный отчет получен для {user_id} ({len(report_text)} символов)")
        
        await ping_user(message, "Отчет готов! Отправляю...", "📨")
        
        if len(report_text) > 4000:
            parts = [report_text[i:i+4000] for i in range(0, len(report_text), 4000)]
            for part in parts:
                await message.answer(part, parse_mode="Markdown")
        else:
            await message.answer(report_text, parse_mode="Markdown")
        
        await ping_user(message, "Отчет успешно доставлен! ✅", "✅")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении недельных данных для {user_id}: {e}")
        await message.answer(f"❌ **Ошибка при получении данных:**\n```\n{str(e)}\n```", parse_mode="Markdown")

@dp.message(F.text == "🔄 Сменить аккаунт")
async def switch_account(message: Message, state: FSMContext):
    """Смена аккаунта."""
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    
    logger.info(f"🔄 Смена аккаунта для {user_id} (@{username})")
    await ping_user(message, "Сбрасываю сессию...", "🔄")
    
    if user_id in user_cache:
        del user_cache[user_id]
    
    await message.answer(
        "🔄 **Сессия сброшена.**\n\n"
        "Введите ваш логин (email) от Mozg для повторной регистрации:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationStates.waiting_for_login)
    
    logger.info(f"✅ Сессия сброшена для {user_id}")

@dp.message()
async def handle_unknown(message: Message):
    """Обработка неизвестных сообщений."""
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)
    
    if user_data:
        await message.answer(
            "Используйте кнопки меню для навигации.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "Отправьте /start для начала работы."
        )

# === ЗАПУСК БОТА ===
async def main():
    logger.info("🤖 Бот запущен и готов к работе!")
    logger.info("📁 База данных: users.db")
    logger.info("👥 Поддерживается несколько пользователей одновременно")
    logger.info("📊 Доступные команды:")
    logger.info("   📊 Отчет за сегодня")
    logger.info("   📈 Отчет за неделю")
    logger.info("   🔄 Сменить аккаунт")
    
    await dp.start_polling(bot, polling_timeout=20)

if __name__ == "__main__":
    asyncio.run(main())


    #prod by D.Pankratov and M.Buanov
    