#!/usr/bin/env python3
import os
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
ADMIN_IDS = {}
DONATION_ALERTS_LINK = os.getenv("DONATION_ALERTS_LINK", "https://www.donationalerts.com/r/your_username")
DB_PATH = "bot.db"

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("TELEGRAM_TOKEN or OPENROUTER_API_KEY not found in .env file")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    http_client=httpx.Client(timeout=30.0)
)

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MEMORY_LIMIT = 10
REQUEST_LIMIT = 50
PREMIUM_REQUEST_LIMIT = 500

# Клавиатуры
main_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Решить текст", callback_data="btn_solve_text"),
         InlineKeyboardButton(text="📸 Решить фото", callback_data="btn_solve_photo")],
        [InlineKeyboardButton(text="📚 Конспект", callback_data="btn_conspект")],
        [InlineKeyboardButton(text="🗑 Очистить память", callback_data="btn_clear_memory"),
         InlineKeyboardButton(text="👤 Личный кабинет", callback_data="btn_profile")]
    ]
)

profile_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="btn_back_main")]
    ]
)

cancel_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="btn_cancel")]
    ]
)

admin_main_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика пользователей", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_users")],
        [InlineKeyboardButton(text="📢 Создать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔍 Просмотр памяти пользователя", callback_data="admin_user_memory")],
        [InlineKeyboardButton(text="📈 Активность пользователей", callback_data="admin_activity")],
        [InlineKeyboardButton(text="📦 Создать резервную копию", callback_data="admin_backup_db")],
        [InlineKeyboardButton(text="💎 Дать премиум", callback_data="admin_add_premium")],
        [InlineKeyboardButton(text="🗑 Забрать премиум", callback_data="admin_remove_premium")],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="admin_back_main")]
    ]
)

admin_broadcast_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить рассылку", callback_data="admin_confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="admin_cancel_broadcast")]
    ]
)

admin_back_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в админ-панель", callback_data="admin_back")]
    ]
)

admin_broadcast_state: Dict[int, str] = {}

# Функции для работы с базой данных
async def init_db():
    """Инициализация базы данных и создание таблиц"""
    async with aiosqlite.connect(DB_PATH) as db:
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
        logger.info("Database initialized")


async def migrate_from_dicts(user_stats: Dict, user_memory: Dict, user_requests: Dict, user_state: Dict):
    """Миграция данных из словарей в SQLite"""
    async with aiosqlite.connect(DB_PATH) as db:
        for user_id, stats in user_stats.items():
            await db.execute("""
                INSERT OR REPLACE INTO users (user_id, first_seen, last_seen, message_count, photo_count, document_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, stats["first_seen"], stats["last_seen"], stats["message_count"],
                  stats["photo_count"], stats["document_count"]))

        for user_id, memories in user_memory.items():
            for question, answer in memories:
                await db.execute("""
                    INSERT INTO user_memory (user_id, question, answer, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (user_id, question, answer, datetime.now().isoformat()))

        for user_id, req in user_requests.items():
            await db.execute("""
                INSERT OR REPLACE INTO user_requests (user_id, date, count)
                VALUES (?, ?, ?)
            """, (user_id, req["date"], req["count"]))

        for user_id, state in user_state.items():
            await db.execute("""
                INSERT OR REPLACE INTO user_state (user_id, state)
                VALUES (?, ?)
            """, (user_id, state))

        await db.commit()
        logger.info("Data migrated from dictionaries to database")


async def update_user_stats(user_id: int, message_type: str = "text"):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()

        if not user:
            await db.execute("""
                INSERT INTO users (user_id, first_seen, last_seen, message_count, photo_count, document_count)
                VALUES (?, ?, ?, 0, 0, 0)
            """, (user_id, now, now))

        if message_type == "text":
            await db.execute("UPDATE users SET message_count = message_count + 1, last_seen = ? WHERE user_id = ?",
                             (now, user_id))
        elif message_type == "photo":
            await db.execute("UPDATE users SET photo_count = photo_count + 1, last_seen = ? WHERE user_id = ?",
                             (now, user_id))
        elif message_type == "document":
            await db.execute("UPDATE users SET document_count = document_count + 1, last_seen = ? WHERE user_id = ?",
                             (now, user_id))
        await db.commit()


async def get_user_stats(user_id: int) -> Dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        if user:
            return {
                "user_id": user[0],
                "first_seen": user[1],
                "last_seen": user[2],
                "message_count": user[3],
                "photo_count": user[4],
                "document_count": user[5]
            }
        return {}


async def get_all_users_stats() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT * FROM users")
        rows = await cursor.fetchall()
        return [{
            "user_id": row[0],
            "first_seen": row[1],
            "last_seen": row[2],
            "message_count": row[3],
            "photo_count": row[4],
            "document_count": row[5]
        } for row in rows]


async def add_premium_user(user_id: int):
    """Добавление пользователя в премиум-группу"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO premium_users (user_id) VALUES (?)", (user_id,))
        await db.commit()
        logger.info(f"User {user_id} added to premium group")


async def remove_premium_user(user_id: int):
    """Удаление пользователя из премиум-группы"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
        await db.commit()
        logger.info(f"User {user_id} removed from premium group")


async def is_premium_user(user_id: int) -> bool:
    """Проверка, является ли пользователь премиум"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM premium_users WHERE user_id = ?", (user_id,))
        result = await cursor.fetchone()
        return bool(result)


async def save_memory(user_id: int, question: str, answer: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO user_memory (user_id, question, answer, timestamp)
            VALUES (?, ?, ?, ?)
        """, (user_id, question, answer, datetime.now().isoformat()))

        cursor = await db.execute("SELECT id FROM user_memory WHERE user_id = ? ORDER BY timestamp ASC", (user_id,))
        memories = await cursor.fetchall()
        if len(memories) > MEMORY_LIMIT:
            for mem_id in memories[:-MEMORY_LIMIT]:
                await db.execute("DELETE FROM user_memory WHERE id = ?", (mem_id[0],))
        await db.commit()


async def get_memory(user_id: int) -> List[Tuple[str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT question, answer FROM user_memory WHERE user_id = ? ORDER BY timestamp DESC",
                                  (user_id,))
        return await cursor.fetchall()


async def clear_memory(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))
        await db.commit()


async def update_request_count(user_id: int):
    if user_id in ADMIN_IDS:
        return  # Админы не учитываются в лимите запросов
    async with aiosqlite.connect(DB_PATH) as db:
        today = date.today().isoformat()
        cursor = await db.execute("SELECT count, date FROM user_requests WHERE user_id = ?", (user_id,))
        req = await cursor.fetchone()

        if not req or req[1] != today:
            await db.execute("INSERT OR REPLACE INTO user_requests (user_id, date, count) VALUES (?, ?, ?)",
                             (user_id, today, 1))
        else:
            await db.execute("UPDATE user_requests SET count = count + 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_requests_left(user_id: int) -> float:
    if user_id in ADMIN_IDS:
        return float('inf')  # Админы не имеют лимита
    async with aiosqlite.connect(DB_PATH) as db:
        today = date.today().isoformat()
        cursor = await db.execute("SELECT count, date FROM user_requests WHERE user_id = ?", (user_id,))
        req = await cursor.fetchone()
        is_premium = await is_premium_user(user_id)
        limit = PREMIUM_REQUEST_LIMIT if is_premium else REQUEST_LIMIT
        if not req or req[1] != today:
            return limit
        return max(0, limit - req[0])


async def set_user_state(user_id: int, state: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO user_state (user_id, state) VALUES (?, ?)", (user_id, state))
        await db.commit()


async def get_user_state(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT state FROM user_state WHERE user_id = ?", (user_id,))
        state = await cursor.fetchone()
        return state[0] if state else None


async def clear_user_state(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))
        await db.commit()


async def backup_db(user_id: int):
    """Создание и отправка резервной копии базы данных"""
    try:
        if not os.path.exists(DB_PATH):
            logger.error(f"Backup failed: Database file {DB_PATH} not found")
            return f"⚠️ Ошибка: Файл базы данных {DB_PATH} не найден."
        backup_filename = f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy(DB_PATH, backup_filename)
        if os.path.getsize(backup_filename) > 50 * 1024 * 1024:
            os.remove(backup_filename)
            logger.error(f"Backup failed: File {backup_filename} exceeds 50 MB")
            return "⚠️ Ошибка: Файл базы данных слишком большой для отправки (>50 МБ)."
        await bot.send_document(
            chat_id=user_id,
            document=FSInputFile(backup_filename, filename=backup_filename),
            caption="📦 Резервная копия базы данных"
        )
        os.remove(backup_filename)
        logger.info(f"Backup sent to admin {user_id}")
        return "✅ Резервная копия успешно создана и отправлена."
    except FileNotFoundError:
        logger.error(f"Backup failed: Database file {DB_PATH} not found")
        return f"⚠️ Ошибка: Файл базы данных {DB_PATH} не найден."
    except Exception as e:
        logger.error(f"Backup failed for user {user_id}: {e}")
        return f"⚠️ Ошибка при создании резервной копии: {e}"


async def ocr_image_from_bytes(img_bytes: bytes):
    try:
        base64_image = base64.b64encode(img_bytes).decode('utf-8')
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: client.chat.completions.create(
            model="openai/gpt-4o-mini",
            extra_headers={
                "HTTP-Referer": "https://your-site-url.com",
                "X-Title": "Homework Helper Bot"
            },
            extra_body={},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text",
                         "text": "Extract all text from this image accurately, including any math formulas. Return only the extracted text."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=1000
        ))
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return ""


async def call_openai_with_prompt(user_id: int, prompt: str, is_math: bool = False):
    system_prompt = (
        "Ты эксперт по всем школьным предметам, включая математику, физику, литературу и другие. Решаешь задачи и отвечаешь на вопросы кратко и четко. "
        "Для математических задач используй простые символы: √ для корня, ^ для степени, × для умножения, ÷ для деления, () для скобок, без лишних квадратных или других скобок. "
        "Без LaTeX и специальных тегов.  "
        "Для литературы давай точные и лаконичные ответы, опираясь на текст произведения, без лишних деталей. "
        "Учитывай контекст предыдущих запросов и ответов, если они есть, чтобы ответить максимально релевантно. "
        "Дай только решение или ответ, минимум текста. Если в запросе есть 'объясни' или 'поясни', добавь краткое объяснение. "
        "Избегай повторений и лишних слов."
        "Ответы должны быть структурированными."
        "Не забывай, ты работаешь в телеграм чате, где надо используй жирный шрифт и т.д. не используй своих символов - ты не на сайте. И не используй **текст** для жирного шрифта - они не помогают, используй <b>текст</b>"
    )
    try:
        loop = asyncio.get_event_loop()
        messages = [{"role": "system", "content": system_prompt}]
        mem = await get_memory(user_id)
        for question, answer in mem[-3:]:
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": prompt})
        completion = await loop.run_in_executor(None, lambda: client.chat.completions.create(
            model="deepseek/deepseek-v3.2-exp",
            extra_headers={
                "HTTP-Referer": "https://your-site-url.com",
                "X-Title": "Homework Helper Bot"
            },
            extra_body={},
            messages=messages,
            temperature=0.1,
            max_tokens=1500
        ))
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        raise


async def get_user_stats_text():
    """Получение статистики пользователей"""
    try:
        stats = await get_all_users_stats()
        total_users = len(stats)
        total_messages = sum(s["message_count"] for s in stats)
        total_photos = sum(s["photo_count"] for s in stats)
        total_documents = sum(s["document_count"] for s in stats)
        text = [
            "📊 <b>Статистика пользователей</b>",
            f"👥 Всего пользователей: {total_users}",
            f"💬 Всего текстовых сообщений: {total_messages}",
            f"📸 Всего фото: {total_photos}",
            f"📄 Всего документов: {total_documents}",
            "",
            "<b>Топ-10 активных пользователей:</b>"
        ]
        user_activity = sorted(stats, key=lambda x: x["message_count"] + x["photo_count"] + x["document_count"],
                               reverse=True)
        for i, stats in enumerate(user_activity[:10], 1):
            first_seen = datetime.fromisoformat(stats["first_seen"]).strftime("%d.%m.%Y %H:%M")
            text.append(
                f"{i}. ID: {stats['user_id']} | Сообщений: {stats['message_count']} | Фото: {stats['photo_count']} | Документы: {stats['document_count']} | Первый визит: {first_seen}")
        return "\n".join(text)
    except Exception as e:
        logger.error(f"Error in get_user_stats_text: {e}")
        return "⚠️ Ошибка при получении статистики пользователей."


async def get_users_list():
    """Получение списка всех пользователей"""
    try:
        stats = await get_all_users_stats()
        if not stats:
            return "👥 <b>Список пользователей пуст</b>"
        text = ["👥 <b>Список пользователей:</b>"]
        for i, stats in enumerate(stats, 1):
            first_seen = datetime.fromisoformat(stats["first_seen"]).strftime("%d.%m.%Y")
            last_seen = datetime.fromisoformat(stats["last_seen"]).strftime("%d.%m.%Y %H:%M")
            text.append(
                f"{i}. ID: {stats['user_id']} | Сообщений: {stats['message_count']} | Фото: {stats['photo_count']} | Документы: {stats['document_count']}")
            text.append(f"   Первый визит: {first_seen} | Последний: {last_seen}\n")
        return "\n".join(text)
    except Exception as e:
        logger.error(f"Error in get_users_list: {e}")
        return "⚠️ Ошибка при получении списка пользователей."


async def build_memory_text(user_id: int):
    mem = await get_memory(user_id)
    if not mem:
        return "📭 Память пуста."
    lines = ["🕑 <b>Последние запросы:</b>\n"]
    for i, (q, a) in enumerate(reversed(mem), 1):
        lines.append(f"<b>{i}.</b> Вопрос: {q[:120]}")
        lines.append(f"<b>Ответ:</b> {a[:300]}\n")
    return "\n".join(lines)


async def send_broadcast_message(message_text: str):
    stats = await get_all_users_stats()
    success_count = 0
    fail_count = 0
    for user in stats:
        try:
            await bot.send_message(user["user_id"], message_text)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send broadcast to {user['user_id']}: {e}")
            fail_count += 1
    return f"✅ Рассылка завершена!\nУспешно: {success_count}\nНе удалось: {fail_count}"


# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    await clear_user_state(user_id)
    await update_user_stats(user_id, "text")
    await message.answer(
        "👋 <b>Привет!</b>\nЯ бот-помощник по дзшке — отправь текст, фото или файл (TXT/PDF) задачи, или выбери действие кнопкой ниже. Помощь по боту - /help.",
        reply_markup=main_kb
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    user_id = message.from_user.id
    await update_user_stats(user_id, "text")
    help_text = (
        "👋 <b>Помощь по боту</b>\n"
        "Я бот-помощник для решения учебных задач! Вот доступные команды и функции:\n\n"
        "🔹 <b>Общие команды:</b>\n"
        "  - /start — Начать работу с ботом\n"
        "  - /help — Показать эту справку\n"
        "🔹 <b>Функции бота:</b>\n"
        "  - ✍️ Решить текст — Отправь текст или файл (TXT/PDF) задачи\n"
        "  - 📸 Решить фото — Отправь фото с задачей\n"
        "  - 📚 Конспект — Создаст конспект по тексту или файлу\n"
        "  - 🗑 Очистить память — Очистит память ИИ\n"
        "  - 👤 Личный кабинет — Посмотреть статистику\n\n"
        "Лимит: 50 запросов в день (500 для премиум, ∞ для админов). Используй кнопки ниже для действий!\n\n"
        "<b>Если нужна помощь - @s1nay3</b>"
    )
    await message.answer(help_text, reply_markup=main_kb)


@dp.message(Command("admpanel"))
async def cmd_admin_panel(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⛔️ У вас нет доступа к админ-панели.")
        return
    await update_user_stats(user_id, "text")
    await message.answer(
        "👨‍💻 <b>Админ-панель</b>\nВыберите действие:",
        reply_markup=admin_main_kb
    )


@dp.message(Command("backup_db"))
async def cmd_backup_db(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        return
    await update_user_stats(user_id, "text")
    result = await backup_db(user_id)
    await message.answer(result, reply_markup=admin_main_kb)


@dp.callback_query(F.data.startswith("btn_"))
async def callbacks_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    await callback.answer()
    if data == "btn_solve_text":
        if await get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
            await callback.message.reply("⚠️ Лимит запросов исчерпан.", reply_markup=main_kb)
            return
        await set_user_state(user_id, "awaiting_text")
        await callback.message.reply(
            "✍️ Хорошо — отправь текст или файл (TXT/PDF) задания. Нажми ❌ Отмена, чтобы выйти.",
            reply_markup=cancel_kb)
    elif data == "btn_solve_photo":
        if await get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
            await callback.message.reply("⚠️ Лимит запросов исчерпан.", reply_markup=main_kb)
            return
        await set_user_state(user_id, "awaiting_photo")
        await callback.message.reply("📸 Отлично — отправь фото задания. Нажми ❌ Отмена, чтобы выйти.",
                                     reply_markup=cancel_kb)
    elif data == "btn_conspект":
        if await get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
            await callback.message.reply("⚠️ Лимит запросов исчерпан.", reply_markup=main_kb)
            return
        await set_user_state(user_id, "awaiting_conspект")
        await callback.message.reply(
            "📚 Хорошо — пришли тему, текст или файл (TXT/PDF), по которому надо сделать конспект.",
            reply_markup=cancel_kb)
    elif data == "btn_clear_memory":
        await clear_memory(user_id)
        await callback.message.reply("🧹 Память очищена.", reply_markup=main_kb)
    elif data == "btn_profile":
        user_data = await get_user_stats(user_id)
        requests_left = await get_requests_left(user_id)
        first_seen = user_data.get("first_seen", datetime.now().isoformat())
        status = "админ" if user_id in ADMIN_IDS else "premium" if await is_premium_user(user_id) else "обычный"
        requests_text = "∞ (админ)" if user_id in ADMIN_IDS else f"{int(requests_left)} (premium)" if await is_premium_user(user_id) else f"{int(requests_left)}"
        text = (
            f"👤 <b>Личный кабинет</b>\n"
            f"🆔 ID: {user_id}\n"
            f"📅 Первый визит: {datetime.fromisoformat(first_seen).strftime('%d.%m.%Y %H:%M')}\n"
            f"📈 Остаток запросов: {requests_text}\n"
            f"💬 Всего сообщений: {user_data.get('message_count', 0)}\n"
            f"📸 Всего фото: {user_data.get('photo_count', 0)}\n"
            f"📄 Всего документов: {user_data.get('document_count', 0)}\n"
            f"👑 Статус: {status}"
        )
        await callback.message.edit_text(text, reply_markup=profile_kb)
    elif data == "btn_back_main":
        await clear_user_state(user_id)
        await callback.message.edit_text("👋 <b>Главное меню</b>", reply_markup=main_kb)
    elif data == "btn_cancel":
        await clear_user_state(user_id)
        await callback.message.reply("❌ Отмена. Возврат в главное меню.", reply_markup=main_kb)


@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in ADMIN_IDS:
        await callback.answer("⛔️ Нет доступа!", show_alert=True)
        return
    data = callback.data
    await callback.answer()
    logger.info(f"Admin callback triggered by user {user_id}: {data}")
    if data == "admin_back":
        await callback.message.edit_text(
            "👨‍💻 <b>Админ-панель</b>\nВыберите действие:",
            reply_markup=admin_main_kb
        )
    elif data == "admin_back_main":
        await clear_user_state(user_id)
        await callback.message.edit_text(
            "👋 <b>Главное меню</b>",
            reply_markup=main_kb
        )
    elif data == "admin_stats":
        stats_text = await get_user_stats_text()
        await callback.message.edit_text(
            stats_text,
            reply_markup=admin_back_kb
        )
    elif data == "admin_users":
        users_text = await get_users_list()
        await callback.message.edit_text(
            users_text,
            reply_markup=admin_back_kb
        )
    elif data == "admin_activity":
        now = datetime.now()
        recent_users = []
        stats = await get_all_users_stats()
        for user_stats in stats:
            last_seen = datetime.fromisoformat(user_stats["last_seen"])
            if (now - last_seen).days < 1:
                recent_users.append((user_stats["user_id"], last_seen, user_stats))
        recent_users.sort(key=lambda x: x[1], reverse=True)
        text = ["🕐 <b>Активность за последние 24 часа:</b>"]
        if not recent_users:
            text.append("Нет активных пользователей.")
        else:
            for i, (uid, last_seen, stats) in enumerate(recent_users[:20], 1):
                time_str = last_seen.strftime("%d.%m.%Y %H:%M")
                text.append(f"{i}. ID: {uid} | Последняя активность: {time_str}")
        await callback.message.edit_text(
            "\n".join(text),
            reply_markup=admin_back_kb
        )
    elif data == "admin_user_memory":
        await set_user_state(user_id, "admin_view_memory")
        await callback.message.edit_text(
            "🔍 <b>Просмотр памяти пользователя</b>\nОтправьте ID пользователя:",
            reply_markup=admin_back_kb
        )
    elif data == "admin_broadcast":
        await set_user_state(user_id, "admin_broadcast")
        await callback.message.edit_text(
            "📢 <b>Создание рассылки</b>\nОтправьте сообщение для рассылки:",
            reply_markup=admin_back_kb
        )
    elif data == "admin_confirm_broadcast":
        if user_id in admin_broadcast_state:
            message_text = admin_broadcast_state[user_id]
            await callback.message.edit_text("🔄 <b>Рассылка началась...</b>")
            result = await send_broadcast_message(message_text)
            admin_broadcast_state.pop(user_id, None)
            await callback.message.edit_text(result, reply_markup=admin_back_kb)
        else:
            await callback.message.edit_text(
                "❌ Нет сообщения для рассылки.",
                reply_markup=admin_back_kb
            )
    elif data == "admin_cancel_broadcast":
        admin_broadcast_state.pop(user_id, None)
        await clear_user_state(user_id)
        await callback.message.edit_text(
            "❌ Рассылка отменена.",
            reply_markup=admin_main_kb
        )
    elif data == "admin_backup_db":
        result = await backup_db(user_id)
        await callback.message.answer(result, reply_markup=admin_main_kb)
    elif data == "admin_add_premium":
        await set_user_state(user_id, "admin_add_premium")
        await callback.message.edit_text(
            "💎 <b>Дать премиум</b>\nОтправьте ID пользователя для добавления премиум-статуса:",
            reply_markup=admin_back_kb
        )
    elif data == "admin_remove_premium":
        await set_user_state(user_id, "admin_remove_premium")
        await callback.message.edit_text(
            "🗑 <b>Забрать премиум</b>\nОтправьте ID пользователя для удаления премиум-статуса:",
            reply_markup=admin_back_kb
        )


@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    state = await get_user_state(user_id)
    user_text = message.text.strip()
    if not user_text:
        await message.reply("Пустой текст — отправь задание.")
        return
    if user_id in ADMIN_IDS and state == "admin_broadcast":
        admin_broadcast_state[user_id] = user_text
        await clear_user_state(user_id)
        await message.reply(
            f"📢 <b>Сообщение для рассылки:</b>\n\n{user_text}\n\n"
            f"Получателей: {len(await get_all_users_stats())}\n"
            "Подтвердите рассылку:",
            reply_markup=admin_broadcast_kb
        )
        return
    elif user_id in ADMIN_IDS and state == "admin_view_memory":
        try:
            target_user_id = int(user_text)
            mem = await get_memory(target_user_id)
            if mem:
                memory_text = f"📚 <b>Память пользователя {target_user_id}:</b>\n\n"
                for i, (q, a) in enumerate(reversed(mem), 1):
                    memory_text += f"<b>{i}.</b> Вопрос: {q[:100]}...\n"
                    memory_text += f"Ответ: {a[:150]}...\n\n"
                await message.reply(memory_text, reply_markup=admin_back_kb)
            else:
                await message.reply("❌ Память пользователя не найдена.", reply_markup=admin_back_kb)
        except ValueError:
            await message.reply("❌ Неверный ID пользователя.", reply_markup=admin_back_kb)
        await clear_user_state(user_id)
        return
    elif user_id in ADMIN_IDS and state == "admin_add_premium":
        try:
            target_user_id = int(user_text)
            await add_premium_user(target_user_id)
            await message.reply(f"✅ Пользователь {target_user_id} добавлен в премиум-группу.", reply_markup=admin_back_kb)
        except ValueError:
            await message.reply("❌ Неверный ID пользователя.", reply_markup=admin_back_kb)
        await clear_user_state(user_id)
        return
    elif user_id in ADMIN_IDS and state == "admin_remove_premium":
        try:
            target_user_id = int(user_text)
            await remove_premium_user(target_user_id)
            await message.reply(f"✅ Пользователь {target_user_id} удален из премиум-группы.", reply_markup=admin_back_kb)
        except ValueError:
            await message.reply("❌ Неверный ID пользователя.", reply_markup=admin_back_kb)
        await clear_user_state(user_id)
        return
    await update_user_stats(user_id, "text")
    if await get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
        await message.reply("⚠️ Лимит запросов исчерпан.", reply_markup=main_kb)
        return
    if state == "awaiting_conspект":
        prompt = f"Составь краткий конспект:\n\n{user_text}"
    else:
        prompt = f"Реши задачу или ответь на вопрос:\n\n{user_text}"
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        answer = await call_openai_with_prompt(user_id, prompt, is_math=False)
        if user_id not in ADMIN_IDS:
            await update_request_count(user_id)
    except Exception as err:
        logger.exception("OpenAI error")
        await message.reply(f"⚠️ Ошибка OpenAI API: {err}")
        await clear_user_state(user_id)
        return
    await save_memory(user_id, user_text, answer)
    await clear_user_state(user_id)
    await message.reply(answer, reply_markup=main_kb)


@dp.message(F.content_type == types.ContentType.PHOTO)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    state = await get_user_state(user_id)
    await update_user_stats(user_id, "photo")
    if await get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
        await message.reply("⚠️ Лимит запросов исчерпан.", reply_markup=main_kb)
        return
    photo = message.photo[-1]
    try:
        file_info = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(file_url, timeout=15)
            resp.raise_for_status()
            img_bytes = resp.content
    except Exception as e:
        logger.exception("Failed to download photo")
        await message.reply("⚠️ Не удалось скачать изображение. Попробуй ещё раз.")
        return
    try:
        ocr_text = await ocr_image_from_bytes(img_bytes)
    except Exception:
        logger.exception("OCR failed")
        ocr_text = ""
    if not ocr_text:
        await message.reply("🤖 Не удалось распознать текст. Попробуй фото получше или добавь описание.",
                            reply_markup=main_kb)
        await clear_user_state(user_id)
        return
    if state == "awaiting_conspект":
        prompt = f"Составь краткий конспект:\n\n{ocr_text}"
    else:
        prompt = f"Реши задачу или ответь на вопрос:\n\n{ocr_text}"
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        answer = await call_openai_with_prompt(user_id, prompt, is_math=False)
        if user_id not in ADMIN_IDS:
            await update_request_count(user_id)
    except Exception as err:
        logger.exception("OpenAI error on photo")
        await message.reply(f"⚠️ Ошибка OpenAI API: {err}")
        await clear_user_state(user_id)
        return
    await save_memory(user_id, ocr_text, answer)
    await clear_user_state(user_id)
    await message.reply(answer, reply_markup=main_kb)


@dp.message(F.content_type == types.ContentType.DOCUMENT)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    state = await get_user_state(user_id)
    if state not in ["awaiting_text", "awaiting_conspект"]:
        await message.reply("📎 Для обработки файлов выбери 'Решить текст' или 'Конспект'.")
        return
    await update_user_stats(user_id, "document")
    if await get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
        await message.reply("⚠️ Лимит запросов исчерпан.", reply_markup=main_kb)
        return
    document = message.document
    file_name_lower = document.file_name.lower()
    if not file_name_lower.endswith(('.txt', '.pdf')):
        await message.reply("📎 Поддерживаемые форматы: TXT, PDF.")
        return
    try:
        file_info = await bot.get_file(document.file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(file_url, timeout=15)
            resp.raise_for_status()
            content = resp.content
    except Exception as e:
        logger.exception("Failed to download document")
        await message.reply("⚠️ Не удалось скачать файл. Попробуй ещё раз.")
        return
    try:
        if file_name_lower.endswith('.txt'):
            extracted_text = content.decode('utf-8').strip()
        elif file_name_lower.endswith('.pdf'):
            with io.BytesIO(content) as pdf_bytes:
                with pdfplumber.open(pdf_bytes) as pdf:
                    extracted_text = '\n\n'.join(page.extract_text() or '' for page in pdf.pages).strip()
    except Exception as e:
        logger.exception("Text extraction failed")
        extracted_text = ""
    if not extracted_text:
        await message.reply("🤖 Не удалось извлечь текст. Если PDF сканированный, отправь как фото.")
        await clear_user_state(user_id)
        return
    if state == "awaiting_conspект":
        prompt = f"Составь краткий конспект:\n\n{extracted_text}"
    else:
        prompt = f"Реши задачу или ответь на вопрос:\n\n{extracted_text}"
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        answer = await call_openai_with_prompt(user_id, prompt, is_math=False)
        if user_id not in ADMIN_IDS:
            await update_request_count(user_id)
    except Exception as err:
        logger.exception("OpenAI error on document")
        await message.reply(f"⚠️ Ошибка запроса. Попробуйте позже")
        await clear_user_state(user_id)
        return
    await save_memory(user_id, extracted_text, answer)
    await clear_user_state(user_id)
    await message.reply(answer, reply_markup=main_kb)


async def main():
    logger.info("Bot is starting...")
    await init_db()
    if not os.path.exists(DB_PATH):
        logger.warning(f"Database file {DB_PATH} not found, a new one was created.")
    user_stats = {}
    user_memory = {}
    user_requests = {}
    user_state = {}
    await migrate_from_dicts(user_stats, user_memory, user_requests, user_state)
    while True:
        try:
            await dp.start_polling(bot, drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Polling failed: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
