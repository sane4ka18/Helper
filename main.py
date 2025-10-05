#!/usr/bin/env python3
import os
import io
import logging
import asyncio
from typing import Dict, List, Tuple
from datetime import datetime, date

import httpx
from dotenv import load_dotenv

import base64
import pdfplumber

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_IDS = {1647999523}
DONATION_ALERTS_LINK = os.getenv("DONATION_ALERTS_LINK", "https://www.donationalerts.com/r/your_username")

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

user_state: Dict[int, str] = {}
user_memory: Dict[int, List[Tuple[str, str]]] = {}
user_stats: Dict[int, Dict] = {}
user_requests: Dict[int, Dict] = {}
MEMORY_LIMIT = 10
REQUEST_LIMIT = 50

main_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Решить текст", callback_data="btn_solve_text"),
         InlineKeyboardButton(text="📸 Решить фото", callback_data="btn_solve_photo")],
        [InlineKeyboardButton(text="📚 Конспект", callback_data="btn_conспект")],
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

def update_user_stats(user_id: int, message_type: str = "text"):
    now = datetime.now().isoformat()
    if user_id not in user_stats:
        user_stats[user_id] = {
            "first_seen": now,
            "last_seen": now,
            "message_count": 0,
            "photo_count": 0,
            "document_count": 0
        }
    user_stats[user_id]["last_seen"] = now
    if message_type == "text":
        user_stats[user_id]["message_count"] += 1
    elif message_type == "photo":
        user_stats[user_id]["photo_count"] += 1
    elif message_type == "document":
        user_stats[user_id]["document_count"] += 1

def save_memory(user_id: int, question: str, answer: str):
    mem = user_memory.setdefault(user_id, [])
    mem.append((question, answer))
    if len(mem) > MEMORY_LIMIT:
        mem.pop(0)

def build_memory_text(user_id: int):
    mem = user_memory.get(user_id, [])
    if not mem:
        return "📭 Память пуста."
    lines = ["🕑 <b>Последние запросы:</b>\n"]
    for i, (q, a) in enumerate(reversed(mem), 1):
        lines.append(f"<b>{i}.</b> Вопрос: {q[:120]}")
        lines.append(f"<b>Ответ:</b> {a[:300]}\n")
    return "\n".join(lines)

def update_request_count(user_id: int):
    today = date.today().isoformat()
    if user_id not in user_requests or user_requests[user_id]["date"] != today:
        user_requests[user_id] = {"count": 0, "date": today}
    if user_id not in ADMIN_IDS:
        user_requests[user_id]["count"] += 1

def get_requests_left(user_id: int):
    today = date.today().isoformat()
    if user_id not in user_requests or user_requests[user_id]["date"] != today:
        user_requests[user_id] = {"count": 0, "date": today}
    return REQUEST_LIMIT - user_requests[user_id]["count"] if user_id not in ADMIN_IDS else float('inf')

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
                        {"type": "text", "text": "Extract all text from this image accurately, including any math formulas. Return only the extracted text."},
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
        "Без LaTeX и специальных тегов. Не пиши пункты типа 'Анализ', 'Формулы', 'Шаги', 'Ответ'. "
        "Для литературы давай точные и лаконичные ответы, опираясь на текст произведения, без лишних деталей. "
        "Учитывай контекст предыдущих запросов и ответов, если они есть, чтобы ответить максимально релевантно. "
        "Дай только решение или ответ, минимум текста. Если в запросе есть 'объясни' или 'поясни', добавь краткое объяснение. "
        "Избегай повторений и лишних слов."
    )
    try:
        loop = asyncio.get_event_loop()
        messages = [{"role": "system", "content": system_prompt}]
        if user_id in user_memory:
            for question, answer in user_memory[user_id][-3:]:
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

def get_user_stats_text():
    total_users = len(user_stats)
    total_messages = sum(stats["message_count"] for stats in user_stats.values())
    total_photos = sum(stats["photo_count"] for stats in user_stats.values())
    total_documents = sum(stats["document_count"] for stats in user_stats.values())
    text = [
        "📊 <b>Статистика пользователей</b>",
        f"👥 Всего пользователей: {total_users}",
        f"💬 Всего текстовых сообщений: {total_messages}",
        f"📸 Всего фото: {total_photos}",
        f"📄 Всего документов: {total_documents}",
        "",
        "<b>Топ-10 активных пользователей:</b>"
    ]
    user_activity = []
    for user_id, stats in user_stats.items():
        total_activity = stats["message_count"] + stats["photo_count"] + stats["document_count"]
        user_activity.append((user_id, total_activity, stats))
    user_activity.sort(key=lambda x: x[1], reverse=True)
    for i, (user_id, activity, stats) in enumerate(user_activity[:10], 1):
        first_seen = datetime.fromisoformat(stats["first_seen"]).strftime("%d.%m.%Y %H:%M")
        text.append(
            f"{i}. ID: {user_id} | Сообщений: {stats['message_count']} | Фото: {stats['photo_count']} | Документы: {stats['document_count']} | Первый визит: {first_seen}")
    return "\n".join(text)

def get_users_list():
    if not user_stats:
        return "👥 <b>Список пользователей пуст</b>"
    text = ["👥 <b>Список пользователей:</b>"]
    for i, (user_id, stats) in enumerate(user_stats.items(), 1):
        first_seen = datetime.fromisoformat(stats["first_seen"]).strftime("%d.%m.%Y")
        last_seen = datetime.fromisoformat(stats["last_seen"]).strftime("%d.%m.%Y %H:%M")
        text.append(
            f"{i}. ID: {user_id} | Сообщений: {stats['message_count']} | Фото: {stats['photo_count']} | Документы: {stats['document_count']}")
        text.append(f"   Первый визит: {first_seen} | Последний: {last_seen}\n")
    return "\n".join(text)

async def send_broadcast_message(message_text: str):
    success_count = 0
    fail_count = 0
    users = list(user_stats.keys())
    for user_id in users:
        try:
            await bot.send_message(user_id, message_text)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send broadcast to {user_id}: {e}")
            fail_count += 1
    return f"✅ Рассылка завершена!\nУспешно: {success_count}\nНе удалось: {fail_count}"

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user_state.pop(user_id, None)
    update_user_stats(user_id, "text")
    await message.answer(
        "👋 <b>Привет!</b>\nЯ бот-помощник по дзшке — отправь текст, фото или файл (TXT/PDF) задачи, или выбери действие кнопкой ниже. Боту можно задавать ЛЮБЫЕ вопросы по учебе.",
        reply_markup=main_kb
    )

@dp.message(Command("admpanel"))
async def cmd_admin_panel(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⛔️ У вас нет доступа к админ-панель.")
        return
    update_user_stats(user_id, "text")
    await message.answer(
        "👨‍💻 <b>Админ-панель</b>\nВыберите действие:",
        reply_markup=admin_main_kb
    )

@dp.callback_query(F.data.startswith("btn_"))
async def callbacks_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    await callback.answer()
    if data == "btn_solve_text":
        if get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
            await callback.message.reply("⚠️ Лимит запросов (50 в день) исчерпан.", reply_markup=main_kb)
            return
        user_state[user_id] = "awaiting_text"
        await callback.message.reply(
            "✍️ Хорошо — отправь текст или файл (TXT/PDF) задания. Нажми ❌ Отмена, чтобы выйти.",
            reply_markup=cancel_kb)
    elif data == "btn_solve_photo":
        if get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
            await callback.message.reply("⚠️ Лимит запросов (50 в день) исчерпан.", reply_markup=main_kb)
            return
        user_state[user_id] = "awaiting_photo"
        await callback.message.reply("📸 Отлично — отправь фото задания. Нажми ❌ Отмена, чтобы выйти.",
                                     reply_markup=cancel_kb)
    elif data == "btn_conспект":
        if get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
            await callback.message.reply("⚠️ Лимит запросов (50 в день) исчерпан.", reply_markup=main_kb)
            return
        user_state[user_id] = "awaiting_conспект"
        await callback.message.reply(
            "📚 Хорошо — пришли тему, текст или файл (TXT/PDF), по которому надо сделать конспект.",
            reply_markup=cancel_kb)
    elif data == "btn_clear_memory":
        user_memory[user_id] = []
        await callback.message.reply("🧹 Память очищена.", reply_markup=main_kb)
    elif data == "btn_profile":
        user_data = user_stats.get(user_id, {})
        requests_left = get_requests_left(user_id)
        first_seen = user_data.get("first_seen", datetime.now().isoformat())
        text = (
            f"👤 <b>Личный кабинет</b>\n"
            f"🆔 ID: {user_id}\n"
            f"📅 Первый визит: {datetime.fromisoformat(first_seen).strftime('%d.%m.%Y %H:%M')}\n"
            f"📈 Остаток запросов: {requests_left if requests_left != float('inf') else '∞ (админ)'}\n"
            f"💬 Всего сообщений: {user_data.get('message_count', 0)}\n"
            f"📸 Всего фото: {user_data.get('photo_count', 0)}\n"
            f"📄 Всего документов: {user_data.get('document_count', 0)}"
        )
        await callback.message.edit_text(text, reply_markup=profile_kb)
    elif data == "btn_back_main":
        user_state[user_id] = None
        await callback.message.edit_text("👋 <b>Главное меню</b>", reply_markup=main_kb)
    elif data == "btn_cancel":
        user_state[user_id] = None
        await callback.message.reply("❌ Отмена. Возврат в главное меню.", reply_markup=main_kb)

@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in ADMIN_IDS:
        await callback.answer("⛔️ Нет доступа!", show_alert=True)
        return
    data = callback.data
    await callback.answer()
    if data == "admin_back":
        await callback.message.edit_text(
            "👨‍💻 <b>Админ-панель</b>\nВыберите действие:",
            reply_markup=admin_main_kb
        )
    elif data == "admin_back_main":
        user_state[user_id] = None
        await callback.message.edit_text(
            "👋 <b>Главное меню</b>",
            reply_markup=main_kb
        )
    elif data == "admin_stats":
        stats_text = get_user_stats_text()
        await callback.message.edit_text(
            stats_text,
            reply_markup=admin_back_kb
        )
    elif data == "admin_users":
        users_text = get_users_list()
        await callback.message.edit_text(
            users_text,
            reply_markup=admin_back_kb
        )
    elif data == "admin_activity":
        now = datetime.now()
        recent_users = []
        for uid, stats in user_stats.items():
            last_seen = datetime.fromisoformat(stats["last_seen"])
            if (now - last_seen).days < 1:
                recent_users.append((uid, last_seen, stats))
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
        user_state[user_id] = "admin_view_memory"
        await callback.message.edit_text(
            "🔍 <b>Просмотр памяти пользователя</b>\nОтправьте ID пользователя:",
            reply_markup=admin_back_kb
        )
    elif data == "admin_broadcast":
        user_state[user_id] = "admin_broadcast"
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
        user_state[user_id] = None
        await callback.message.edit_text(
            "❌ Рассылка отменена.",
            reply_markup=admin_main_kb
        )

@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id)
    user_text = message.text.strip()
    if not user_text:
        await message.reply("Пустой текст — отправь задание.")
        return
    if user_id in ADMIN_IDS and state == "admin_broadcast":
        admin_broadcast_state[user_id] = user_text
        user_state[user_id] = None
        await message.reply(
            f"📢 <b>Сообщение для рассылки:</b>\n\n{user_text}\n\n"
            f"Получателей: {len(user_stats)}\n"
            "Подтвердите рассылку:",
            reply_markup=admin_broadcast_kb
        )
        return
    elif user_id in ADMIN_IDS and state == "admin_view_memory":
        try:
            target_user_id = int(user_text)
            if target_user_id in user_memory:
                mem = user_memory[target_user_id]
                memory_text = f"📚 <b>Память пользователя {target_user_id}:</b>\n\n"
                for i, (q, a) in enumerate(reversed(mem), 1):
                    memory_text += f"<b>{i}.</b> Вопрос: {q[:100]}...\n"
                    memory_text += f"Ответ: {a[:150]}...\n\n"
                await message.reply(memory_text, reply_markup=admin_back_kb)
            else:
                await message.reply("❌ Память пользователя не найдена.", reply_markup=admin_back_kb)
        except ValueError:
            await message.reply("❌ Неверный ID пользователя.", reply_markup=admin_back_kb)
        user_state[user_id] = None
        return
    update_user_stats(user_id, "text")
    if get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
        await message.reply("⚠️ Лимит запросов (50 в день) исчерпан.", reply_markup=main_kb)
        return
    if state == "awaiting_conспект":
        prompt = f"Составь краткий конспект:\n\n{user_text}"
    else:
        prompt = f"Реши задачу или ответь на вопрос:\n\n{user_text}"
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        answer = await call_openai_with_prompt(user_id, prompt, is_math=False)
        if user_id not in ADMIN_IDS:
            update_request_count(user_id)
    except Exception as err:
        logger.exception("OpenAI error")
        await message.reply(f"⚠️ Ошибка OpenAI API: {err}")
        user_state[user_id] = None
        return
    save_memory(user_id, user_text, answer)
    user_state[user_id] = None
    await message.reply(answer, reply_markup=main_kb)

@dp.message(F.content_type == types.ContentType.PHOTO)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id)
    update_user_stats(user_id, "photo")
    if get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
        await message.reply("⚠️ Лимит запросов (50 в день) исчерпан.", reply_markup=main_kb)
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
        user_state[user_id] = None
        return
    if state == "awaiting_conспект":
        prompt = f"Составь краткий конспект:\n\n{ocr_text}"
    else:
        prompt = f"Реши задачу или ответь на вопрос:\n\n{ocr_text}"
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        answer = await call_openai_with_prompt(user_id, prompt, is_math=False)
        if user_id not in ADMIN_IDS:
            update_request_count(user_id)
    except Exception as err:
        logger.exception("OpenAI error on photo")
        await message.reply(f"⚠️ Ошибка OpenAI API: {err}")
        user_state[user_id] = None
        return
    save_memory(user_id, ocr_text, answer)
    user_state[user_id] = None
    await message.reply(answer, reply_markup=main_kb)

@dp.message(F.content_type == types.ContentType.DOCUMENT)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    state = user_state.get(user_id)
    if state not in ["awaiting_text", "awaiting_conспект"]:
        await message.reply("📎 Для обработки файлов выбери 'Решить текст' или 'Конспект'.")
        return
    update_user_stats(user_id, "document")
    if get_requests_left(user_id) <= 0 and user_id not in ADMIN_IDS:
        await message.reply("⚠️ Лимит запросов (50 в день) исчерпан.", reply_markup=main_kb)
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
        user_state[user_id] = None
        return
    if state == "awaiting_conспект":
        prompt = f"Составь краткий конспект:\n\n{extracted_text}"
    else:
        prompt = f"Реши задачу или ответь на вопрос:\n\n{extracted_text}"
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        answer = await call_openai_with_prompt(user_id, prompt, is_math=False)
        if user_id not in ADMIN_IDS:
            update_request_count(user_id)
    except Exception as err:
        logger.exception("OpenAI error on document")
        await message.reply(f"⚠️ Ошибка запроса. Попробуйте позже")
        user_state[user_id] = None
        return
    save_memory(user_id, extracted_text, answer)
    user_state[user_id] = None
    await message.reply(answer, reply_markup=main_kb)

async def main():
    logger.info("Bot is starting...")
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