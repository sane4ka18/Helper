import aiosqlite
import logging
from datetime import datetime, date
from typing import Dict, List, Tuple
from aiogram.types import FSInputFile

# Настройка логирования
logger = logging.getLogger(__name__)

# Путь к файлу базы данных
DB_PATH = "bot.db"


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
        # Миграция user_stats
        for user_id, stats in user_stats.items():
            await db.execute("""
                INSERT OR REPLACE INTO users (user_id, first_seen, last_seen, message_count, photo_count, document_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, stats["first_seen"], stats["last_seen"], stats["message_count"],
                  stats["photo_count"], stats["document_count"]))

        # Миграция user_memory
        for user_id, memories in user_memory.items():
            for question, answer in memories:
                await db.execute("""
                    INSERT INTO user_memory (user_id, question, answer, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (user_id, question, answer, datetime.now().isoformat()))

        # Миграция user_requests
        for user_id, req in user_requests.items():
            await db.execute("""
                INSERT OR REPLACE INTO user_requests (user_id, date, count)
                VALUES (?, ?, ?)
            """, (user_id, req["date"], req["count"]))

        # Миграция user_state
        for user_id, state in user_state.items():
            await db.execute("""
                INSERT OR REPLACE INTO user_state (user_id, state)
                VALUES (?, ?)
            """, (user_id, state))

        await db.commit()
        logger.info("Data migrated from dictionaries to database")


# Функции для работы с users
async def update_user_stats(user_id: int, message_type: str = "text"):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        # Проверяем, есть ли пользователь
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()

        if not user:
            await db.execute("""
                INSERT INTO users (user_id, first_seen, last_seen, message_count, photo_count, document_count)
                VALUES (?, ?, ?, 0, 0, 0)
            """, (user_id, now, now))

        # Обновляем счетчики
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


# Функции для работы с user_memory
async def save_memory(user_id: int, question: str, answer: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # Сохраняем новый запрос
        await db.execute("""
            INSERT INTO user_memory (user_id, question, answer, timestamp)
            VALUES (?, ?, ?, ?)
        """, (user_id, question, answer, datetime.now().isoformat()))

        # Удаляем старые записи, если превышен лимит
        MEMORY_LIMIT = 10
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


# Функции для работы с user_requests
async def update_request_count(user_id: int):
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
    REQUEST_LIMIT = 50
    async with aiosqlite.connect(DB_PATH) as db:
        today = date.today().isoformat()
        cursor = await db.execute("SELECT count, date FROM user_requests WHERE user_id = ?", (user_id,))
        req = await cursor.fetchone()
        if not req or req[1] != today:
            return REQUEST_LIMIT
        return max(0, REQUEST_LIMIT - req[0])


# Функции для работы с user_state
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
