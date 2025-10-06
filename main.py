#!/usr/bin/env python3
import os
import stat
import io
import logging
import asyncio
import shutil
from datetime import datetime, date
from typing import Dict, List, Tuple

import httpx
import aiosqlite
from dotenv import load_dotenv

import base64
import pdfplumber

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_IDS = {1647999523}
DONATION_ALERTS_LINK = os.getenv("DONATION_ALERTS_LINK", "https://www.donationalerts.com/r/your_username")
DB_PATH = "/app/data/bot.db"

# Ensure the directory for the database exists and has correct permissions
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.chmod(os.path.dirname(DB_PATH), stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
    logger.info(f"Created directory {os.path.dirname(DB_PATH)} with permissions {oct(os.stat(os.path.dirname(DB_PATH)).st_mode)}")
    logger.info(f"Current working directory: {os.getcwd()}")
except Exception as e:
    logger.error(f"Failed to create or set permissions for {os.path.dirname(DB_PATH)}: {e}")
    raise

# Configure logging to both console and file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/data/bot.log", mode='a')
    ]
)
logger = logging.getLogger(__name__)

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("TELEGRAM_TOKEN or OPENROUTER_API_KEY not found in .env file")

# Initialize OpenAI client with OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY
)

# Initialize Bot and Dispatcher
bot = Bot(TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Constants
MAX_REQUESTS_PER_DAY = 50
PREMIUM_MAX_REQUESTS = 500
REQUEST_LIMIT_RESET_DAYS = 7

async def init_db():
    """Initialize the database and create tables"""
    logger.info(f"Attempting to connect to database at {DB_PATH}")
    logger.info(f"Directory exists: {os.path.exists(os.path.dirname(DB_PATH))}")
    logger.info(f"Database file exists: {os.path.exists(DB_PATH)}")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            logger.info("Successfully connected to database")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_seen TEXT,
                    last_seen TEXT,
                    message_count INTEGER DEFAULT 0,
                    photo_count INTEGER DEFAULT 0,
                    document_count INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    question TEXT,
                    answer TEXT,
                    timestamp TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_requests (
                    user_id INTEGER PRIMARY KEY,
                    date TEXT,
                    count INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_state (
                    user_id INTEGER PRIMARY KEY,
                    state TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS premium_users (
                    user_id INTEGER PRIMARY KEY
                )
            """)
            await db.commit()
            logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database at {DB_PATH}: {e}")
        raise

async def update_user_stats(user_id: int, message_type: str):
    """Update user statistics in the database"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"INSERT OR REPLACE INTO users (user_id, last_seen) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen = ?",
            (user_id, datetime.now().isoformat(), datetime.now().isoformat())
        )
        await db.execute(
            f"UPDATE users SET {message_type}_count = {message_type}_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def update_request_count(user_id: int):
    """Update daily request count for the user"""
    if user_id in ADMIN_IDS:
        return
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT date, count FROM user_requests WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row:
            db_date, count = row
            if db_date != today:
                await db.execute("UPDATE user_requests SET date = ?, count = 1 WHERE user_id = ?", (today, user_id))
            else:
                await db.execute("UPDATE user_requests SET count = count + 1 WHERE user_id = ?", (user_id,))
        else:
            await db.execute("INSERT INTO user_requests (user_id, date, count) VALUES (?, ?, 1)", (user_id, today))
        await db.commit()

async def get_request_count(user_id: int) -> int:
    """Get the current request count for the user"""
    if user_id in ADMIN_IDS:
        return float('inf')
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT count FROM user_requests WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def is_premium_user(user_id: int) -> bool:
    """Check if the user is a premium user"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM premium_users WHERE user_id = ?", (user_id,))
        return (await cursor.fetchone()) is not None

async def get_user_max_requests(user_id: int) -> int:
    """Get the maximum number of requests for the user"""
    return PREMIUM_MAX_REQUESTS if await is_premium_user(user_id) else MAX_REQUESTS_PER_DAY

async def save_memory(user_id: int, question: str, answer: str):
    """Save user memory to the database"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_memory (user_id, question, answer, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, question, answer, datetime.now().isoformat())
        )
        await db.commit()

async def get_memory(user_id: int) -> List[Tuple[str, str]]:
    """Retrieve user memory from the database"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT question, answer FROM user_memory WHERE user_id = ? ORDER BY timestamp DESC", (user_id,))
        return await cursor.fetchall()

async def clear_memory(user_id: int):
    """Clear user memory from the database"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))
        await db.commit()

async def backup_db():
    """Create a backup of the database"""
    backup_path = "/app/data/bot_backup.db"
    try:
        shutil.copy2(DB_PATH, backup_path)
        return backup_path
    except Exception as e:
        logger.error(f"Failed to create backup: {e}")
        return None

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    await update_user_stats(user_id, "message_count")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton(text="💰 Поддержать проект", url=DONATION_ALERTS_LINK)]
    ])
    await message.answer("👋 Привет! Я бот с искусственным интеллектом. Чем могу помочь?\n\n"
                         "📌 Используйте /help для списка команд.", reply_markup=keyboard)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("📋 Список команд:\n"
                         "/start - Начать работу с ботом\n"
                         "/help - Показать список команд\n"
                         "/clear - Очистить историю переписки\n"
                         "/profile - Показать личный кабинет\n"
                         "/backup_db - Создать резервную копию базы данных (только для админов)\n"
                         "/get_logs - Получить лог-файл (только для админов)\n"
                         "/check_volume - Проверить состояние тома (только для админов)\n"
                         "(админ-панель: /admpanel)")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    user_id = message.from_user.id
    await clear_memory(user_id)
    await message.answer("🧹 История переписки очищена!")

@dp.message(Command("admpanel"))
async def cmd_admpanel(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⛔️ У вас нет доступа к админ-панели.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Дать премиум", callback_data="give_premium")],
        [InlineKeyboardButton(text="❌ Забрать премиум", callback_data="revoke_premium")],
        [InlineKeyboardButton(text="📤 Создать резервную копию", callback_data="backup_db")]
    ])
    await message.answer("👮‍♂️ Админ-панель:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "give_premium")
async def process_give_premium(callback: types.CallbackQuery):
    await callback.answer("📝 Введите ID пользователя, которому хотите дать премиум:")
    await callback.message.edit_reply_markup()

@dp.callback_query(lambda c: c.data == "revoke_premium")
async def process_revoke_premium(callback: types.CallbackQuery):
    await callback.answer("📝 Введите ID пользователя, у которого хотите забрать премиум:")
    await callback.message.edit_reply_markup()

@dp.callback_query(lambda c: c.data == "backup_db")
async def process_backup_db(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in ADMIN_IDS:
        await callback.answer("⛔️ У вас нет доступа к этой функции.")
        return
    backup_path = await backup_db()
    if backup_path:
        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(backup_path, filename="bot_backup.db"),
            caption="📦 Резервная копия базы данных"
        )
    else:
        await callback.answer("⚠️ Не удалось создать резервную копию.")
    await callback.message.delete()

@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    await update_user_stats(user_id, "message_count")
    await update_request_count(user_id)
    request_count = await get_request_count(user_id)
    max_requests = await get_user_max_requests(user_id)

    if request_count >= max_requests:
        await message.answer("⏳ Лимит запросов исчерпан. Попробуйте завтра или станьте премиум-пользователем!")
        return

    await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    memory = await get_memory(user_id)
    memory_context = "\n".join([f"Q: {q}\nA: {a}" for q, a in memory[-5:]]) if memory else ""

    response = client.chat.completions.create(
        model="mistralai/mixtral-8x7b-instruct",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": memory_context + "\n" + message.text}
        ],
        max_tokens=500
    )
    answer = response.choices[0].message.content.strip()

    await message.answer(answer)
    await save_memory(user_id, message.text, answer)

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    await update_user_stats(user_id, "photo_count")
    await update_request_count(user_id)
    request_count = await get_request_count(user_id)
    max_requests = await get_user_max_requests(user_id)

    if request_count >= max_requests:
        await message.answer("⏳ Лимит запросов исчерпан. Попробуйте завтра или станьте премиум-пользователем!")
        return

    await bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_PHOTO)
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    content = base64.b64encode(await bot.download_file(file.file_path)).decode("utf-8")

    response = client.chat.completions.create(
        model="xai/grok",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "Describe this image:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{content}"}}
        ]}],
        max_tokens=500
    )
    answer = response.choices[0].message.content.strip()

    await message.answer(answer)
    await save_memory(user_id, "Image description request", answer)

@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    await update_user_stats(user_id, "document_count")
    await update_request_count(user_id)
    request_count = await get_request_count(user_id)
    max_requests = await get_user_max_requests(user_id)

    if request_count >= max_requests:
        await message.answer("⏳ Лимит запросов исчерпан. Попробуйте завтра или станьте премиум-пользователем!")
        return

    await bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_DOCUMENT)
    file = await bot.get_file(message.document.file_id)
    file_content = await bot.download_file(file.file_path)

    if message.document.file_name.endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(file_content.read())) as pdf:
            text = "\n".join(page.extract_text() for page in pdf.pages if page.extract_text())
            response = client.chat.completions.create(
                model="mistralai/mixtral-8x7b-instruct",
                messages=[{"role": "user", "content": f"Analyze this PDF content:\n{text}"}],
                max_tokens=500
            )
            answer = response.choices[0].message.content.strip()
    else:
        answer = "⚠️ Поддерживаются только PDF-файлы."

    await message.answer(answer)
    await save_memory(user_id, f"Document ({message.document.file_name}) analysis", answer)

@dp.callback_query(F.data == "profile")
async def process_profile(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    request_count = await get_request_count(user_id)
    max_requests = await get_user_max_requests(user_id)
    status = "premium" if await is_premium_user(user_id) else "обычный"
    status_text = "(premium)" if await is_premium_user(user_id) else ""
    if user_id in ADMIN_IDS:
        status = "админ"
        status_text = "(админ)"
        request_count = "∞"
        max_requests = "∞"
    await callback.message.edit_text(
        f"👤 Личный кабинет:\n"
        f"Статус: {status}\n"
        f"Остаток запросов: {max_requests - request_count} из {max_requests} {status_text}"
    )

@dp.message(Command("check_volume"))
async def cmd_check_volume(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        return
    try:
        dir_path = "/app/data"
        files = os.listdir(dir_path) if os.path.exists(dir_path) else []
        perms = oct(os.stat(dir_path).st_mode & 0o777) if os.path.exists(dir_path) else "N/A"
        db_exists = os.path.exists(DB_PATH)
        response = (
            f"📂 Volume check for {dir_path}:\n"
            f"Files: {files}\n"
            f"Permissions: {perms}\n"
            f"Database exists: {db_exists}\n"
            f"Volume mount path: {os.getenv('RAILWAY_VOLUME_MOUNT_PATH', 'Not set')}"
        )
        await message.answer(response)
    except Exception as e:
        await message.answer(f"⚠️ Error checking volume: {e}")

@dp.message(Command("get_logs"))
async def cmd_get_logs(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        return
    log_path = "/app/data/bot.log"
    if not os.path.exists(log_path):
        await message.answer("⚠️ Файл логов не найден.")
        return
    await bot.send_document(
        chat_id=user_id,
        document=FSInputFile(log_path, filename="bot.log"),
        caption="📜 Логи бота"
    )

async def main():
    logger.info("Bot is starting...")
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
