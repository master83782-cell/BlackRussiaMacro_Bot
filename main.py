import asyncio
import os
import csv
import io
from html import escape
from aiohttp import web  # ДОБАВЛЕНО: веб-сервер для Render

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest  # ДОБАВЛЕНО: ошибка BadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile,
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_TEXT = os.getenv("ADMIN_IDS", "").strip()
if not ADMIN_IDS_TEXT:
    ADMIN_IDS_TEXT = os.getenv("ADMIN_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в файле .env")

try:
    ADMIN_IDS = {int(part.strip()) for part in ADMIN_IDS_TEXT.split(",") if part.strip()}
except ValueError as exc:
    raise RuntimeError("ADMIN_IDS должен содержать ID через запятую") from exc
if not ADMIN_IDS:
    raise RuntimeError("Укажите хотя бы один Telegram ID в ADMIN_IDS в файле .env")

# ---------- Роли ----------
ADMIN_CACHE: dict[int, str] = {}

async def load_admins():
    ADMIN_CACHE.clear()
    for uid in ADMIN_IDS:
        ADMIN_CACHE[uid] = "superadmin"
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id, role FROM admins") as cur:
                for row in await cur.fetchall():
                    if row["user_id"] not in ADMIN_IDS:
                        ADMIN_CACHE[row["user_id"]] = row["role"] or "moderator"
    except Exception:
        pass

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_CACHE

def is_superadmin(user_id: int) -> bool:
    return ADMIN_CACHE.get(user_id) == "superadmin"

def get_admin_role(user_id: int):
    return ADMIN_CACHE.get(user_id)

# ДОБАВЛЕНО: безопасный ответ на кнопки, чтобы не было ошибки "query is too old"
async def safe_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass

router = Router()
CATEGORIES = {
    "bluestacks": "BlueStacks",
    "mumu": "MuMu Player",
    "phone": "Телефон",
}
KINDS = {
    "macro": {"emoji": "📝", "name": "Макрос", "plural": "Макросы", "reply": "📝 Макросы"},
    "app": {"emoji": "📱", "name": "Приложение", "plural": "Приложения", "reply": "📱 Приложения"},
    "tutorial": {"emoji": "🎥", "name": "Туториал", "plural": "Туториалы", "reply": "🎥 Туториалы"},
}

class AddItem(StatesGroup):
    category = State()
    title = State()
    description = State()
    video = State()
    apk = State()
    content = State()
    guide = State()

class EditItem(StatesGroup):
    value = State()

class AddChannel(StatesGroup):
    title = State()
    chat_id = State()
    url = State()

class UserManagement(StatesGroup):
    ban_id = State()
    ban_username = State()
    unban_id = State()

class Broadcast(StatesGroup):
    message = State()

class Support(StatesGroup):
    text = State()

class SupportReply(StatesGroup):
    text = State()

class ManageAdmins(StatesGroup):
    add_id = State()
    add_role = State()

class UserMessage(StatesGroup):
    text = State()

def kb_home():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Макросы"), KeyboardButton(text="📱 Приложения")],
            [KeyboardButton(text="🎥 Туториалы"), KeyboardButton(text="🆘 Поддержка")],
        ],
        resize_keyboard=True,
    )

def kb_admin(user_id: int):
    buttons = [
        [InlineKeyboardButton(text="📝 Управление макросами", callback_data="adm:kind:macro")],
        [InlineKeyboardButton(text="📱 Управление приложениями", callback_data="adm:kind:app")],
        [InlineKeyboardButton(text="🎥 Управление туториалами", callback_data="adm:kind:tutorial")],
    ]
    if is_superadmin(user_id):
        buttons.append([InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="adm:broadcast")])
        buttons.append([InlineKeyboardButton(text="📢 Каналы подписки", callback_data="adm:channels")])
        buttons.append([InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users")])
        buttons.append([InlineKeyboardButton(text="🔑 Администраторы", callback_data="adm:admins")])
        buttons.append([InlineKeyboardButton(text="🔄 Восстановить файлы макросов", callback_data="adm:restore_files")])
    buttons.append([InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_admin_kind(kind: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"adm:add:{kind}")],
        [InlineKeyboardButton(text="📚 Список", callback_data=f"adm:list:{kind}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:del:{kind}")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"adm:edit:{kind}")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:open")],
    ])

def kb_categories_for_add(kind: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"addcat:{kind}:{key}")]
        for key, name in CATEGORIES.items()
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm:kind:{kind}")]])

def kb_tab_categories(kind: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"tabcat:{kind}:{key}")]
        for key, name in CATEGORIES.items()
    ] + [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]])

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                category TEXT DEFAULT '',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                content TEXT DEFAULT '',
                guide TEXT DEFAULT '',
                video_file_id TEXT,
                apk_file_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                url TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT DEFAULT '',
                banned_by INTEGER NOT NULL,
                banned_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'moderator',
                added_by INTEGER NOT NULL,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_items_kind_cat ON items(kind, category)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

        # Миграция колонок для старых БД
        async with db.execute("PRAGMA table_info(items)") as cur:
            item_cols = [row[1] for row in await cur.fetchall()]
        if "video_file_id" not in item_cols:
            await db.execute("ALTER TABLE items ADD COLUMN video_file_id TEXT")
        if "apk_file_id" not in item_cols:
            await db.execute("ALTER TABLE items ADD COLUMN apk_file_id TEXT")
        if "file_id" in item_cols:
            await db.execute("UPDATE items SET video_file_id = file_id WHERE video_file_id IS NULL AND (kind='macro' OR kind='tutorial')")
            await db.execute("UPDATE items SET apk_file_id = file_id WHERE apk_file_id IS NULL AND kind='app'")

        async with db.execute("PRAGMA table_info(users)") as cur:
            user_cols = [row[1] for row in await cur.fetchall()]
        if "last_seen" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
            await db.execute("UPDATE users SET last_seen = joined_at WHERE last_seen IS NULL")

        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='macros'") as cur:
            old_exists = await cur.fetchone()
        if old_exists:
            async with db.execute("SELECT COUNT(*) FROM items") as cur:
                items_count = (await cur.fetchone())[0]
            if items_count == 0:
                async with db.execute("PRAGMA table_info(macros)") as cur:
                    cols = [row[1] for row in await cur.fetchall()]
                old_db = await aiosqlite.connect(DB_PATH)
                try:
                    old_db.row_factory = aiosqlite.Row
                    async with old_db.execute("SELECT * FROM macros") as cur:
                        old_macros = await cur.fetchall()
                    for m in old_macros:
                        content = m["content"] or ""
                        desc = m["description"] or ""
                        guide = m["guide"] if "guide" in cols else ""
                        vid = m["tutorial_file_id"] if "tutorial_file_id" in cols else None
                        apk = m["apk_file_id"] if "apk_file_id" in cols else None
                        await db.execute(
                            "INSERT INTO items(kind,category,title,description,content,guide,video_file_id,apk_file_id) "
                            "VALUES('macro',?,?,?,?,?,?,?)",
                            (m["category"], m["title"], desc, content, guide or "", vid, apk),
                        )
                finally:
                    await old_db.close()

        async with db.execute("SELECT COUNT(*) FROM channels") as cur:
            count = (await cur.fetchone())[0]
        if count == 0:
            await db.executemany(
                "INSERT INTO channels(title, chat_id, url, active) VALUES(?,?,?,1)",
                [
                    ("StarBlackRussia", "@StarBlackRussia", "https://t.me/StarBlackRussia"),
                    ("gradientufa", "@gradientufa", "https://t.me/gradientufa"),
                ],
            )
        await db.commit()

async def record_user(user):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users(user_id, username, first_name, last_seen)
               VALUES(?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username,
               first_name=excluded.first_name,
               last_seen=CURRENT_TIMESTAMP""",
            (user.id, user.username, user.first_name),
        )
        await db.commit()

async def get_ban_info(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM banned_users WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone()

async def ban_user(user_id: int, admin_id: int, reason: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO banned_users(user_id, reason, banned_by) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, banned_by=excluded.banned_by, banned_at=CURRENT_TIMESTAMP",
            (user_id, reason, admin_id),
        )
        await db.commit()

async def unban_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        await db.commit()
        return cur.rowcount > 0

class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not is_admin(user.id):
            ban = await get_ban_info(user.id)
            if ban:
                reason = ban["reason"] or "Причина не указана"
                notice = f"⛔ Доступ к боту ограничен администратором.\nПричина: {escape(reason)}"
                if isinstance(event, Message):
                    await event.answer(notice)
                elif isinstance(event, CallbackQuery):
                    await safe_answer(event, "⛔ Доступ к боту ограничен.", show_alert=True)
                return
        return await handler(event, data)

async def get_active_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM channels WHERE active=1 ORDER BY id") as cur:
            return await cur.fetchall()

async def subscription_kb():
    channels = await get_active_channels()
    rows = [[InlineKeyboardButton(text=f"📢 {c['title']}", url=c["url"])] for c in channels]
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if is_admin(user_id):
        return True
    channels = await get_active_channels()
    if not channels:
        return True
    for ch in channels:
        chat_id = ch["chat_id"]
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in ("creator", "administrator", "member"):
                continue
            if member.status == "restricted" and getattr(member, "is_member", False):
                continue
            return False
        except Exception:
            return False
    return True

async def prompt_subscription(target):
    markup = await subscription_kb()
    text = "Чтобы пользоваться ботом, подпишись на все каналы и нажми «Проверить подписку»."
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await target.message.answer(text, reply_markup=markup)

# ---------- items ----------
async def add_item(kind, category, title, description, content, guide, video_file_id, apk_file_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO items(kind,category,title,description,content,guide,video_file_id,apk_file_id) VALUES(?,?,?,?,?,?,?,?)",
            (kind, category or "", title, description or "", content or "", guide or "", video_file_id, apk_file_id),
        )
        await db.commit()

async def get_item(item_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM items WHERE id=?", (item_id,)) as cur:
            return await cur.fetchone()

async def list_items(kind, category=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if category:
            q = "SELECT * FROM items WHERE kind=? AND category=? ORDER BY id DESC"
            args = (kind, category)
        else:
            q = "SELECT * FROM items WHERE kind=? ORDER BY id DESC"
            args = (kind,)
        async with db.execute(q, args) as cur:
            return await cur.fetchall()

async def send_tab_flat(target, kind):
    items = await list_items(kind)
    rows = [[InlineKeyboardButton(text=it["title"][:60], callback_data=f"viewitem:{it['id']}")] for it in items]
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    text = f"{KINDS[kind]['emoji']} {KINDS[kind]['plural']}"
    if not items:
        text += "\n\nПока пусто."
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await target.message.answer(text, reply_markup=markup)

async def send_tab_category(target, kind, category):
    items = await list_items(kind, category)
    rows = [[InlineKeyboardButton(text=it["title"][:60], callback_data=f"viewitem:{it['id']}")] for it in items]
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data=f"tab:{kind}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    text = f"{KINDS[kind]['emoji']} {KINDS[kind]['plural']}: {CATEGORIES.get(category, category)}"
    if not items:
        text += "\n\nПока пусто."
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await target.message.answer(text, reply_markup=markup)

async def send_item_view(bot: Bot, chat_id: int, item_id: int):
    item = await get_item(item_id)
    if not item:
        await bot.send_message(chat_id, "Элемент не найден.")
        return
    kind = item["kind"]

    if item["video_file_id"]:
        try:
            await bot.send_video(chat_id, item["video_file_id"], caption=f"🎥 Видео: {item['title']}")
        except Exception:
            pass
    if item["apk_file_id"]:
        try:
            await bot.send_document(chat_id, item["apk_file_id"], caption=f"📦 APK: {item['title']}")
        except Exception:
            pass

    text = f"<b>{escape(item['title'])}</b>"
    if item["description"]:
        text += f"\n\n{escape(item['description'])}"
    if item["guide"]:
        text += f"\n\n<b>📘 Инструкция:</b>\n{escape(item['guide'])}"
    if item["content"]:
        text += f"\n\n<b>⚙️ Содержимое:</b>\n<pre>{escape(item['content'])}</pre>"

    if len(text) > 3900:
        content = f"{item['title']}\n\n{item['description'] or ''}\n\nИнструкция:\n{item['guide'] or ''}\n\nСодержимое:\n{item['content'] or ''}"
        file = BufferedInputFile(content.encode("utf-8"), filename=f"item_{item_id}.txt")
        await bot.send_document(chat_id, file, caption=f"📄 {item['title']}")
        text = f"<b>{escape(item['title'])}</b>\n\nТекст отправлен файлом .txt."

    back_cb = f"tabcat:{kind}:{item['category']}" if kind == "macro" else f"tab:{kind}"

    await bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]),
    )

# ---------- Общие команды ----------
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    await record_user(message.from_user)
    if not await is_subscribed(bot, message.from_user.id):
        await prompt_subscription(message)
    else:
        await message.answer("Выбери раздел:", reply_markup=kb_home())

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await state.clear()
        role = get_admin_role(message.from_user.id)
        role_name = "👑 Суперадмин" if role == "superadmin" else "🛡 Модератор"
        await message.answer(f"🔐 Панель администратора\nРоль: {role_name}", reply_markup=kb_admin(message.from_user.id))

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=kb_home())

@router.callback_query(F.data == "checksub")
async def check_subscription(callback: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, callback.from_user.id):
        await callback.message.answer("✅ Подписка подтверждена!", reply_markup=kb_home())
    else:
        await safe_answer(callback, "Не вижу подписку на все каналы. Подпишись и попробуй снова.", show_alert=True)

@router.callback_query(F.data == "home")
async def home(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    if not await is_subscribed(bot, callback.from_user.id):
        await prompt_subscription(callback)
    else:
        await callback.message.answer("Выбери раздел:", reply_markup=kb_home())
    await safe_answer(callback)

# ---------- Поддержка ----------
@router.message(StateFilter(None), Command("support"))
async def support_start(message: Message, state: FSMContext):
    await state.set_state(Support.text)
    await message.answer("🆘 Опиши свою проблему одним сообщением.\nДля отмены: /cancel")

@router.message(StateFilter(None), F.text == "🆘 Поддержка")
async def support_button(message: Message, state: FSMContext):
    await support_start(message, state)

@router.message(Support.text)
async def support_send(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user = message.from_user
    sent = 0
    for admin_id in list(ADMIN_CACHE.keys()):
        try:
            await bot.send_message(
                admin_id,
                f"🆘 <b>Обращение в поддержку</b>\nОт: {escape(user.full_name)}" +
                (f" (@{escape(user.username)})" if user.username else "") +
                f"\nID: <code>{user.id}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"sup_reply:{user.id}")]
                ]),
            )
            await bot.copy_message(admin_id, message.chat.id, message.message_id)
            sent += 1
        except Exception:
            pass
    if sent > 0:
        await message.answer("✅ Сообщение отправлено. Администрация ответит вам в ближайшее время.")
    else:
        await message.answer("❌ Не удалось доставить сообщение. Попробуйте позже.")

@router.callback_query(F.data.startswith("sup_reply:"))
async def sup_reply_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    try: target_id = int(callback.data.split(":")[1])
    except: return await safe_answer(callback, "Ошибка.", show_alert=True)
    await state.clear()
    await state.update_data(sup_reply_target=target_id)
    await state.set_state(SupportReply.text)
    await callback.message.answer(f"✏️ Введи ответ для пользователя <code>{target_id}</code>:")
    await safe_answer(callback)

@router.message(SupportReply.text)
async def sup_reply_send(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id): return await state.clear()
    data = await state.get_data()
    target_id = data.get("sup_reply_target")
    if not target_id: return await state.clear()
    try:
        await bot.send_message(target_id, f"📩 <b>Ответ поддержки:</b>\n\n{escape(message.text or '')}")
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {escape(str(e))}")
    await state.clear()

@router.message(Command("reply"))
async def cmd_reply(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3: return await message.answer("Использование: /reply USER_ID текст")
    try: target_id = int(parts[1])
    except: return await message.answer("ID должен быть числом.")
    try:
        await bot.send_message(target_id, f"📩 <b>Ответ поддержки:</b>\n\n{escape(parts[2])}")
        await message.answer("✅ Отправлено.")
    except Exception as e:
        await message.answer(f"❌ Не удалось: {escape(str(e))}")

# ---------- Пользовательские вкладки ----------
@router.message(StateFilter(None), F.text.in_([k["reply"] for k in KINDS.values()]))
async def tab_message(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id): return await prompt_subscription(message)
    kind = {k["reply"]: kind for kind, k in KINDS.items()}[message.text]
    if kind == "macro":
        await message.answer(f"{KINDS[kind]['emoji']} {KINDS[kind]['plural']} — выбери категорию:", reply_markup=kb_tab_categories(kind))
    else:
        await send_tab_flat(message, kind)

@router.callback_query(F.data.startswith("tab:"))
async def tab_cb(callback: CallbackQuery, bot: Bot):
    kind = callback.data.split(":", 1)[1]
    if kind not in KINDS: return await safe_answer(callback, "Ошибка.", show_alert=True)
    if not await is_subscribed(bot, callback.from_user.id):
        await prompt_subscription(callback)
    else:
        if kind == "macro":
            await callback.message.answer(f"{KINDS[kind]['emoji']} {KINDS[kind]['plural']} — выбери категорию:", reply_markup=kb_tab_categories(kind))
        else:
            await send_tab_flat(callback, kind)
    await safe_answer(callback)

@router.callback_query(F.data.startswith("tabcat:"))
async def tabcat_cb(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) < 3: return await safe_answer(callback, "Ошибка.", show_alert=True)
    kind, category = parts[1], parts[2]
    if not await is_subscribed(bot, callback.from_user.id):
        await prompt_subscription(callback)
        await safe_answer(callback)
        return
    if kind == "macro" and category in CATEGORIES: await send_tab_category(callback, kind, category)
    else: await send_tab_flat(callback, kind)
    await safe_answer(callback)

@router.callback_query(F.data.startswith("viewitem:"))
async def viewitem_cb(callback: CallbackQuery, bot: Bot):
    try: item_id = int(callback.data.split(":")[1])
    except: return await safe_answer(callback, "Ошибка.", show_alert=True)
    if not await is_subscribed(bot, callback.from_user.id): await prompt_subscription(callback)
    else: await send_item_view(bot, callback.from_user.id, item_id)
    await safe_answer(callback)

# ---------- Админка: общее ----------
@router.callback_query(F.data == "adm:open")
async def admin_open(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    await state.clear()
    role = get_admin_role(callback.from_user.id)
    role_name = "👑 Суперадмин" if role == "superadmin" else "🛡 Модератор"
    await callback.message.answer(f"🔐 Панель администратора\nРоль: {role_name}", reply_markup=kb_admin(callback.from_user.id))
    await safe_answer(callback)

# ---------- Восстановление файлов ----------
@router.callback_query(F.data == "adm:restore_files")
async def restore_files(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await safe_answer(callback, "⏳ Ищу файлы…")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='macros'") as cur:
            if not await cur.fetchone(): return await callback.message.answer("Старая таблица 'macros' не найдена.")
        async with db.execute("PRAGMA table_info(macros)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        has_apk = "apk_file_id" in cols
        has_tut = "tutorial_file_id" in cols
        if not has_apk and not has_tut: return await callback.message.answer("В старой таблице нет колонок для видео или APK.")
        
        old_db = await aiosqlite.connect(DB_PATH)
        old_db.row_factory = aiosqlite.Row
        q = "SELECT title, "
        if has_tut: q += "tutorial_file_id, "
        if has_apk: q += "apk_file_id "
        q = q.rstrip(", ") + " FROM macros"
        async with old_db.execute(q) as cur: old_macros = await cur.fetchall()
            
        restored = 0
        for m in old_macros:
            vid = m["tutorial_file_id"] if has_tut else None
            apk = m["apk_file_id"] if has_apk else None
            if vid or apk:
                async with db.execute("UPDATE items SET video_file_id=COALESCE(video_file_id, ?), apk_file_id=COALESCE(apk_file_id, ?) WHERE kind='macro' AND title=?", (vid, apk, m["title"])) as update_cur:
                    if update_cur.rowcount > 0: restored += update_cur.rowcount
        await db.commit()
        await old_db.close()
    await callback.message.answer(f"✅ Восстановление завершено.\nОбновлено макросов: {restored}", reply_markup=kb_admin(callback.from_user.id))

@router.callback_query(F.data.startswith("adm:kind:"))
async def admin_kind(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    kind = callback.data.split(":")[2]
    await state.clear()
    await callback.message.answer(f"{KINDS[kind]['emoji']} <b>{KINDS[kind]['plural']}</b>\n\nЧто сделать?", reply_markup=kb_admin_kind(kind))
    await safe_answer(callback)

# ---------- Рассылка ----------
async def do_broadcast(bot: Bot, from_chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            user_ids = [row[0] for row in await cur.fetchall()]
    success = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            success += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
                success += 1
            except Exception: failed += 1
        except Exception: failed += 1
        await asyncio.sleep(0.05)
    return success, failed

@router.callback_query(F.data == "adm:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Только для суперадминов.", show_alert=True)
    await state.clear()
    await state.set_state(Broadcast.message)
    await callback.message.answer("📢 <b>Рассылка</b>\n\nОтправь сообщение для всех пользователей.\nДля отмены: /cancel")
    await safe_answer(callback)

@router.message(Broadcast.message)
async def broadcast_preview(message: Message, state: FSMContext):
    if not is_superadmin(message.from_user.id): return await state.clear()
    await state.update_data(broadcast_chat_id=message.chat.id, broadcast_message_id=message.message_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur: total = (await cur.fetchone())[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить {total} пользователям", callback_data="broadcast:confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])
    await message.answer(f"📢 Будет разослано <b>{total}</b> пользователям.\n\nОтправляем?", reply_markup=kb)

@router.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    data = await state.get_data()
    await state.clear()
    await callback.message.answer("📤 Рассылка запущена…")
    success, failed = await do_broadcast(bot, data.get("broadcast_chat_id"), data.get("broadcast_message_id"))
    await callback.message.answer(f"✅ Рассылка завершена.\nОтправлено: {success}\nНе доставлено: {failed}", reply_markup=kb_admin(callback.from_user.id))
    await safe_answer(callback)

@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await state.clear()
    await callback.message.answer("❌ Рассылка отменена.", reply_markup=kb_admin(callback.from_user.id))
    await safe_answer(callback)

# ---------- Добавление контента ----------
@router.callback_query(F.data.startswith("adm:add:"))
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    kind = callback.data.split(":")[2]
    await state.clear()
    await state.update_data(kind=kind)
    if kind == "macro":
        await state.set_state(AddItem.category)
        await callback.message.answer(f"Выбери категорию для «{KINDS[kind]['name']}»:", reply_markup=kb_categories_for_add(kind))
    else:
        await state.update_data(category="")
        await state.set_state(AddItem.title)
        await callback.message.answer(f"Введи название для «{KINDS[kind]['name']}»:")
    await safe_answer(callback)

@router.callback_query(AddItem.category, F.data.startswith("addcat:"))
async def add_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    parts = callback.data.split(":")
    kind, category = parts[1], parts[2]
    await state.update_data(category=category, kind=kind)
    await state.set_state(AddItem.title)
    await callback.message.answer(f"Введи название для «{KINDS[kind]['name']}»:")
    await safe_answer(callback)

@router.message(AddItem.title)
async def add_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    if not message.text or not message.text.strip(): return await message.answer("Введи название текстом.")
    await state.update_data(title=message.text.strip())
    await state.set_state(AddItem.description)
    await message.answer("Введи описание или /skip:")

@router.message(AddItem.description)
async def add_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    await state.update_data(description="" if (message.text or "").strip() == "/skip" else (message.text or "").strip())
    data = await state.get_data()
    kind = data.get("kind")
    
    # И для макроса, и для приложения, и для туториала спрашиваем видео первым
    await state.set_state(AddItem.video)
    if kind == "tutorial":
        await message.answer("🎥 Отправь видео прямо в Telegram (обязательно):")
    else:
        await message.answer("🎥 Отправь видео (или /skip):")

# --- ВИДЕО (для всех типов) ---
@router.message(AddItem.video, F.video)
async def add_video(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    await state.update_data(video_file_id=message.video.file_id)
    await state.set_state(AddItem.apk)
    await message.answer("✅ Видео принято! Теперь отправь APK-файл (или /skip).")

@router.message(AddItem.video, F.document)
async def add_video_doc(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    doc = message.document
    if (doc.mime_type or "").startswith("video/") or (doc.file_name or "").lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
        await state.update_data(video_file_id=doc.file_id)
        await state.set_state(AddItem.apk)
        await message.answer("✅ Видео-файл принят! Теперь отправь APK (или /skip).")
    else:
        await message.answer("Это не видео. Отправь видео или /skip.")

@router.message(AddItem.video, F.text == "/skip")
async def add_video_skip(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    await state.update_data(video_file_id=None)
    await state.set_state(AddItem.apk)
    await message.answer("Видео пропущено. Теперь отправь APK-файл (или /skip).")

@router.message(AddItem.video)
async def add_video_invalid(message: Message, state: FSMContext):
    await message.answer("Отправь видео или /skip.")

# --- APK (для всех типов) ---
@router.message(AddItem.apk, F.document)
async def add_apk(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".apk"):
        return await message.answer("Нужен файл с расширением .apk или /skip.")
    await state.update_data(apk_file_id=doc.file_id)
    
    data = await state.get_data()
    if data.get("kind") == "macro":
        await state.set_state(AddItem.content)
        await message.answer("✅ APK принят! Теперь отправь текст макроса.")
    else:
        await state.set_state(AddItem.guide)
        await message.answer("✅ APK принят! Введи инструкцию или /skip.")

@router.message(AddItem.apk, F.text == "/skip")
async def add_apk_skip(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    await state.update_data(apk_file_id=None)
    data = await state.get_data()
    if data.get("kind") == "macro":
        await state.set_state(AddItem.content)
        await message.answer("APK пропущен. Теперь отправь текст макроса.")
    else:
        await state.set_state(AddItem.guide)
        await message.answer("APK пропущен. Введи инструкцию или /skip.")

@router.message(AddItem.apk)
async def add_apk_invalid(message: Message, state: FSMContext):
    await message.answer("Отправь APK-файл документом или /skip.")

# --- ТЕКСТ МАКРОСА ---
@router.message(AddItem.content, F.document)
async def add_content_document(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id): return await state.clear()
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".txt"): return await message.answer("Нужен файл .txt.")
    tg_file = await bot.get_file(doc.file_id)
    buffer = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buffer)
    await state.update_data(content=buffer.getvalue().decode("utf-8", errors="replace"))
    await state.set_state(AddItem.guide)
    await message.answer("Введи инструкцию по установке или /skip:")

@router.message(AddItem.content)
async def add_content(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    if not message.text: return await message.answer("Отправь текст макроса или файл .txt.")
    await state.update_data(content=message.text)
    await state.set_state(AddItem.guide)
    await message.answer("Введи инструкцию по установке или /skip:")

@router.message(AddItem.guide)
async def add_guide(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    data = await state.get_data()
    kind = data.get("kind")
    guide = "" if (message.text or "").strip() == "/skip" else (message.text or "").strip()
    await add_item(
        kind=kind, category=data.get("category", ""), title=data["title"], 
        description=data.get("description", ""), content=data.get("content", ""), guide=guide, 
        video_file_id=data.get("video_file_id"), apk_file_id=data.get("apk_file_id")
    )
    await state.clear()
    await message.answer(f"✅ {KINDS[kind]['name']} сохранён!", reply_markup=kb_admin_kind(kind))

# ---------- Списки / Удаление / Редактирование ----------
@router.callback_query(F.data.startswith("adm:list:"))
async def admin_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    kind = callback.data.split(":")[2]
    items = await list_items(kind)
    if not items: text = f"{KINDS[kind]['emoji']} {KINDS[kind]['plural']} — пока пусто."
    else:
        lines = [f"{KINDS[kind]['emoji']} <b>{KINDS[kind]['plural']} ({len(items)}):</b>"]
        for it in items:
            cat = CATEGORIES.get(it["category"], it["category"]) if kind == "macro" else ""
            prefix = f"{cat}: " if cat else ""
            lines.append(f"#{it['id']} — {prefix}{escape(it['title'])}")
        text = "\n".join(lines)
    await callback.message.answer(text, reply_markup=kb_admin_kind(kind))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("adm:del:"))
async def admin_delete_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    kind = callback.data.split(":")[2]
    items = await list_items(kind)
    buttons = [[InlineKeyboardButton(text=f"🗑 #{it['id']} {it['title']}"[:60], callback_data=f"delitem:{it['id']}")] for it in items]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:kind:{kind}")])
    await callback.message.answer("Выбери для удаления:" if items else "Пусто.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("delitem:"))
async def delete_item(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    item_id = int(callback.data.split(":")[1])
    item = await get_item(item_id)
    if not item: return await safe_answer(callback, "Не найдено.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM items WHERE id=?", (item_id,))
        await db.commit()
    await callback.message.answer(f"✅ Удалено #{item_id}.", reply_markup=kb_admin_kind(item["kind"]))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("adm:edit:"))
async def admin_edit_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    kind = callback.data.split(":")[2]
    items = await list_items(kind)
    buttons = [[InlineKeyboardButton(text=f"✏️ #{it['id']} {it['title']}"[:60], callback_data=f"edititem:{it['id']}")] for it in items]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:kind:{kind}")])
    await callback.message.answer("Выбери для изменения:" if items else "Пусто.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("edititem:"))
async def edit_item_choose(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    item_id = int(callback.data.split(":")[1])
    item = await get_item(item_id)
    if not item: return await safe_answer(callback, "Не найдено.", show_alert=True)
    kind = item["kind"]
    await state.update_data(edit_item_id=item_id, edit_kind=kind)
    
    # Теперь кнопки видео и APK есть У ВСЕХ типов
    buttons = [
        [InlineKeyboardButton(text="Название", callback_data=f"editfield:{item_id}:title"), 
         InlineKeyboardButton(text="Описание", callback_data=f"editfield:{item_id}:description")],
        [InlineKeyboardButton(text="Заменить видео", callback_data=f"editfield:{item_id}:video")],
        [InlineKeyboardButton(text="Заменить APK", callback_data=f"editfield:{item_id}:apk")],
    ]
    # Текст есть только у макроса
    if kind == "macro": 
        buttons.insert(1, [InlineKeyboardButton(text="Текст макроса", callback_data=f"editfield:{item_id}:content")])
    
    buttons.append([InlineKeyboardButton(text="Инструкция", callback_data=f"editfield:{item_id}:guide")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:kind:{kind}")])
    
    await callback.message.answer("Что изменить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("editfield:"))
async def edit_field(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Нет доступа.", show_alert=True)
    parts = callback.data.split(":")
    item_id = int(parts[1])
    field = parts[2]
    item = await get_item(item_id)
    if not item: return await safe_answer(callback, "Не найдено.", show_alert=True)
    await state.update_data(edit_item_id=item_id, edit_field=field, edit_kind=item["kind"])
    await state.set_state(EditItem.value)
    if field == "video": await callback.message.answer("Отправь новое видео (или /skip).")
    elif field == "apk": await callback.message.answer("Отправь новый APK (или /skip).")
    else: await callback.message.answer("Отправь новое значение текстом.")
    await safe_answer(callback)

@router.message(EditItem.value, F.video)
async def edit_value_video(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    data = await state.get_data()
    if data.get("edit_field") != "video": return await message.answer("Для выбранного поля отправь текст.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE items SET video_file_id=? WHERE id=?", (message.video.file_id, data["edit_item_id"]))
        await db.commit()
    kind = data["edit_kind"]
    await state.clear()
    await message.answer("✅ Видео обновлено.", reply_markup=kb_admin_kind(kind))

@router.message(EditItem.value, F.document)
async def edit_value_document(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    data = await state.get_data()
    field = data.get("edit_field")
    item_id = data.get("edit_item_id")
    doc = message.document
    
    if field == "apk":
        if not (doc.file_name or "").lower().endswith(".apk"): return await message.answer("Нужен .apk файл.")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE items SET apk_file_id=? WHERE id=?", (doc.file_id, item_id))
            await db.commit()
    elif field == "video":
        if not ((doc.mime_type or "").startswith("video/") or (doc.file_name or "").lower().endswith((".mp4", ".mov", ".m4v", ".webm"))): return await message.answer("Нужен видео-файл.")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE items SET video_file_id=? WHERE id=?", (doc.file_id, item_id))
            await db.commit()
    else:
        return await message.answer("Для выбранного поля отправь текст.")
        
    kind = data["edit_kind"]
    await state.clear()
    await message.answer("✅ Файл обновлён.", reply_markup=kb_admin_kind(kind))

@router.message(EditItem.value)
async def edit_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return await state.clear()
    data = await state.get_data()
    field = data.get("edit_field")
    item_id = data.get("edit_item_id")
    kind = data.get("edit_kind")
    value = (message.text or "").strip()
    
    if field in ("video", "apk"):
        if value == "/skip":
            col = "video_file_id" if field == "video" else "apk_file_id"
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(f"UPDATE items SET {col}=NULL WHERE id=?", (item_id,))
                await db.commit()
            await state.clear()
            await message.answer("✅ Файл удалён.", reply_markup=kb_admin_kind(kind))
        else:
            await message.answer("Отправь файл (или /skip).")
        return
        
    if field not in {"title", "description", "content", "guide"} or not value:
        return await message.answer("Отправь непустой текст.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE items SET {field}=? WHERE id=?", (value, item_id))
        await db.commit()
    await state.clear()
    await message.answer("✅ Изменено.", reply_markup=kb_admin_kind(kind))

# ---------- Каналы ----------
@router.callback_query(F.data == "adm:channels")
async def channels_menu(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Только для суперадминов.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM channels ORDER BY id") as cur:
            channels = await cur.fetchall()
    buttons = []
    for ch in channels:
        status = "🟢" if ch["active"] else "⚪"
        buttons.append([InlineKeyboardButton(text=f"{status} #{ch['id']} {ch['title']}"[:60], callback_data=f"togglech:{ch['id']}")])
    buttons.extend([
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="ch:add")],
        [InlineKeyboardButton(text="🗑 Удалить канал", callback_data="ch:delete_list")],
        [InlineKeyboardButton(text="🩺 Проверить права бота", callback_data="ch:check_perms")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:open")],
    ])
    await callback.message.answer("Каналы подписки (нажми, чтобы включить/выключить):", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data == "ch:check_perms")
async def check_channel_perms(callback: CallbackQuery, bot: Bot):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    channels = await get_active_channels()
    if not channels: return await callback.message.answer("Нет активных каналов.")
    msg = "🩺 <b>Проверка прав бота в каналах:</b>\n\n"
    for ch in channels:
        try:
            bot_member = await bot.get_chat_member(ch["chat_id"], bot.id)
            if bot_member.status in ("administrator", "creator"): msg += f"✅ {ch['title']} (<code>{ch['chat_id']}</code>) — OK\n"
            else: msg += f"⚠️ {ch['title']} (<code>{ch['chat_id']}</code>) — Бот НЕ админ (статус: {bot_member.status})\n"
        except Exception as e:
            msg += f"❌ {ch['title']} (<code>{ch['chat_id']}</code>) — Ошибка: {escape(str(e))}\n"
    msg += "\n⚠️ <b>Внимание!</b> Бот обязан быть администратором для проверки подписки."
    await callback.message.answer(msg)
    await safe_answer(callback)

@router.callback_query(F.data.startswith("togglech:"))
async def toggle_channel(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    channel_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE channels SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (channel_id,))
        await db.commit()
    await safe_answer(callback, "Статус канала изменён.")
    await channels_menu(callback)

@router.callback_query(F.data == "ch:add")
async def channel_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await state.clear()
    await state.set_state(AddChannel.title)
    await callback.message.answer("Введи название канала:")
    await safe_answer(callback)

@router.message(AddChannel.title)
async def channel_add_title(message: Message, state: FSMContext):
    if not is_superadmin(message.from_user.id): return await state.clear()
    await state.update_data(title=(message.text or "").strip())
    await state.set_state(AddChannel.chat_id)
    await message.answer("Введи @username канала или chat_id (например, @mychannel):")

@router.message(AddChannel.chat_id)
async def channel_add_chat_id(message: Message, state: FSMContext):
    if not is_superadmin(message.from_user.id): return await state.clear()
    await state.update_data(chat_id=(message.text or "").strip())
    await state.set_state(AddChannel.url)
    await message.answer("Введи ссылку подписки, например https://t.me/mychannel:")

@router.message(AddChannel.url)
async def channel_add_url(message: Message, state: FSMContext):
    if not is_superadmin(message.from_user.id): return await state.clear()
    url = (message.text or "").strip()
    if not url.startswith("https://t.me/"): return await message.answer("Нужна ссылка формата https://t.me/...")
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO channels(title,chat_id,url,active) VALUES(?,?,?,1)", (data["title"], data["chat_id"], url))
        await db.commit()
    await state.clear()
    await message.answer("✅ Канал добавлен.", reply_markup=kb_admin(message.from_user.id))

@router.callback_query(F.data == "ch:delete_list")
async def channel_delete_list(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id,title FROM channels ORDER BY id") as cur: rows = await cur.fetchall()
    buttons = [[InlineKeyboardButton(text=f"🗑 #{r['id']} {r['title']}"[:60], callback_data=f"delch:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ К каналам", callback_data="adm:channels")])
    await callback.message.answer("Выбери канал для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("delch:"))
async def channel_delete(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    channel_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        await db.commit()
    await safe_answer(callback, "Канал удалён.")
    await channels_menu(callback)

# ---------- Пользователи ----------
@router.callback_query(F.data == "adm:users")
async def users_menu(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Только для суперадминов.", show_alert=True)
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Последние пользователи", callback_data="users:list")],
        [InlineKeyboardButton(text="🚫 Забанить по ID", callback_data="users:ban"), InlineKeyboardButton(text="🚫 Забанить по @username", callback_data="users:ban_username")],
        [InlineKeyboardButton(text="✅ Разбанить по ID", callback_data="users:unban"), InlineKeyboardButton(text="⛔ Список забаненных", callback_data="users:banned")],
        [InlineKeyboardButton(text="⚙️ Массовые действия", callback_data="users:mass")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:open")],
    ])
    await callback.message.answer("👥 <b>Управление пользователями</b>", reply_markup=keyboard)
    await safe_answer(callback)

async def build_user_profile(target_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (target_id,)) as cur: user = await cur.fetchone()
        async with db.execute("SELECT * FROM banned_users WHERE user_id=?", (target_id,)) as cur: ban = await cur.fetchone()
    if not user: return None, None
    is_adm = is_admin(target_id)
    text = f"👤 <b>Профиль</b>\n\nИмя: {escape(user['first_name'] or 'Без имени')}\n"
    if user["username"]: text += f"Username: @{escape(user['username'])}\n"
    text += f"ID: <code>{user['user_id']}</code>\nЗашёл: {user['joined_at'] or '—'}\n"
    if "last_seen" in user.keys() and user["last_seen"]: text += f"Активен: {user['last_seen']}\n"
    if is_adm: text += f"\n👑 Администратор: {get_admin_role(target_id)}\n"
    text += f"\n⛔ <b>Забанен</b>\nПричина: {escape(ban['reason'] or 'не указана')}\n" if ban else "\n✅ Не забанен\n"
    buttons = []
    if not is_adm: buttons.append([InlineKeyboardButton(text="✅ Разбанить" if ban else "🚫 Забанить", callback_data=f"user_unban:{target_id}" if ban else f"user_ban:{target_id}")])
    buttons.append([InlineKeyboardButton(text="✉️ Написать", callback_data=f"user_msg:{target_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ К пользователям", callback_data="users:list")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)

@router.callback_query(F.data.startswith("user_profile:"))
async def user_profile(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    target_id = int(callback.data.split(":")[1])
    text, markup = await build_user_profile(target_id)
    if not text: return await safe_answer(callback, "Не найден.", show_alert=True)
    await callback.message.answer(text, reply_markup=markup)
    await safe_answer(callback)

async def _refresh_profile(callback: CallbackQuery, target_id: int):
    text, markup = await build_user_profile(target_id)
    if text:
        try: await callback.message.edit_text(text, reply_markup=markup)
        except: pass

@router.callback_query(F.data.startswith("user_ban:"))
async def user_ban_quick(callback: CallbackQuery, bot: Bot):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    target_id = int(callback.data.split(":")[1])
    if is_admin(target_id): return await safe_answer(callback, "Нельзя забанить админа.", show_alert=True)
    await ban_user(target_id, callback.from_user.id)
    try: await bot.send_message(target_id, "⛔ Ваш доступ ограничен.")
    except: pass
    await safe_answer(callback, "Забанен.")
    await _refresh_profile(callback, target_id)

@router.callback_query(F.data.startswith("user_unban:"))
async def user_unban_quick(callback: CallbackQuery, bot: Bot):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    target_id = int(callback.data.split(":")[1])
    await unban_user(target_id)
    try: await bot.send_message(target_id, "✅ Доступ восстановлен.")
    except: pass
    await safe_answer(callback, "Разбанен.")
    await _refresh_profile(callback, target_id)

@router.callback_query(F.data.startswith("user_msg:"))
async def user_msg_start(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    target_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.update_data(msg_target=target_id)
    await state.set_state(UserMessage.text)
    await callback.message.answer(f"✉️ Введи сообщение для <code>{target_id}</code>.\nДля отмены: /cancel")
    await safe_answer(callback)

@router.message(UserMessage.text)
async def user_msg_send(message: Message, state: FSMContext, bot: Bot):
    if not is_superadmin(message.from_user.id): return await state.clear()
    target_id = (await state.get_data()).get("msg_target")
    try:
        if message.text: await bot.send_message(target_id, f"📩 <b>Сообщение от админа:</b>\n\n{escape(message.text)}")
        else:
            await bot.send_message(target_id, "📩 <b>Сообщение от админа:</b>")
            await bot.copy_message(target_id, message.chat.id, message.message_id)
        await message.answer("✅ Отправлено.")
    except Exception as e: await message.answer(f"❌ Ошибка: {escape(str(e))}")
    await state.clear()

@router.callback_query(F.data == "users:list")
async def users_list(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, username, first_name FROM users ORDER BY joined_at DESC LIMIT 20") as cur: rows = await cur.fetchall()
    buttons = [[InlineKeyboardButton(text=f"👤 {r['first_name'] or '—'} (@{r['username'] or '—'})"[:60], callback_data=f"user_profile:{r['user_id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")])
    await callback.message.answer("📋 <b>Последние пользователи:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data == "users:ban")
async def user_ban_start(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await state.clear()
    await state.set_state(UserManagement.ban_id)
    await callback.message.answer("Введи ID для бана.")
    await safe_answer(callback)

@router.message(UserManagement.ban_id)
async def user_ban_finish(message: Message, state: FSMContext, bot: Bot):
    if not is_superadmin(message.from_user.id): return await state.clear()
    try: target_id = int((message.text or "").strip())
    except: return await message.answer("Нужен ID.")
    if is_admin(target_id): return await message.answer("Нельзя забанить админа.")
    await ban_user(target_id, message.from_user.id)
    try: await bot.send_message(target_id, "⛔ Доступ ограничен.")
    except: pass
    await state.clear()
    await message.answer("🚫 Забанен.", reply_markup=kb_admin(message.from_user.id))

@router.callback_query(F.data == "users:ban_username")
async def user_ban_username_start(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await state.clear()
    await state.set_state(UserManagement.ban_username)
    await callback.message.answer("Введи @username для бана.")
    await safe_answer(callback)

@router.message(UserManagement.ban_username)
async def user_ban_username_finish(message: Message, state: FSMContext, bot: Bot):
    if not is_superadmin(message.from_user.id): return await state.clear()
    username = (message.text or "").strip().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM users WHERE username=?", (username,)) as cur: row = await cur.fetchone()
    if not row: return await message.answer("Не найден.")
    target_id = row["user_id"]
    if is_admin(target_id): return await message.answer("Нельзя забанить админа.")
    await ban_user(target_id, message.from_user.id)
    try: await bot.send_message(target_id, "⛔ Доступ ограничен.")
    except: pass
    await state.clear()
    await message.answer("🚫 Забанен.", reply_markup=kb_admin(message.from_user.id))

@router.callback_query(F.data == "users:unban")
async def user_unban_start(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await state.clear()
    await state.set_state(UserManagement.unban_id)
    await callback.message.answer("Введи ID для разбана.")
    await safe_answer(callback)

@router.message(UserManagement.unban_id)
async def user_unban_finish(message: Message, state: FSMContext, bot: Bot):
    if not is_superadmin(message.from_user.id): return await state.clear()
    try: target_id = int((message.text or "").strip())
    except: return await message.answer("Нужен ID.")
    if await unban_user(target_id):
        try: await bot.send_message(target_id, "✅ Доступ восстановлен.")
        except: pass
    await state.clear()
    await message.answer("✅ Разбанен.", reply_markup=kb_admin(message.from_user.id))

@router.callback_query(F.data == "users:banned")
async def banned_users_list(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, reason, banned_at FROM banned_users ORDER BY banned_at DESC LIMIT 50") as cur: rows = await cur.fetchall()
    if not rows: return await callback.message.answer("Пусто.")
    lines = ["⛔ <b>Забаненные:</b>"]
    for row in rows: lines.append(f"• <code>{row['user_id']}</code> — {escape(row['reason'] or 'без причины')}")
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:users")]]))
    await safe_answer(callback)

@router.callback_query(F.data == "users:mass")
async def mass_menu(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM banned_users") as cur: banned_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE last_seen < datetime('now', '-30 days')") as cur: inactive_count = (await cur.fetchone())[0]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"♻️ Разбанить всех ({banned_count})", callback_data="mass:unban_all")],
        [InlineKeyboardButton(text=f"🗑 Очистить неактивных 30д+ ({inactive_count})", callback_data="mass:cleanup")],
        [InlineKeyboardButton(text="📥 Экспорт users в CSV", callback_data="mass:export")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:users")],
    ])
    await callback.message.answer(f"⚙️ <b>Массовые действия</b>\nЗабанено: {banned_count}\nНеактивных: {inactive_count}", reply_markup=keyboard)
    await safe_answer(callback)

@router.callback_query(F.data == "mass:unban_all")
async def mass_unban_all(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM banned_users")
        await db.commit()
    await callback.message.answer("✅ Все разбанены.", reply_markup=kb_admin(callback.from_user.id))
    await safe_answer(callback)

@router.callback_query(F.data == "mass:cleanup")
async def mass_cleanup(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    admin_ids = list(ADMIN_CACHE.keys())
    async with aiosqlite.connect(DB_PATH) as db:
        if admin_ids:
            ph = ",".join("?" for _ in admin_ids)
            await db.execute(f"DELETE FROM users WHERE last_seen < datetime('now', '-30 days') AND user_id NOT IN ({ph})", admin_ids)
        else:
            await db.execute("DELETE FROM users WHERE last_seen < datetime('now', '-30 days')")
        await db.commit()
    await callback.message.answer("✅ Неактивные удалены.", reply_markup=kb_admin(callback.from_user.id))
    await safe_answer(callback)

@router.callback_query(F.data == "mass:export")
async def mass_export(callback: CallbackQuery, bot: Bot):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await safe_answer(callback, "⏳ Готовлю файл…")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur: rows = await cur.fetchall()
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "joined_at", "last_seen"])
    for r in rows: writer.writerow([r["user_id"], r["username"] or "", r["first_name"] or "", r["joined_at"] or "", r["last_seen"] or ""])
    file = BufferedInputFile(buf.getvalue().encode("utf-8"), filename="users.csv")
    await bot.send_document(callback.from_user.id, file, caption=f"📥 Экспорт {len(rows)} пользователей")

# ---------- Администраторы ----------
@router.callback_query(F.data == "adm:admins")
async def admins_menu(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    lines = ["🔑 <b>Администраторы</b>\n\nИз .env:"]
    for uid in ADMIN_IDS: lines.append(f"👑 <code>{uid}</code> — superadmin")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM admins") as cur: db_admins = await cur.fetchall()
    if db_admins:
        lines.append("\nДобавленные:")
        for a in db_admins: lines.append(f"{'👑' if a['role']=='superadmin' else '🛡'} <code>{a['user_id']}</code> — {a['role']}")
    buttons = [[InlineKeyboardButton(text="➕ Добавить", callback_data="adm:admin_add")]]
    for a in db_admins: buttons.append([InlineKeyboardButton(text=f"🗑 Удалить {a['user_id']}", callback_data=f"adm:admin_remove:{a['user_id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:open")])
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data == "adm:admin_add")
async def admin_add_id(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    await state.clear()
    await state.set_state(ManageAdmins.add_id)
    await callback.message.answer("Введи ID нового админа.")
    await safe_answer(callback)

@router.message(ManageAdmins.add_id)
async def admin_add_role(message: Message, state: FSMContext):
    if not is_superadmin(message.from_user.id): return await state.clear()
    try: target_id = int((message.text or "").strip())
    except: return await message.answer("Нужен ID.")
    if target_id in ADMIN_CACHE: return await message.answer("Уже админ.")
    await state.update_data(new_admin_id=target_id)
    await state.set_state(ManageAdmins.add_role)
    await message.answer("Выбери роль:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Модератор", callback_data="admin_role:moderator")],
        [InlineKeyboardButton(text="👑 Суперадмин", callback_data="admin_role:superadmin")],
    ]))

@router.callback_query(ManageAdmins.add_role, F.data.startswith("admin_role:"))
async def admin_add_role_finish(callback: CallbackQuery, state: FSMContext):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    role = callback.data.split(":")[1]
    target_id = (await state.get_data()).get("new_admin_id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO admins(user_id, role, added_by) VALUES(?,?,?)", (target_id, role, callback.from_user.id))
        await db.commit()
    await load_admins()
    await state.clear()
    await callback.message.answer(f"✅ <code>{target_id}</code> теперь {role}.", reply_markup=kb_admin(callback.from_user.id))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("adm:admin_remove:"))
async def admin_remove(callback: CallbackQuery):
    if not is_superadmin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    target_id = int(callback.data.split(":")[2])
    if target_id in ADMIN_IDS: return await safe_answer(callback, "Нельзя удалить из .env.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
        await db.commit()
    await load_admins()
    await safe_answer(callback, "Удалён.")
    await admins_menu(callback)

# ---------- Статистика ----------
@router.callback_query(F.data == "adm:stats")
async def stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return await safe_answer(callback, "Ошибка.", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur: users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM items WHERE kind='macro'") as cur: macros = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM items WHERE kind='app'") as cur: apps = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM items WHERE kind='tutorial'") as cur: tutorials = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM channels WHERE active=1") as cur: channels = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM banned_users") as cur: banned = (await cur.fetchone())[0]
    await callback.message.answer(
        f"📊 Статистика\nПользователей: {users}\n📝 Макросов: {macros}\n📱 Приложений: {apps}\n🎥 Туториалов: {tutorials}\n⛔ Забанено: {banned}\nАктивных каналов: {channels}",
        reply_markup=kb_admin(callback.from_user.id),
    )
    await safe_answer(callback)

# ---------- ЗАПУСК ----------
async def main():
    await init_db()
    await load_admins()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    router.message.outer_middleware(BanMiddleware())
    router.callback_query.outer_middleware(BanMiddleware())
    dp.include_router(router)
    
    # --- ДОБАВЛЕНО: Веб-сервер для Render (чтобы не было ошибки No open ports) ---
    async def handle(request):
        return web.Response(text="Bot is running!")
    
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")
    # -------------------------------------------------------------------------

    print("Bot started. Stop with Ctrl+C.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
