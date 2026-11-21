
import asyncio
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from html import escape

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    LabeledPrice, PreCheckoutQuery,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("У файлі .env немає BOT_TOKEN")

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

DB = "inkognito_v2.db"
EVIDENCE_LIMIT = 10
REPORT_WINDOW = 300
EVIDENCE_DAYS = 30
PLANS = {"7": (7, 49), "30": (30, 149), "365": (365, 999)}
UNBAN_STARS = 99
KYIV = ZoneInfo("Europe/Kyiv")
REWARD_FIRST_WEEK = (10, 15, 20, 25, 30, 40, 60)
REWARD_PLANS = {"1": (1, 200), "7": (7, 2500), "30": (30, 15000)}

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
lock = asyncio.Lock()

REGIONS = {
    "all": "🇺🇦 Вся Україна",
    "vin": "Вінницька",
    "vol": "Волинська",
    "dn": "Дніпропетровська",
    "don": "Донецька",
    "zh": "Житомирська",
    "zak": "Закарпатська",
    "zap": "Запорізька",
    "if": "Івано-Франківська",
    "kyiv_ob": "Київська",
    "kir": "Кіровоградська",
    "lug": "Луганська",
    "lv": "Львівська",
    "myk": "Миколаївська",
    "od": "Одеська",
    "pol": "Полтавська",
    "riv": "Рівненська",
    "sum": "Сумська",
    "ter": "Тернопільська",
    "kha": "Харківська",
    "khe": "Херсонська",
    "khm": "Хмельницька",
    "cherk": "Черкаська",
    "chern": "Чернівецька",
    "cherg": "Чернігівська",
    "crimea": "АР Крим",
    "city_kyiv": "м. Київ",
    "sev": "м. Севастополь",
}

GENDERS = {
    "male": "👨 Хлопець",
    "female": "👩 Дівчина",
    "unspecified": "🙋 Не вказувати",
}
AGES = ["18-24", "25-34", "35-44", "45+"]

REASONS = {
    "spam": "📢 Спам або реклама",
    "abuse": "😡 Образи або переслідування",
    "fraud": "💸 Шахрайство",
    "content": "⚠️ Неприйнятний контент",
    "other": "📝 Інше порушення",
}

# Тимчасові дані лише в оперативній пам'яті.
waiting = deque()
partners = {}

# Для кожного користувача — останні вхідні повідомлення.
incoming = defaultdict(lambda: deque(maxlen=EVIDENCE_LIMIT))

# Дані останньої розмови, доступні 5 хвилин.
recent = {}

# Незавершені скарги.
drafts = {}

# Захист від надто частих повідомлень.
last_message = {}
match_started = {}
message_bursts = defaultdict(deque)



def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def inline(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=data
                )
                for title, data in row
            ]
            for row in rows
        ]
    )


INTERESTS = {"games":"🎮 Ігри","music":"🎵 Музика","films":"🎬 Фільми та серіали","sport":"🏋️ Спорт","cars":"🚗 Автомобілі","travel":"✈️ Подорожі","books":"📚 Книги","tech":"💻 Технології","food":"🍳 Кулінарія","animals":"🐾 Тварини","art":"🎨 Творчість","business":"💼 Бізнес","nature":"🌿 Природа","talk":"💬 Спілкування"}
GOALS = {"friends":"🤝 Дружба","relationship":"❤️ Стосунки","flirt":"💕 Флірт","chat":"💬 Спілкування","people":"🌟 Однодумці"}

def age_label(value):
    return 'Будь-який вік' if value == 'any' else (value + ' років' if value in AGES else 'Не вказано')

menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔎 Знайти співрозмовника")],
        [
            KeyboardButton(text="👤 Мій профіль"),
            KeyboardButton(text="🎯 Фільтри пошуку"),
        ],
        [
            KeyboardButton(text="🔒 Чорний список"),
            KeyboardButton(text="⭐ Premium"),
        ],
        [KeyboardButton(text="🎁 Запросити друзів"), KeyboardButton(text="🟢 Онлайн")],
        [KeyboardButton(text="🎁 Щоденний бонус"), KeyboardButton(text="🛍 Магазин бонусів")],
        [KeyboardButton(text="🔐 Приватність")],
        [KeyboardButton(text="🎯 Мої інтереси"), KeyboardButton(text="❤️ Мета знайомства")],
        [KeyboardButton(text="⚙️ Налаштування")],
    ],
    resize_keyboard=True,
)

chat_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏭ Наступний"), KeyboardButton(text="🛑 Завершити")],
        [KeyboardButton(text="🚫 Заблокувати"), KeyboardButton(text="⚠️ Поскаржитися")],
        [KeyboardButton(text="💕 Запропонувати знайомство")],
    ],
    resize_keyboard=True,
)

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            adult INTEGER DEFAULT 0,
            gender TEXT DEFAULT 'unspecified',
            age TEXT DEFAULT '18-24',
            region TEXT DEFAULT 'if',
            search_region TEXT DEFAULT 'all',
            step TEXT DEFAULT 'age_confirm',
            registered INTEGER DEFAULT 0,
            premium_until TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blocker_id INTEGER NOT NULL,
            blocked_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(blocker_id, blocked_id)
        );

        CREATE TABLE IF NOT EXISTS moderation_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL,
            reported_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS user_bans (
            user_id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL,
            banned_until TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            admin_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS moderation_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS report_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            message_time TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(report_id)
                REFERENCES moderation_reports(id)
        );

        CREATE INDEX IF NOT EXISTS idx_evidence_report
            ON report_evidence(report_id);

        CREATE INDEX IF NOT EXISTS idx_evidence_expiry
            ON report_evidence(expires_at);

        CREATE INDEX IF NOT EXISTS idx_reports_status
            ON moderation_reports(status);
        """)
        # Міграція існуючих баз без втрати профілів.
        cols = {r[1] for r in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
        for name, definition in {
            "search_age": "TEXT DEFAULT 'any'",
            "search_gender": "TEXT DEFAULT 'any'",
            "referrer_id": "INTEGER",
            "referral_awards": "INTEGER DEFAULT 0",
            "goal": "TEXT DEFAULT NULL",
            "match_mode": "TEXT DEFAULT 'balanced'",
            "show_age": "INTEGER DEFAULT 1",
            "show_region": "INTEGER DEFAULT 0",
            "show_interests": "INTEGER DEFAULT 1",
            "show_goal": "INTEGER DEFAULT 1",
            "allow_contact": "INTEGER DEFAULT 1",
            "bonus_points": "INTEGER DEFAULT 0",
            "bonus_last": "TEXT",
            "bonus_streak": "INTEGER DEFAULT 0",
            "bonus_shields": "INTEGER DEFAULT 0",
            "bonus_last_shield_at": "INTEGER DEFAULT 0",
            "reputation": "INTEGER DEFAULT 100",
            "last_match_notice": "TEXT",
        }.items():
            if name not in cols:
                await db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS premium_payments (
            charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            payload TEXT NOT NULL, stars INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS referrals (
            invited_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ban_appeals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, ban_created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            decided_by INTEGER, decided_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
        CREATE TABLE IF NOT EXISTS user_interests (user_id INTEGER NOT NULL, interest TEXT NOT NULL, PRIMARY KEY(user_id, interest));
        CREATE TABLE IF NOT EXISTS mutual_likes (user_id INTEGER NOT NULL, liked_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, liked_id));
        CREATE TABLE IF NOT EXISTS contact_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_contact_requests_pair ON contact_requests(requester_id, recipient_id, status);
        CREATE TABLE IF NOT EXISTS reputation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, report_id INTEGER UNIQUE, points INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS user_notifications (user_id INTEGER PRIMARY KEY, match_alerts INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS chat_ratings (id INTEGER PRIMARY KEY AUTOINCREMENT, rater_id INTEGER NOT NULL, partner_id INTEGER NOT NULL, rating TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS match_events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_a INTEGER NOT NULL, user_b INTEGER NOT NULL, compatibility INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_match_events_date ON match_events(created_at);
        """)
        await db.commit()


async def one(query, args=()):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, args) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def all_rows(query, args=()):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, args) as cursor:
            return [dict(x) for x in await cursor.fetchall()]


async def write(query, args=()):
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(query, args)
        await db.commit()
        return cursor.lastrowid


async def user_get(uid):
    return await one("SELECT * FROM users WHERE id=?", (uid,))


async def user_create(uid):
    await write(
        "INSERT OR IGNORE INTO users(id) VALUES(?)",
        (uid,),
    )


async def user_update(uid, **fields):
    allowed = {
        "adult", "gender", "age", "region",
        "search_region", "search_age", "search_gender", "step", "registered", "goal", "match_mode", "show_age", "show_region", "show_interests", "show_goal", "allow_contact",
    }
    fields = {
        key: value for key, value in fields.items()
        if key in allowed
    }
    if not fields:
        return

    assignments = ", ".join(f"{key}=?" for key in fields)
    await write(
        f"UPDATE users SET {assignments} WHERE id=?",
        (*fields.values(), uid),
    )


async def send(bot, uid, text, markup=None):
    try:
        await bot.send_message(
            uid,
            text,
            reply_markup=markup,
        )
        return True
    except TelegramAPIError:
        logging.warning("Повідомлення не доставлено: %s", uid)
        return False


async def is_banned(uid):
    row = await one(
        "SELECT banned_until FROM user_bans WHERE user_id=?",
        (uid,),
    )
    if not row:
        return False

    if row["banned_until"] is None:
        return True

    try:
        expiry = datetime.fromisoformat(row["banned_until"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry > now_utc():
            return True
    except ValueError:
        return True

    await write(
        "DELETE FROM user_bans WHERE user_id=?",
        (uid,),
    )
    return False


async def allowed(bot, uid):
    if await is_banned(uid):
        row = await one('SELECT reason,banned_until FROM user_bans WHERE user_id=?', (uid,))
        await send(bot, uid,
                   '⛔ <b>Доступ до Інкогніто обмежено.</b>\n'
                   f'Причина: {escape(row["reason"])}\n'
                   f'До: {escape(row["banned_until"][:10]) if row["banned_until"] else "назавжди"}\n\n'
                   '📝 Подати апеляцію: /appeal')
        return False
    return True


async def is_blocked(a, b):
    return bool(await one("""
        SELECT 1 FROM user_blocks
        WHERE (blocker_id=? AND blocked_id=?)
           OR (blocker_id=? AND blocked_id=?)
        LIMIT 1
    """, (a, b, b, a)))


def region_keyboard(prefix, page=0):
    items = list(REGIONS.items())
    page = max(0, min(page, (len(items) - 1) // 8))

    rows = [
        [(name, f"{prefix}:{code}")]
        for code, name in items[page * 8:(page + 1) * 8]
    ]

    navigation = []
    if page:
        navigation.append(
            ("⬅️ Назад", f"page:{prefix}:{page-1}")
        )
    if (page + 1) * 8 < len(items):
        navigation.append(
            ("Далі ➡️", f"page:{prefix}:{page+1}")
        )
    if navigation:
        rows.append(navigation)

    return inline(rows)


async def show_gender(bot, uid, editing=False):
    prefix = "editgender" if editing else "gender"
    await send(
        bot,
        uid,
        "🙋 <b>Обери стать</b>",
        inline([
            [(name, f"{prefix}:{code}")]
            for code, name in GENDERS.items()
        ]),
    )


async def show_age(bot, uid, editing=False):
    prefix = "editage" if editing else "age"
    await send(
        bot,
        uid,
        "🎂 <b>Обери вікову категорію</b>",
        inline([
            [(age, f"{prefix}:{age}")]
            for age in AGES
        ]),
    )


async def show_region(bot, uid, editing=False):
    prefix = "editregion" if editing else "region"
    await send(
        bot,
        uid,
        "📍 <b>Звідки ти?</b>\n\n"
        "Обери регіон або «Вся Україна».",
        region_keyboard(prefix),
    )


async def welcome(bot, uid):
    await send(
        bot,
        uid,
        "🎭 <b>ІНКОГНІТО 🇺🇦</b>\n\n"
        "💬 Нові знайомства починаються тут!\n\n"
        "🔒 Твій Telegram-профіль прихований "
        "від співрозмовника.\n"
        "🌍 Знайомся з людьми з усієї України.\n"
        "🛡️ Можеш блокувати порушників і "
        "надсилати скарги.\n\n"
        "📋 <b>Приватність:</b> бот тимчасово "
        "обробляє останні 10 отриманих текстових "
        "повідомлень для можливості скарги. "
        "Після завершення чату вони доступні "
        "для скарги до 5 хвилин. "
        "Докази зберігаються до 30 днів лише "
        "після твоєї згоди.\n\n"
        "✨ <i>Твоє ім'я — секрет. "
        "Твоя історія — ні.</i>",
        menu,
    )


@dp.message(CommandStart())
async def start(message: Message, bot: Bot):
    if message.chat.type != "private":
        return

    uid = message.from_user.id
    if not await allowed(bot, uid):
        return

    await user_create(uid)
    args = (message.text or '').split(maxsplit=1)
    if len(args) == 2:
        await register_referral(uid, args[1])
    user = await user_get(uid)

    if user["registered"]:
        await welcome(bot, uid)
    elif not user["adult"]:
        await message.answer(
            "🎭 <b>Ласкаво просимо в Інкогніто!</b>\n\n"
            "🔞 Сервіс призначений для осіб 18+.\n"
            "Підтверди свій вік:",
            reply_markup=inline([
                [("✅ Мені є 18", "adult:yes")],
                [("❌ Мені немає 18", "adult:no")],
            ]),
        )
    else:
        await show_gender(bot, uid)


@dp.callback_query(F.data.startswith("adult:"))
async def adult_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    await call.answer()
    await user_create(uid)

    if not await allowed(bot, uid):
        return

    if call.data == "adult:no":
        await send(bot, uid, "🔞 Сервіс доступний лише з 18 років.")
        return

    await user_update(uid, adult=1, step="gender")
    await show_gender(bot, uid)


@dp.callback_query(
    F.data.startswith("gender:") |
    F.data.startswith("editgender:")
)
async def gender_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    user = await user_get(uid)
    if not user or not user["adult"] or await is_banned(uid):
        await call.answer("Немає доступу", show_alert=True)
        return

    prefix, value = call.data.split(":", 1)
    if value not in GENDERS:
        await call.answer("Невірний вибір")
        return

    if prefix == "editgender" and not user["registered"]:
        await call.answer("Спочатку заверши реєстрацію")
        return

    await user_update(uid, gender=value)
    await call.answer("Збережено!")

    if prefix == "editgender":
        await send(bot, uid, "✅ Стать змінено.", menu)
    else:
        await user_update(uid, step="age")
        await show_age(bot, uid)


@dp.callback_query(
    F.data.startswith("age:") |
    F.data.startswith("editage:")
)
async def age_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    user = await user_get(uid)
    if not user or not user["adult"] or await is_banned(uid):
        await call.answer("Немає доступу", show_alert=True)
        return

    prefix, value = call.data.split(":", 1)
    if value not in AGES:
        await call.answer("Невірний вибір")
        return

    if prefix == "editage" and not user["registered"]:
        await call.answer("Спочатку заверши реєстрацію")
        return

    await user_update(uid, age=value)
    await call.answer("Збережено!")

    if prefix == "editage":
        await send(bot, uid, "✅ Вік змінено.", menu)
    else:
        await user_update(uid, step="region")
        await show_region(bot, uid)


@dp.callback_query(F.data.startswith("page:"))
async def page_cb(call: CallbackQuery):
    try:
        _, prefix, page = call.data.split(":")
        if prefix not in ("region", "editregion", "search"):
            raise ValueError()
        page = int(page)
    except (ValueError, TypeError):
        await call.answer("Помилка")
        return

    await call.message.edit_reply_markup(
        reply_markup=region_keyboard(prefix, page)
    )
    await call.answer()


@dp.callback_query(
    F.data.startswith("region:") |
    F.data.startswith("editregion:") |
    F.data.startswith("search:")
)
async def region_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    user = await user_get(uid)
    if not user or not user["adult"] or await is_banned(uid):
        await call.answer("Спочатку введи /start")
        return

    prefix, value = call.data.split(":", 1)
    if value not in REGIONS:
        await call.answer("Невірний регіон")
        return

    if prefix in ("search", "editregion") and not user["registered"]:
        await call.answer("Спочатку зареєструйся")
        return

    if prefix == "search":
        if not await has_premium(uid):
            await call.answer('Потрібен Premium', show_alert=True)
            return
        await user_update(uid, search_region=value)
        text = f"🌍 Пошук: <b>{REGIONS[value]}</b>"
    elif prefix == "editregion":
        await user_update(uid, region=value)
        text = f"📍 Твій регіон: <b>{REGIONS[value]}</b>"
    else:
        await user_update(
            uid,
            region=value,
            registered=1,
            step="done",
        )
        text = "🎉 <b>Реєстрацію завершено!</b>"

    await call.answer("Збережено!")
    await send(bot, uid, text, menu)
    if prefix == "region":
        await complete_referral(uid, bot)
    elif prefix in ("search", "editregion"):
        await refresh_waiting(bot, uid)


def purge_memory():
    current = time.monotonic()

    for uid, record in list(recent.items()):
        if record["expires"] <= current:
            recent.pop(uid, None)

    for uid, draft in list(drafts.items()):
        if draft["expires"] <= current:
            drafts.pop(uid, None)


async def cleanup_database():
    expiry = iso_now()
    await write(
        "DELETE FROM report_evidence WHERE expires_at<=?",
        (expiry,),
    )


async def cleanup_loop():
    while True:
        try:
            purge_memory()
            await cleanup_database()
        except Exception:
            logging.exception("Помилка очищення даних")
        await asyncio.sleep(60)


async def compatible(a, b):
    ua = await user_get(a)
    ub = await user_get(b)

    if not ua or not ub:
        return False
    if not ua["registered"] or not ub["registered"]:
        return False
    if await is_banned(a) or await is_banned(b):
        return False
    if await is_blocked(a, b):
        return False

    pa = await has_premium(a)
    pb = await has_premium(b)
    return (
        (not pa or ua["search_region"] in ("all", ub["region"]))
        and (not pb or ub["search_region"] in ("all", ua["region"]))
        and (not pa or ua["search_age"] in ("any", ub["age"]))
        and (not pb or ub["search_age"] in ("any", ua["age"]))
        and (not pa or ua["search_gender"] in ("any", ub["gender"]))
        and (not pb or ub["search_gender"] in ("any", ua["gender"]))
    )


def remember_finished(uid, other):
    recent[uid] = {
        "partner": other,
        "messages": list(incoming[uid]),
        "expires": time.monotonic() + REPORT_WINDOW,
    }
    incoming.pop(uid, None)


async def finish_chat(bot, uid, notify=True):
    async with lock:
        other = partners.pop(uid, None)

        if other is not None and partners.get(other) == uid:
            partners.pop(other, None)

            remember_finished(uid, other)
            remember_finished(other, uid)

        try:
            waiting.remove(uid)
        except ValueError:
            pass

    if notify:
        await send(
            bot,
            uid,
            "🛑 <b>Розмову або пошук завершено.</b>\n\n"
            "⚠️ На останнього співрозмовника можна "
            "поскаржитися протягом 5 хвилин.\n"
            "🔎 /next — нове знайомство.",
            menu,
        )

    if other is not None:
        await send(
            bot,
            other,
            "👋 <b>Співрозмовник завершив розмову.</b>\n\n"
            "⚠️ Скаргу можна подати протягом 5 хвилин.\n"
            "🔎 /next — знайти іншого.",
            menu,
        )

    if other is not None:
        await write("UPDATE contact_requests SET status='expired', resolved_at=? WHERE status IN ('pending','accepted','declined') AND ((requester_id=? AND recipient_id=?) OR (requester_id=? AND recipient_id=?))",(iso_now(),uid,other,other,uid))
        await write("DELETE FROM mutual_likes WHERE (user_id=? AND liked_id=?) OR (user_id=? AND liked_id=?)",(uid,other,other,uid))
        match_started.pop(uid, None)
        match_started.pop(other, None)
    if other is not None:
        for person, peer in ((uid, other), (other, uid)):
            await send(bot, person, '👍 Як пройшла розмова? Оцінка анонімна та необов’язкова.', inline([[('😍 Чудово', f'rate:{peer}:great'), ('🙂 Нормально', f'rate:{peer}:good')], [('😕 Не сподобалось', f'rate:{peer}:bad'), ('⏭ Пропустити', f'rate:{peer}:skip')]]))
    return other


async def user_interests(uid):
    return {r['interest'] for r in await all_rows('SELECT interest FROM user_interests WHERE user_id=?', (uid,))}


async def compatibility_score(a, b):
    ua, ub = await user_get(a), await user_get(b)
    ia, ib = await user_interests(a), await user_interests(b)
    common = ia & ib
    # Оцінюємо лише заповнені критерії; відсутні дані не є збігом.
    points, possible = 0, 0
    if ua.get('goal') and ub.get('goal'):
        possible += 40
        if ua['goal'] == ub['goal']:
            points += 40
    if ia and ib:
        possible += 30
        points += round(30 * len(common) / max(len(ia), len(ib)))
    if ua.get('age') in AGES and ub.get('age') in AGES:
        possible += 20
        if ua['age'] == ub['age']:
            points += 20
    if ua.get('region') not in (None, 'all') and ub.get('region') not in (None, 'all'):
        possible += 10
        if ua['region'] == ub['region']:
            points += 10
    return (round(100 * points / possible) if possible else None), common


async def match_description(viewer, peer):
    mine, other = await user_get(viewer), await user_get(peer)
    score, shared = await compatibility_score(viewer, peer)
    interests = await user_interests(peer)
    lines = ['🎉 <b>Співрозмовника знайдено!</b>']
    if await has_premium(viewer):
        lines.append(f'💯 Сумісність: <b>{str(score) + "%" if score is not None else "недостатньо даних"}</b>')
    if other.get('show_goal', 1):
        lines.append('❤️ Мета: ' + GOALS.get(other.get('goal'), 'Не вибрано'))
    if other.get('show_interests', 1):
        lines.append('🎯 Інтереси: ' + (', '.join(INTERESTS[k] for k in sorted(interests) if k in INTERESTS) or 'Не вибрано'))
        if shared:
            lines.append('✨ Спільне: ' + ', '.join(INTERESTS[k] for k in sorted(shared) if k in INTERESTS))
    if other.get('show_age', 1):
        lines.append('🎂 Вік: ' + age_label(other.get('age')))
    if other.get('show_region', 0):
        lines.append('📍 Область: ' + REGIONS.get(other.get('region'), 'Не вказано'))
    if mine.get('goal') and mine.get('goal') == other.get('goal') and other.get('show_goal', 1):
        lines.append('💞 Мета знайомства збігається!')
    lines.append('\n💬 Починайте анонімне спілкування.\n⏭ /next · 🛑 /stop')
    return '\n'.join(lines)


async def find_partner(bot, uid):
    if not await allowed(bot, uid):
        return
    user = await user_get(uid)
    if not user or not user['registered']:
        await send(bot, uid, '👋 Спочатку введи /start.')
        return
    async with lock:
        if uid in partners:
            result, other = '💬 <b>Ти вже в чаті!</b> Натисни «⏭ Наступний», щоб змінити співрозмовника.', None
        elif uid in waiting:
            result, other = '⏳ <b>Ти вже в пошуку.</b> Натисни «🛑 Завершити», щоб скасувати.', None
        else:
            other, result = None, None
            ranked = []
            for candidate in list(waiting):
                if candidate == uid or candidate in partners or not await compatible(uid, candidate):
                    continue
                cu = await user_get(candidate)
                score, shared = await compatibility_score(uid, candidate)
                goal_match = bool(user.get('goal') and user.get('goal') == cu.get('goal'))
                # Строгий режим обох сторін потребує збігу мети або інтересу.
                if (user.get('match_mode') == 'strict' or cu.get('match_mode') == 'strict') and not (goal_match or shared):
                    continue
                ranked.append((score if score is not None else -1, -list(waiting).index(candidate), candidate))
            if ranked:
                other = max(ranked)[2]
                waiting.remove(other)
                partners[uid] = other
                partners[other] = uid
                match_started[uid] = match_started[other] = time.monotonic()
                incoming.pop(uid, None)
                incoming.pop(other, None)
            else:
                waiting.append(uid)
                result = '🔎 <b>Шукаємо співрозмовника...</b>\n✨ Спочатку підбираємо найбільш сумісних. Обов’язкові Premium-фільтри не послаблюємо.\n🛑 /stop — скасувати.'
    if result:
        await send(bot, uid, result, chat_menu)
        return
    score, _ = await compatibility_score(uid, other)
    await write('INSERT INTO match_events(user_a,user_b,compatibility) VALUES(?,?,?)', (uid, other, score if score is not None else -1))
    for recipient in (uid, other):
        setting = await one('SELECT match_alerts FROM user_notifications WHERE user_id=?', (recipient,))
        if not setting or setting['match_alerts']:
            await send(bot, recipient, '🔔 Знайдено пару!')
    ok1 = await send(bot, uid, await match_description(uid, other), chat_menu)
    ok2 = await send(bot, other, await match_description(other, uid), chat_menu)
    if not (ok1 and ok2):
        await finish_chat(bot, uid)


@dp.message(Command("next"))
@dp.message(F.text == "⏭ Наступний")
async def next_cmd(message: Message, bot: Bot):
    uid = message.from_user.id
    await finish_chat(bot, uid, notify=False)
    await find_partner(bot, uid)


@dp.message(Command("stop"))
@dp.message(F.text == "🛑 Завершити")
async def stop_cmd(message: Message, bot: Bot):
    await finish_chat(bot, message.from_user.id)


@dp.message(F.text == "🔎 Знайти співрозмовника")
async def search_cmd(message: Message, bot: Bot):
    await find_partner(bot, message.from_user.id)


@dp.message(F.text == "🚫 Заблокувати")
async def block_cmd(message: Message, bot: Bot):
    uid = message.from_user.id

    if not await allowed(bot, uid):
        return

    async with lock:
        other = partners.get(uid)

        if other is None:
            await message.answer(
                "💬 Немає активного співрозмовника."
            )
            return

        await write("""
            INSERT OR IGNORE INTO user_blocks
            (blocker_id, blocked_id)
            VALUES(?,?)
        """, (uid, other))

    await finish_chat(bot, uid)

    await send(
        bot,
        uid,
        "🚫 <b>Співрозмовника заблоковано.</b>\n\n"
        "Ви більше не потрапите в одну пару.\n"
        "🔒 Розблокувати можна в чорному списку.",
        menu,
    )


def report_source(uid):
    purge_memory()

    other = partners.get(uid)
    if other is not None:
        return {
            "partner": other,
            "messages": list(incoming[uid]),
            "expires": time.monotonic() + REPORT_WINDOW,
        }

    return recent.get(uid)


@dp.message(Command("report"))
@dp.message(F.text == "⚠️ Поскаржитися")
async def report_cmd(message: Message, bot: Bot):
    uid = message.from_user.id

    if not await allowed(bot, uid):
        return

    async with lock:
        source = report_source(uid)

        if not source:
            await message.answer(
                "⚠️ Немає доступної розмови для скарги.\n\n"
                "Після завершення чату є 5 хвилин "
                "на подання скарги."
            )
            return

        token = secrets.token_hex(5)
        drafts[uid] = {
            "token": token,
            "partner": source["partner"],
            "messages": list(source["messages"]),
            "expires": source["expires"],
            "reason": None,
        }

    await message.answer(
        "⚠️ <b>Скарга на співрозмовника</b>\n\n"
        "Обери причину:",
        reply_markup=inline([
            [(title, f"reason:{token}:{code}")]
            for code, title in REASONS.items()
        ]),
    )


@dp.callback_query(F.data.startswith("reason:"))
async def reason_cb(call: CallbackQuery):
    uid = call.from_user.id

    try:
        _, token, reason = call.data.split(":")
    except ValueError:
        await call.answer("Помилка")
        return

    async with lock:
        purge_memory()
        draft = drafts.get(uid)

        if (
            not draft
            or draft["token"] != token
            or reason not in REASONS
        ):
            await call.answer(
                "Час подання скарги минув.",
                show_alert=True,
            )
            return

        draft["reason"] = reason
        count = len(draft["messages"])

    await call.answer()

    await call.message.answer(
        "📎 <b>Додати докази до скарги?</b>\n\n"
        f"Бот має {count} останніх текстових "
        "повідомлень цього співрозмовника.\n\n"
        "Якщо погодишся, вони будуть збережені "
        "та стануть доступні лише адміністраторам "
        "для розгляду скарги.\n\n"
        "🗑️ Докази видаляються через 30 днів.\n"
        "🔒 Можна подати скаргу без доказів.",
        reply_markup=inline([
            [("📎 Додати докази", f"submit:{token}:yes")],
            [("🔒 Без доказів", f"submit:{token}:no")],
            [("❌ Скасувати", f"cancel:{token}")],
        ]),
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_cb(call: CallbackQuery):
    uid = call.from_user.id
    token = call.data.split(":", 1)[1]

    async with lock:
        draft = drafts.get(uid)
        if draft and draft["token"] == token:
            drafts.pop(uid, None)

    await call.answer("Скасовано")
    await call.message.answer("❌ Скаргу скасовано.")


@dp.callback_query(F.data.startswith("submit:"))
async def submit_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id

    try:
        _, token, choice = call.data.split(":")
    except ValueError:
        await call.answer("Помилка")
        return

    if choice not in ("yes", "no"):
        await call.answer("Невірний вибір")
        return

    if await is_banned(uid):
        await call.answer("Немає доступу", show_alert=True)
        return

    async with lock:
        purge_memory()
        draft = drafts.get(uid)

        if (
            not draft
            or draft["token"] != token
            or not draft["reason"]
        ):
            await call.answer(
                "Скарга вже недоступна.",
                show_alert=True,
            )
            return

        target = draft["partner"]
        reason = draft["reason"]
        evidence = (
            list(draft["messages"])
            if choice == "yes"
            else []
        )

        expires = (
            now_utc() + timedelta(days=EVIDENCE_DAYS)
        ).isoformat()

        # Скарга і всі докази зберігаються однією
        # транзакцією, щоб уникнути неповних записів.
        async with aiosqlite.connect(DB) as db:
            cursor = await db.execute("""
                INSERT INTO moderation_reports
                (reporter_id, reported_id, reason)
                VALUES(?,?,?)
            """, (uid, target, reason))

            report_id = cursor.lastrowid

            if evidence:
                await db.executemany("""
                    INSERT INTO report_evidence
                    (report_id, message_text,
                     message_time, expires_at)
                    VALUES(?,?,?,?)
                """, [
                    (
                        report_id,
                        item["text"],
                        item["time"],
                        expires,
                    )
                    for item in evidence
                ])

            await db.commit()

        drafts.pop(uid, None)

        active = partners.get(uid) == target

    await call.answer("Скаргу надіслано!")

    if active:
        await finish_chat(bot, uid, notify=False)

    await send(
        bot,
        uid,
        "✅ <b>Скаргу прийнято!</b>\n\n"
        f"🛡️ Номер: <b>#{report_id}</b>\n"
        f"📎 Доказів: <b>{len(evidence)}</b>\n\n"
        "Адміністратор зможе переглянути скаргу.\n"
        "Дякуємо, що допомагаєш робити "
        "Інкогніто безпечнішим! 💙",
        menu,
    )

    if active:
        await send(
            bot,
            target,
            "👋 Співрозмовник завершив розмову.\n\n"
            "🔎 /next — нове знайомство.",
            menu,
        )


@dp.message(F.text == "🔒 Чорний список")
async def blacklist(message: Message):
    uid = message.from_user.id

    rows = await all_rows("""
        SELECT id FROM user_blocks
        WHERE blocker_id=?
        ORDER BY id DESC
        LIMIT 30
    """, (uid,))

    if not rows:
        await message.answer(
            "🔒 <b>Чорний список порожній.</b>"
        )
        return

    await message.answer(
        "🔒 <b>МІЙ ЧОРНИЙ СПИСОК</b>\n\n"
        "Натисни на запис, щоб розблокувати:",
        reply_markup=inline([
            [(
                f"🔓 Розблокувати №{row['id']}",
                f"unblock:{row['id']}",
            )]
            for row in rows
        ]),
    )


@dp.callback_query(F.data.startswith("unblock:"))
async def unblock_cb(call: CallbackQuery):
    uid = call.from_user.id

    try:
        block_id = int(call.data.split(":")[1])
    except ValueError:
        await call.answer("Помилка")
        return

    row = await one("""
        SELECT id FROM user_blocks
        WHERE id=? AND blocker_id=?
    """, (block_id, uid))

    if not row:
        await call.answer("Запис не знайдено")
        return

    await write("""
        DELETE FROM user_blocks
        WHERE id=? AND blocker_id=?
    """, (block_id, uid))

    await call.answer("Розблоковано!")
    await call.message.answer(
        f"🔓 Блокування №{block_id} скасовано."
    )


@dp.message(F.text == "👤 Мій профіль")
async def profile(message: Message):
    user = await user_get(message.from_user.id)

    if not user or not user["registered"]:
        await message.answer("Спочатку введи /start.")
        return

    await message.answer(
        "👤 <b>МІЙ ПРОФІЛЬ</b>\n\n"
        f"🙋 {GENDERS.get(user['gender'], '—')}\n"
        f"🎂 Вік: {age_label(user['age'])}\n"
        f"📍 Регіон: {REGIONS.get(user['region'], '—')}\n"
        f"🌍 Пошук: "
        f"{REGIONS.get(user['search_region'], '—')}\n"
        f"⭐ Premium: {await premium_status(message.from_user.id)}\n"
        f"❤️ Мета: {GOALS.get(user.get('goal'), 'Не вибрано')}\n"
        f"🛡️ Репутація: {user.get('reputation',100)}/100\n\n"
        "✨ Змінити дані:",
        reply_markup=inline([
            [("🙋 Стать", "edit:gender")],
            [("🎂 Вік", "edit:age")],
            [("📍 Регіон", "edit:region")],
        ]),
    )


@dp.callback_query(F.data.startswith("edit:"))
async def edit_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    user = await user_get(uid)
    await call.answer()

    if not user or not user["registered"] or await is_banned(uid):
        return

    field = call.data.split(":")[1]

    if field == "gender":
        await show_gender(bot, uid, True)
    elif field == "age":
        await show_age(bot, uid, True)
    elif field == "region":
        await show_region(bot, uid, True)


@dp.message(F.text == "🌍 Пошук за областю")
@dp.message(F.text == "🎯 Фільтри пошуку")
async def search_region(message: Message):
    await filters_menu(message)


@dp.message(F.text == "⭐ Premium")
async def premium(message: Message):
    await show_premium(message)


@dp.message(F.text == "⚙️ Налаштування")
async def settings(message: Message):
    await message.answer(
        "⚙️ <b>НАЛАШТУВАННЯ</b>\n\n"
        "👤 Мій профіль — особисті дані\n"
        "🎯 Фільтри пошуку — область, вік, стать\n"
        "🎯 Мої інтереси — до 5 захоплень\n"
        "❤️ Мета знайомства — обери ціль\n"
        "🧠 /matchmode — режим підбору\n"
        "🔐 /privacy — приватність\n"
        "🎁 /daily — щоденний бонус\n"
        "🔔 /notifications — сповіщення\n"
        "🔒 Чорний список — блокування\n"
        "⚠️ /report — скарга на останнього "
        "співрозмовника\n"
        "⏭ /next — новий пошук\n"
        "🛑 /stop — завершити\n\n"
        "🛡️ <b>Приватність</b>\n"
        "Тимчасовий буфер — до 10 отриманих "
        "повідомлень. Після завершення розмови "
        "він доступний для скарги до 5 хвилин. "
        "Лише добровільно прикріплені докази "
        "зберігаються до 30 днів."
    )


def admin(uid):
    return uid in ADMIN_IDS


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not admin(message.from_user.id):
        return

    pending = await one("""
        SELECT COUNT(*) AS n
        FROM moderation_reports
        WHERE status='pending'
    """)

    await message.answer(
        "🛡️ <b>АДМІНПАНЕЛЬ</b>\n\n"
        f"⚠️ Нові скарги: {pending['n']}\n\n"
        "/reports — переглянути скарги\n"
        "/evidence ID — докази скарги\n"
        "/ban ID HOURS причина — бан (0 = назавжди)\n"
        "/appeals — апеляції\n"
        "/testpremium DAYS [ID] — тестовий доступ\n"
        "/unban ID — розблокувати\n\n"
        "HOURS=0 означає постійний бан."
    )


@dp.message(Command("reports"))
async def admin_reports(message: Message):
    if not admin(message.from_user.id):
        return

    rows = await all_rows("""
        SELECT r.*,
               (SELECT COUNT(*)
                FROM report_evidence e
                WHERE e.report_id=r.id
                  AND e.expires_at>?) AS evidence_count
        FROM moderation_reports r
        WHERE r.status='pending'
        ORDER BY r.id ASC
        LIMIT 10
    """, (iso_now(),))

    if not rows:
        await message.answer("✅ Нових скарг немає.")
        return

    for row in rows:
        await message.answer(
            "⚠️ <b>СКАРГА</b>\n\n"
            f"Номер: <b>#{row['id']}</b>\n"
            f"Користувач: <code>{row['reported_id']}</code>\n"
            f"Заявник: <code>{row['reporter_id']}</code>\n"
            f"Причина: "
            f"{REASONS.get(row['reason'], 'Інше')}\n"
            f"📎 Доказів: {row['evidence_count']}\n"
            f"Дата: {escape(row['created_at'])}",
            reply_markup=inline([
                [(
                    "📎 Переглянути докази",
                    f"admin_evidence:{row['id']}",
                )],
                [(
                    "✅ Закрити без бану",
                    f"review:{row['id']}",
                )],
                [("⛔ Вибрати бан", f"banpick:{row['id']}")],
            ]),
        )


async def show_evidence(bot, admin_id, report_id):
    report = await one(
        "SELECT * FROM moderation_reports WHERE id=?",
        (report_id,),
    )

    if not report:
        await send(bot, admin_id, "Скаргу не знайдено.")
        return

    rows = await all_rows("""
        SELECT message_text, message_time
        FROM report_evidence
        WHERE report_id=? AND expires_at>?
        ORDER BY id ASC
    """, (report_id, iso_now()))

    await send(
        bot,
        admin_id,
        "🛡️ <b>ДОКАЗИ СКАРГИ</b>\n\n"
        f"Номер: #{report_id}\n"
        f"На користувача: "
        f"<code>{report['reported_id']}</code>\n"
        f"Причина: "
        f"{REASONS.get(report['reason'], 'Інше')}\n"
        f"📎 Повідомлень: {len(rows)}\n\n"
        "Нижче наведено текст, який бот "
        "доставив заявнику від цього співрозмовника.",
    )

    if not rows:
        await send(
            bot,
            admin_id,
            "🔒 Доказів немає або термін "
            "їх зберігання минув.",
        )
        return

    for index, row in enumerate(rows, 1):
        text = escape(row["message_text"])
        stamp = escape(row["message_time"])

        # Кожне повідомлення показується окремо,
        # щоб не перевищити ліміт Telegram.
        await send(
            bot,
            admin_id,
            f"📎 <b>Доказ {index}/{len(rows)}</b>\n"
            f"🕒 {stamp}\n\n"
            f"{text}",
        )


@dp.message(Command("evidence"))
async def evidence_cmd(message: Message, bot: Bot):
    if not admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /evidence ID")
        return

    await show_evidence(
        bot,
        message.from_user.id,
        int(parts[1]),
    )


@dp.callback_query(F.data.startswith("admin_evidence:"))
async def evidence_cb(call: CallbackQuery, bot: Bot):
    if not admin(call.from_user.id):
        await call.answer("Немає доступу", show_alert=True)
        return

    try:
        report_id = int(call.data.split(":")[1])
    except ValueError:
        await call.answer("Помилка")
        return

    await call.answer()
    await show_evidence(bot, call.from_user.id, report_id)


@dp.callback_query(F.data.startswith("review:"))
async def review_cb(call: CallbackQuery):
    if not admin(call.from_user.id):
        await call.answer("Немає доступу", show_alert=True)
        return

    report_id = int(call.data.split(":")[1])

    await write("""
        UPDATE moderation_reports
        SET status='reviewed',
            reviewed_at=CURRENT_TIMESTAMP
        WHERE id=? AND status='pending'
    """, (report_id,))

    await call.answer("Розглянуто!")
    await call.message.answer(
        f"✅ Скаргу #{report_id} закрито без бану."
    )


async def apply_ban(bot, admin_id, target, hours, reason):
    if target in ADMIN_IDS:
        return False

    expiry = None
    if hours > 0:
        expiry = (
            now_utc() + timedelta(hours=hours)
        ).isoformat()

    await write("""
        INSERT INTO user_bans
        (user_id, reason, banned_until, admin_id)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            reason=excluded.reason,
            banned_until=excluded.banned_until,
            admin_id=excluded.admin_id,
            created_at=CURRENT_TIMESTAMP
    """, (target, reason, expiry, admin_id))

    await write("""
        INSERT INTO moderation_actions
        (admin_id, target_id, action, reason)
        VALUES(?,?,?,?)
    """, (admin_id, target, "ban", reason))

    # Зниження репутації за підтверджене адміністративне покарання.
    await write('UPDATE users SET reputation=MAX(0,COALESCE(reputation,100)-10) WHERE id=?',(target,))
    await finish_chat(bot, target, notify=False)

    await send(
        bot,
        target,
        "⛔ <b>Доступ до Інкогніто обмежено.</b>\n\n"
        f"Причина: {escape(reason)}\n"
        + (
            f"Тривалість: {hours} год."
            if hours else "Бан безстроковий."
        ),
    )
    return True


@dp.callback_query(F.data.startswith("reportban:"))
async def reportban_cb(call: CallbackQuery, bot: Bot):
    if not admin(call.from_user.id):
        await call.answer("Немає доступу", show_alert=True)
        return

    report_id = int(call.data.split(":")[1])

    row = await one("""
        SELECT * FROM moderation_reports
        WHERE id=? AND status='pending'
    """, (report_id,))

    if not row:
        await call.answer("Скаргу вже оброблено")
        return

    success = await apply_ban(
        bot,
        call.from_user.id,
        row["reported_id"],
        24,
        REASONS.get(row["reason"], "Порушення правил"),
    )

    if not success:
        await call.answer("Адміністратора банити не можна")
        return

    await write("""
        UPDATE moderation_reports
        SET status='reviewed',
            reviewed_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (report_id,))

    await call.answer("Бан застосовано!")
    await call.message.answer(
        f"⛔ Скарга #{report_id}: бан на 24 години."
    )


@dp.message(Command("ban"))
async def ban_cmd(message: Message, bot: Bot):
    if not admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "Формат: /ban ID HOURS причина"
        )
        return

    try:
        target = int(parts[1])
        hours = int(parts[2])
        if target <= 0 or not 0 <= hours <= 87600:
            raise ValueError()
    except ValueError:
        await message.answer("Невірний ID або час.")
        return

    reason = parts[3] if len(parts) > 3 else "Порушення правил"

    success = await apply_ban(
        bot,
        message.from_user.id,
        target,
        hours,
        reason,
    )

    await message.answer(
        "⛔ Бан застосовано."
        if success else "Не можна банити адміністратора."
    )


@dp.message(Command("unban"))
async def unban_cmd(message: Message):
    if not admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /unban ID")
        return

    target = int(parts[1])

    await write(
        "DELETE FROM user_bans WHERE user_id=?",
        (target,),
    )

    await write("""
        INSERT INTO moderation_actions
        (admin_id, target_id, action, reason)
        VALUES(?,?,?,?)
    """, (
        message.from_user.id,
        target,
        "unban",
        "Manual unban",
    ))

    await message.answer("✅ Бан знято.")


# --- Інкогніто 3.3: Premium, Stars, referrals, appeals, filters ---

async def has_premium(uid):
    row = await one('SELECT premium_until FROM users WHERE id=?', (uid,))
    if not row or not row['premium_until']:
        return False
    try:
        d = datetime.fromisoformat(row['premium_until'])
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d > now_utc()
    except ValueError:
        return False


async def premium_status(uid):
    row = await one('SELECT premium_until FROM users WHERE id=?', (uid,))
    if await has_premium(uid):
        return 'активний до ' + escape(row['premium_until'][:10])
    return 'неактивний'


async def add_premium(uid, days):
    async with aiosqlite.connect(DB) as db:
        await db.execute('BEGIN IMMEDIATE')
        row = await (await db.execute('SELECT premium_until FROM users WHERE id=?', (uid,))).fetchone()
        if not row:
            await db.rollback()
            return False
        base = now_utc()
        if row[0]:
            try:
                old = datetime.fromisoformat(row[0])
                if old.tzinfo is None:
                    old = old.replace(tzinfo=timezone.utc)
                base = max(base, old)
            except ValueError:
                pass
        await db.execute('UPDATE users SET premium_until=? WHERE id=?',
                         ((base + timedelta(days=days)).isoformat(), uid))
        await db.commit()
    return True


async def show_premium(message):
    uid = message.from_user.id
    if await is_banned(uid):
        await message.answer('⛔ Premium не скасовує бан. Для апеляції введи /appeal.')
        return
    await message.answer(
        '⭐ <b>ІНКОГНІТО PREMIUM</b>\n\n'
        '📍 Фільтр області\n🎂 Фільтр віку\n🙋 Фільтр статі\n\n'
        f'Статус: {await premium_status(uid)}\n\n'
        '🎁 За кожні 10 запрошених друзів — 1 день Premium безкоштовно.',
        reply_markup=inline([
            [('7 днів — 49 ⭐', 'buy:7')],
            [('30 днів — 149 ⭐', 'buy:30')],
            [('365 днів — 999 ⭐', 'buy:365')],
            [('🎁 Запросити друзів', 'refs:show')],
        ]),
    )


@dp.callback_query(F.data.startswith('buy:'))
async def buy_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    plan = call.data.split(':', 1)[1]
    if plan not in PLANS or await is_banned(uid):
        await call.answer('Недоступно', show_alert=True)
        return
    user = await user_get(uid)
    if not user or not user['registered']:
        await call.answer('Спочатку зареєструйся', show_alert=True)
        return
    days, stars = PLANS[plan]
    await call.answer()
    await bot.send_invoice(
        chat_id=uid, title=f'Інкогніто Premium — {days} днів',
        description=f'Доступ до фільтрів пошуку на {days} днів.',
        payload=f'premium:{plan}:{uid}:{secrets.token_hex(8)}',
        currency='XTR', prices=[LabeledPrice(label='Premium', amount=stars)],
        provider_token='',
    )


@dp.pre_checkout_query()
async def checkout(query: PreCheckoutQuery):
    parts = query.invoice_payload.split(':')
    valid = False
    if len(parts) == 4 and parts[0] == 'premium' and parts[1] in PLANS:
        valid = (parts[2] == str(query.from_user.id)
                 and query.currency == 'XTR'
                 and query.total_amount == PLANS[parts[1]][1]
                 and not await is_banned(query.from_user.id))
    if len(parts) == 4 and parts[0] == 'unban' and parts[1].isdigit():
        appeal = await one('SELECT * FROM ban_appeals WHERE id=? AND user_id=? AND status="approved_paid"',
                           (int(parts[1]), query.from_user.id))
        ban = await one('SELECT * FROM user_bans WHERE user_id=?', (query.from_user.id,))
        valid = bool(appeal and ban and ban['created_at'] == appeal['ban_created_at']
                     and await is_banned(query.from_user.id)
                     and query.currency == 'XTR' and query.total_amount == UNBAN_STARS)
    await query.answer(ok=valid, error_message=None if valid else 'Оплата недоступна. Спробуй ще раз.')


@dp.message(F.successful_payment)
async def paid(message: Message, bot: Bot):
    payment = message.successful_payment
    parts = payment.invoice_payload.split(':')
    uid = message.from_user.id
    if len(parts) == 4 and parts[0] == 'unban':
        await process_unban_payment(message, parts, bot)
        return
    if not (len(parts) == 4 and parts[0] == 'premium'
            and parts[1] in PLANS and parts[2] == str(uid)
            and payment.currency == 'XTR'
            and payment.total_amount == PLANS[parts[1]][1]):
        logging.error('Unrecognized payment for %s: %s', uid, payment.invoice_payload)
        await message.answer('Платіж отримано, але потрібна перевірка адміністратора. Звернися до підтримки.')
        return
    charge = payment.telegram_payment_charge_id
    days, stars = PLANS[parts[1]]
    async with aiosqlite.connect(DB) as db:
        await db.execute('BEGIN IMMEDIATE')
        cur = await db.execute('INSERT OR IGNORE INTO premium_payments(charge_id,user_id,payload,stars) VALUES(?,?,?,?)',
                               (charge, uid, payment.invoice_payload, stars))
        if cur.rowcount:
            row = await (await db.execute('SELECT premium_until FROM users WHERE id=?', (uid,))).fetchone()
            if not row:
                await db.execute('INSERT OR IGNORE INTO users(id) VALUES(?)', (uid,))
                row = (None,)
            base = now_utc()
            if row[0]:
                try:
                    old = datetime.fromisoformat(row[0])
                    if old.tzinfo is None:
                        old = old.replace(tzinfo=timezone.utc)
                    base = max(base, old)
                except ValueError:
                    pass
            await db.execute('UPDATE users SET premium_until=? WHERE id=?',
                             ((base + timedelta(days=days)).isoformat(), uid))
        await db.commit()
    await message.answer(f'✅ Premium активний! {await premium_status(uid)}')


async def complete_referral(uid, bot):
    user = await user_get(uid)
    if not user or not user['registered'] or not user['referrer_id']:
        return
    inviter = user['referrer_id']
    async with aiosqlite.connect(DB) as db:
        await db.execute('BEGIN IMMEDIATE')
        await db.execute('INSERT OR IGNORE INTO referrals(invited_id,referrer_id) VALUES(?,?)', (uid, inviter))
        count = (await (await db.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?', (inviter,))).fetchone())[0]
        old = (await (await db.execute('SELECT referral_awards,premium_until FROM users WHERE id=?', (inviter,))).fetchone())
        award = False
        if old and count // 10 > old[0]:
            base = now_utc()
            if old[1]:
                try:
                    dt = datetime.fromisoformat(old[1])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    base = max(base, dt)
                except ValueError:
                    pass
            await db.execute('UPDATE users SET referral_awards=?,premium_until=? WHERE id=?',
                             (count // 10, (base + timedelta(days=1 * (count // 10 - old[0]))).isoformat(), inviter))
            award = True
        await db.commit()
    if award:
        await send(bot, inviter, '🎁 Ти запросив ще 10 друзів! Додано 1 день Premium.')


@dp.message(F.text == '🎁 Запросити друзів')
async def referrals_menu(message: Message, bot: Bot):
    await show_referrals(bot, message.from_user.id)


@dp.callback_query(F.data == 'refs:show')
async def refs_cb(call: CallbackQuery, bot: Bot):
    await call.answer()
    await show_referrals(bot, call.from_user.id)


async def show_referrals(bot, uid):
    me = await bot.get_me()
    count = await one('SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?', (uid,))
    n = count['n']
    await send(bot, uid,
               f'🎁 <b>МОЇ ЗАПРОШЕННЯ</b>\n\nПідтверджено: {n}\n'
               f'До наступної нагороди: {10 - n % 10}\n'
               f'За кожні 10 друзів — 1 день Premium.\n\n'
               f'Твоє посилання:\n<code>https://t.me/{me.username}?start=ref_{uid}</code>')


async def register_referral(uid, payload):
    if not payload.startswith('ref_'):
        return
    raw = payload[4:]
    if not raw.isdigit():
        return
    inviter = int(raw)
    if inviter == uid or inviter <= 0:
        return
    async with aiosqlite.connect(DB) as db:
        await db.execute('UPDATE users SET referrer_id=? WHERE id=? AND referrer_id IS NULL AND registered=0 AND EXISTS(SELECT 1 FROM users WHERE id=? AND registered=1)',
                         (inviter, uid, inviter))
        await db.commit()


async def filters_menu(message):
    uid = message.from_user.id
    if not await has_premium(uid):
        await message.answer('⭐ Фільтри області, віку та статі доступні в Premium. Режим розумного підбору доступний усім: /matchmode')
        return
    user = await user_get(uid)
    await message.answer(
        '🎯 <b>ФІЛЬТРИ ПОШУКУ</b>\n\n'
        f'📍 {REGIONS.get(user["search_region"], "Вся Україна")}\n'
        f'🎂 {age_label(user["search_age"])}\n'
        f'🙋 {GENDERS.get(user["search_gender"], "Будь-яка")}\n\n'
        'Зміна фільтра оновить поточну чергу.',
        reply_markup=inline([
            [('📍 Область', 'filter:region')],
            [('🎂 Вік', 'filter:age')],
            [('🙋 Стать', 'filter:gender')],
            [('🔄 Скинути', 'filter:reset')],
        ]),
    )


@dp.callback_query(F.data.startswith('filter:'))
async def filter_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    if not await has_premium(uid) or await is_banned(uid):
        await call.answer('Потрібен Premium', show_alert=True)
        return
    key = call.data.split(':', 1)[1]
    await call.answer()
    if key == 'region':
        await send(bot, uid, '📍 Обери область пошуку:', region_keyboard('search'))
    elif key == 'age':
        await send(bot, uid, '🎂 Вік співрозмовника:', inline([[(age_label(a), f'fage:{a}')] for a in ['any', *AGES]]))
    elif key == 'gender':
        await send(bot, uid, '🙋 Стать співрозмовника:', inline([[('Будь-яка', 'fgender:any')]] + [[(label, f'fgender:{code}')] for code, label in GENDERS.items() if code != 'unspecified']))
    elif key == 'reset':
        await user_update(uid, search_region='all', search_age='any', search_gender='any')
        await refresh_waiting(bot, uid)
        await send(bot, uid, '✅ Фільтри скинуто.')


@dp.callback_query(F.data.startswith('fage:') | F.data.startswith('fgender:'))
async def filter_value(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    if not await has_premium(uid) or await is_banned(uid):
        await call.answer('Потрібен Premium', show_alert=True)
        return
    key, val = call.data.split(':', 1)
    if key == 'fage' and val in ['any', *AGES]:
        await user_update(uid, search_age=val)
    elif key == 'fgender' and val in ('any', 'male', 'female'):
        await user_update(uid, search_gender=val)
    else:
        await call.answer('Невірний фільтр')
        return
    await call.answer('Збережено')
    await refresh_waiting(bot, uid)
    await send(bot, uid, '✅ Фільтр змінено.')


async def refresh_waiting(bot, uid):
    async with lock:
        in_queue = uid in waiting
        if in_queue:
            waiting.remove(uid)
    if in_queue:
        await find_partner(bot, uid)


@dp.message(Command('testpremium'))
async def testpremium_cmd(message: Message):
    uid = message.from_user.id
    if not admin(uid):
        return
    parts = message.text.split()
    try:
        days = int(parts[1])
        target = int(parts[2]) if len(parts) > 2 else uid
        if not 1 <= days <= 3650 or target <= 0:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer('Формат: /testpremium DAYS [USER_ID]')
        return
    if await add_premium(target, days):
        await message.answer(f'⭐ Додано {days} днів Premium користувачу {target}.')
    else:
        await message.answer('Користувач не знайдений.')


@dp.message(Command('appeal'))
async def appeal_cmd(message: Message, bot: Bot):
    uid = message.from_user.id
    ban = await one('SELECT * FROM user_bans WHERE user_id=?', (uid,))
    if not ban or not await is_banned(uid):
        await message.answer('✅ У тебе немає активного бану.')
        return
    previous = await one('SELECT * FROM ban_appeals WHERE user_id=? AND ban_created_at=? ORDER BY id DESC LIMIT 1',
                         (uid, ban['created_at']))
    if previous:
        await message.answer(f'📝 Апеляція #{previous["id"]}: {previous["status"]}.')
        return
    appeal_id = await write('INSERT INTO ban_appeals(user_id,ban_created_at) VALUES(?,?)', (uid, ban['created_at']))
    await message.answer(f'📝 Апеляцію #{appeal_id} надіслано. Адміністратор може відмовити, зняти бан безкоштовно або дозволити платний розбан.')
    for aid in ADMIN_IDS:
        await send(bot, aid, f'📝 Нова апеляція #{appeal_id}, користувач <code>{uid}</code>. /appeals')


@dp.message(Command('appeals'))
async def appeals_admin(message):
    if not admin(message.from_user.id):
        return
    rows = await all_rows('SELECT a.*,b.reason FROM ban_appeals a LEFT JOIN user_bans b ON b.user_id=a.user_id WHERE a.status="pending" ORDER BY a.id LIMIT 15')
    if not rows:
        await message.answer('Апеляцій немає.')
    for row in rows:
        await message.answer(f'📝 Апеляція #{row["id"]}\nID: <code>{row["user_id"]}</code>\nПричина: {escape(row["reason"] or "—")}',
                             reply_markup=inline([
                                 [('✅ Зняти безкоштовно', f'apdec:{row["id"]}:free')],
                                 [('⭐ Дозволити 99 Stars', f'apdec:{row["id"]}:paid')],
                                 [('❌ Відхилити', f'apdec:{row["id"]}:deny')],
                             ]))


@dp.callback_query(F.data.startswith('apdec:'))
async def appeal_decision(call: CallbackQuery, bot: Bot):
    if not admin(call.from_user.id):
        await call.answer('Немає доступу')
        return
    _, rid, decision = call.data.split(':')
    row = await one('SELECT * FROM ban_appeals WHERE id=? AND status="pending"', (int(rid),))
    if not row:
        await call.answer('Уже оброблено')
        return
    ban = await one('SELECT * FROM user_bans WHERE user_id=?', (row['user_id'],))
    if not ban or ban['created_at'] != row['ban_created_at'] or not await is_banned(row['user_id']):
        await write('UPDATE ban_appeals SET status="expired" WHERE id=?', (row['id'],))
        await call.answer('Бан уже неактуальний')
        return
    if decision == 'paid' and any(word in (ban['reason'] or '').lower() for word in ('шахрай', 'погроз', 'fraud', 'threat')):
        await call.answer('Для тяжких порушень платний розбан недоступний', show_alert=True)
        return
    if decision not in ('paid', 'free', 'deny'):
        await call.answer('Невірна дія')
        return
    status = {'paid': 'approved_paid', 'free': 'approved_free', 'deny': 'denied'}[decision]
    await write('UPDATE ban_appeals SET status=?,decided_by=?,decided_at=? WHERE id=? AND status="pending"',
                (status, call.from_user.id, iso_now(), row['id']))
    if decision == 'free':
        await write('DELETE FROM user_bans WHERE user_id=? AND created_at=?', (row['user_id'], row['ban_created_at']))
        await send(bot, row['user_id'], '✅ Твою апеляцію схвалено. Бан знято безкоштовно.')
    elif decision == 'paid':
        await send(bot, row['user_id'], '⭐ Адміністратор дозволив достроковий розбан за 99 Stars. Оплата добровільна.',
                   inline([[('⭐ Оплатити 99 Stars', f'unbanpay:{row["id"]}')]]))
    else:
        await send(bot, row['user_id'], '❌ Апеляцію відхилено. Бан залишається чинним.')
    await call.answer('Рішення збережено')


@dp.callback_query(F.data.startswith('unbanpay:'))
async def unbanpay_cb(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    appeal_id = int(call.data.split(':')[1])
    appeal = await one('SELECT * FROM ban_appeals WHERE id=? AND user_id=? AND status="approved_paid"',
                       (appeal_id, uid))
    ban = await one('SELECT * FROM user_bans WHERE user_id=?', (uid,))
    if not appeal or not ban or appeal['ban_created_at'] != ban['created_at'] or not await is_banned(uid):
        await call.answer('Дозвіл уже неактуальний', show_alert=True)
        return
    await call.answer()
    await bot.send_invoice(chat_id=uid, title='Дострокове зняття бану',
                           description='Розбан після схвалення апеляції адміністратором.',
                           payload=f'unban:{appeal_id}:{uid}:{secrets.token_hex(8)}',
                           currency='XTR', prices=[LabeledPrice(label='Розбан', amount=UNBAN_STARS)],
                           provider_token='')


async def process_unban_payment(message, parts, bot):
    uid = message.from_user.id
    pay = message.successful_payment
    charge = pay.telegram_payment_charge_id
    if not (parts[1].isdigit() and parts[2] == str(uid)
            and pay.currency == 'XTR' and pay.total_amount == UNBAN_STARS):
        await message.answer('⚠️ Платіж потребує ручної перевірки. Звернися до адміністратора.')
        return
    appeal_id = int(parts[1])
    processed = False
    async with aiosqlite.connect(DB) as db:
        await db.execute('BEGIN IMMEDIATE')
        cur = await db.execute('INSERT OR IGNORE INTO premium_payments(charge_id,user_id,payload,stars) VALUES(?,?,?,?)',
                               (charge, uid, pay.invoice_payload, UNBAN_STARS))
        if not cur.rowcount:
            await db.commit()
            await message.answer('Цей платіж уже оброблено.')
            return
        appeal = await (await db.execute('SELECT ban_created_at FROM ban_appeals WHERE id=? AND user_id=? AND status="approved_paid"',
                                         (appeal_id, uid))).fetchone()
        if appeal:
            deleted = await db.execute('DELETE FROM user_bans WHERE user_id=? AND created_at=?', (uid, appeal[0]))
            if deleted.rowcount:
                await db.execute('UPDATE ban_appeals SET status="paid_unbanned" WHERE id=?', (appeal_id,))
                processed = True
        await db.commit()
    if processed:
        await message.answer('✅ Оплату підтверджено. Бан знято!')
        for aid in ADMIN_IDS:
            await send(bot, aid, f'⭐ Користувача {uid} розблоковано за схваленою апеляцією #{appeal_id}.')
    else:
        # Якщо бан знято або змінено між підтвердженням рахунку і оплатою,
        # повертаємо Stars, а не залишаємо оплату без послуги.
        try:
            await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge)
            await message.answer('ℹ️ Дозвіл уже неактуальний. Stars повернено.')
        except TelegramAPIError:
            logging.exception('Не вдалося автоматично повернути Stars %s', charge)
            await message.answer('⚠️ Послуга не застосована. Потрібне ручне повернення Stars адміністрацією.')


@dp.callback_query(F.data.startswith('banpick:'))
async def ban_pick(call: CallbackQuery):
    if not admin(call.from_user.id):
        await call.answer('Немає доступу')
        return
    report_id = int(call.data.split(':')[1])
    await call.answer()
    await call.message.answer('⛔ Обери тривалість бану:', reply_markup=inline([
        [('1 година', f'banterm:{report_id}:1'), ('24 години', f'banterm:{report_id}:24')],
        [('3 дні', f'banterm:{report_id}:72'), ('7 днів', f'banterm:{report_id}:168')],
        [('30 днів', f'banterm:{report_id}:720')],
        [('Назавжди', f'banterm:{report_id}:0')],
    ]))


@dp.callback_query(F.data.startswith('banterm:'))
async def ban_term(call: CallbackQuery, bot: Bot):
    if not admin(call.from_user.id):
        await call.answer('Немає доступу')
        return
    _, report_id, hours = call.data.split(':')
    hours = int(hours)
    if hours not in (0, 1, 24, 72, 168, 720):
        await call.answer('Невірний термін')
        return
    report = await one('SELECT * FROM moderation_reports WHERE id=? AND status="pending"', (int(report_id),))
    if not report:
        await call.answer('Скаргу вже розглянуто')
        return
    if not await apply_ban(bot, call.from_user.id, report['reported_id'], hours,
                           REASONS.get(report['reason'], 'Порушення правил')):
        await call.answer('Не можна банити адміністратора')
        return
    await write('UPDATE moderation_reports SET status="reviewed",reviewed_at=CURRENT_TIMESTAMP WHERE id=?', (int(report_id),))
    await call.answer('Бан застосовано')
    await call.message.answer(f'⛔ Бан користувача {report["reported_id"]}: {hours or "назавжди"} год.')



@dp.message(F.text == '🎯 Мої інтереси')
@dp.message(Command('interests'))
async def interests_menu(message: Message):
    uid = message.from_user.id
    if await is_banned(uid):
        return
    user = await user_get(uid)
    if not user or not user['registered']:
        await message.answer('Спочатку заверши реєстрацію через /start.')
        return
    chosen = {r['interest'] for r in await all_rows('SELECT interest FROM user_interests WHERE user_id=?', (uid,))}
    await message.answer('🎯 <b>ТВОЇ ІНТЕРЕСИ</b>\n\nОбери до 5 інтересів. Натисни повторно, щоб прибрати.\nВибрано: ' + str(len(chosen)) + '/5', reply_markup=inline([[(('✅ ' if key in chosen else '') + label, 'interest:' + key)] for key,label in INTERESTS.items()] + [[('🗑 Очистити всі інтереси', 'interest:clear')]]))

@dp.callback_query(F.data.startswith('interest:'))
async def interest_toggle(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    key = call.data.split(':',1)[1]
    if key not in (*INTERESTS, 'clear') or await is_banned(uid):
        await call.answer('Недоступно', show_alert=True)
        return
    async with aiosqlite.connect(DB) as db:
        if key == 'clear':
            await db.execute('DELETE FROM user_interests WHERE user_id=?', (uid,))
            await db.commit()
            await call.answer('Інтереси очищено')
            await call.message.edit_reply_markup(reply_markup=inline([[('🎯 ' + label, 'interest:' + k)] for k,label in INTERESTS.items()] + [[('🗑 Очистити всі інтереси', 'interest:clear')]]))
            await refresh_waiting(bot, uid)
            return
        found = await (await db.execute('SELECT 1 FROM user_interests WHERE user_id=? AND interest=?', (uid,key))).fetchone()
        if found:
            await db.execute('DELETE FROM user_interests WHERE user_id=? AND interest=?',(uid,key))
        else:
            count = (await (await db.execute('SELECT COUNT(*) FROM user_interests WHERE user_id=?',(uid,))).fetchone())[0]
            if count >= 5:
                await call.answer('Можна вибрати не більше 5 інтересів',show_alert=True)
                return
            await db.execute('INSERT OR IGNORE INTO user_interests(user_id,interest) VALUES(?,?)',(uid,key))
        await db.commit()
    await call.answer('Збережено')
    chosen = {r['interest'] for r in await all_rows('SELECT interest FROM user_interests WHERE user_id=?',(uid,))}
    await call.message.edit_reply_markup(reply_markup=inline([[(('✅ ' if k in chosen else '') + label, 'interest:' + k)] for k,label in INTERESTS.items()] + [[('🗑 Очистити всі інтереси', 'interest:clear')]]))
    await refresh_waiting(bot,uid)

@dp.message(F.text == '❤️ Мета знайомства')
@dp.message(Command('goal'))
async def goal_menu(message: Message):
    if await is_banned(message.from_user.id):
        return
    user = await user_get(message.from_user.id)
    if not user or not user['registered']:
        await message.answer('Спочатку заверши реєстрацію через /start.')
        return
    current = user.get('goal')
    buttons = [[(('✅ ' if key == current else '') + name, 'goal:' + key)] for key, name in GOALS.items()] + [[('🗑 Видалити мету', 'goal:clear')]]
    await message.answer('❤️ <b>Для чого шукаєш знайомства?</b>\n\nПоточна мета: ' + GOALS.get(current, 'Не вибрано'), reply_markup=inline(buttons))

@dp.callback_query(F.data.startswith('goal:'))
async def goal_choose(call: CallbackQuery, bot: Bot):
    uid=call.from_user.id
    key=call.data.split(':',1)[1]
    user=await user_get(uid)
    if key not in (*GOALS, 'clear') or not user or not user['registered'] or await is_banned(uid):
        await call.answer('Недоступно',show_alert=True)
        return
    await user_update(uid,goal=None if key == 'clear' else key)
    await call.answer('Збережено')
    await call.message.edit_text('✅ <b>Мета знайомства:</b> ' + GOALS.get(key, 'Не вибрано'))
    await send(bot,uid,'❤️ Твоя мета знайомства: '+GOALS.get(key, 'Не вибрано'))
    await refresh_waiting(bot,uid)

@dp.message(F.text == '💕 Запропонувати знайомство')
@dp.message(F.text == '💕 Симпатія')  # Сумісність зі старою клавіатурою
@dp.message(Command('like'))
async def like_cmd(message: Message, bot: Bot):
    if not (await user_get(message.from_user.id) or {}).get('allow_contact', 1):
        await message.answer('🔐 Увімкни запити на знайомство в меню «🔐 Приватність».')
        return
    uid = message.from_user.id
    if not await allowed(bot, uid):
        return
    async with lock:
        other = partners.get(uid)
        if other is None:
            await message.answer('💕 Надіслати запит можна лише під час активного чату.')
            return
        peer_settings = await user_get(other)
        if not peer_settings or not peer_settings.get('allow_contact', 1):
            await message.answer('🔐 Співрозмовник вимкнув запити на знайомство.')
            return
        existing = await one("""SELECT id, requester_id, recipient_id, status FROM contact_requests
            WHERE ((requester_id=? AND recipient_id=?) OR (requester_id=? AND recipient_id=?))
              AND status IN ('pending', 'accepted', 'declined')
            ORDER BY id DESC LIMIT 1""", (uid, other, other, uid))
        if existing:
            if existing['status'] == 'pending':
                text = ('⏳ Твій запит уже надіслано. Очікуй відповіді.' if existing['requester_id'] == uid
                        else '💕 Співрозмовник уже надіслав тобі запит. Скористайся кнопками в його повідомленні.')
            elif existing['status'] == 'accepted':
                text = '💕 Ви вже погодилися обмінятися профілями.'
            else:
                text = '💕 Запит уже було відхилено. Повторно турбувати цю людину в цьому чаті не можна.'
            await message.answer(text)
            return
        await write("INSERT INTO contact_requests(requester_id,recipient_id,status) VALUES(?,?,'pending')", (uid,other))
        row = await one("SELECT id FROM contact_requests WHERE requester_id=? AND recipient_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (uid,other))
        request_id = row['id']
    await message.answer('💕 Запит на знайомство надіслано! Очікуй відповіді. Твій профіль поки приховано.')
    ok = await send(bot, other,
        '💕 <b>Тобі запропонували знайомство!</b>\n\n'
        'Співрозмовник хоче обмінятися Telegram-профілями.\n'
        'Якщо приймеш, <b>ви обоє</b> отримаєте посилання на профілі один одного.\n'
        'Якщо відхилиш, контакти не відкриються.',
        inline([[('✅ Прийняти',f'contact:accept:{request_id}'),('❌ Відхилити',f'contact:decline:{request_id}')]]))
    if not ok:
        await write("UPDATE contact_requests SET status='expired', resolved_at=? WHERE id=? AND status='pending'", (iso_now(),request_id))
        await message.answer('⚠️ Не вдалося доставити запит. Спробуй пізніше.')


@dp.callback_query(F.data.startswith('contact:'))
async def contact_decision(call: CallbackQuery, bot: Bot):
    parts = call.data.split(':')
    if len(parts) != 3 or parts[1] not in ('accept','decline') or not parts[2].isdigit():
        await call.answer('Некоректний запит.', show_alert=True)
        return
    request_id = int(parts[2])
    uid = call.from_user.id
    async with lock:
        request = await one('SELECT * FROM contact_requests WHERE id=?', (request_id,))
        if not request or request['recipient_id'] != uid:
            await call.answer('Цей запит не для тебе.', show_alert=True)
            return
        if request['status'] != 'pending':
            await call.answer('Запит уже оброблено або він застарів.', show_alert=True)
            return
        other = request['requester_id']
        if partners.get(uid) != other or partners.get(other) != uid:
            await write("UPDATE contact_requests SET status='expired',resolved_at=? WHERE id=? AND status='pending'", (iso_now(),request_id))
            await call.answer('Чат уже завершено. Запит недійсний.', show_alert=True)
            return
        recipient_settings = await user_get(uid)
        if not recipient_settings or not recipient_settings.get('allow_contact', 1) or await is_banned(uid) or await is_banned(other) or await is_blocked(uid, other):
            await call.answer('Обмін контактами недоступний.', show_alert=True)
            return
        status = 'accepted' if parts[1] == 'accept' else 'declined'
        await write("UPDATE contact_requests SET status=?,resolved_at=? WHERE id=? AND status='pending'", (status,iso_now(),request_id))
    await call.answer('Відповідь збережено.')
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if status == 'declined':
        await send(bot, uid, '❌ Запит відхилено. Твій Telegram-профіль залишився прихованим.')
        await send(bot, other, '💔 Співрозмовник відхилив запит. Контакти не розкрито.')
        return
    # Відкриваємо контакти тільки після явного прийняття запиту.
    # Посилання за числовим ID у Telegram може залежати від клієнта та налаштувань приватності.
    for recipient, target in ((uid,other),(other,uid)):
        try:
            chat = await bot.get_chat(target)
            username = chat.username
        except TelegramAPIError:
            username = None
        if username:
            url = 'https://t.me/' + username
            label = '@' + username
        else:
            url = 'tg://user?id=' + str(target)
            label = 'Відкрити профіль у Telegram'
        await send(bot, recipient,
            '💕 <b>Запит прийнято!</b>\n\n'
            'Ви обоє погодилися обмінятися профілями.\n'
            f'👤 <a href="{escape(url, quote=True)}">{escape(label)}</a>\n\n'
            'ℹ️ Якщо посилання не відкривається на твоєму телефоні, '
            'це може залежати від налаштувань приватності Telegram. '
            'Попроси співрозмовника поділитися своїм @username у чаті.')


@dp.message(Command('notifications'))
async def notifications_cmd(message: Message):
    uid=message.from_user.id
    await write('INSERT OR IGNORE INTO user_notifications(user_id) VALUES(?)',(uid,))
    row=await one('SELECT match_alerts FROM user_notifications WHERE user_id=?',(uid,))
    await message.answer('🔔 Сповіщення про знайдену пару: '+('увімкнено' if row['match_alerts'] else 'вимкнено'),reply_markup=inline([[('🔕 Вимкнути' if row['match_alerts'] else '🔔 Увімкнути','notify:toggle')]]))

@dp.callback_query(F.data=='notify:toggle')
async def notifications_toggle(call: CallbackQuery):
    uid=call.from_user.id
    await write('INSERT OR IGNORE INTO user_notifications(user_id) VALUES(?)',(uid,))
    await write('UPDATE user_notifications SET match_alerts=1-match_alerts WHERE user_id=?',(uid,))
    row=await one('SELECT match_alerts FROM user_notifications WHERE user_id=?',(uid,))
    await call.answer('Збережено')
    await call.message.answer('🔔 Сповіщення '+('увімкнено' if row['match_alerts'] else 'вимкнено'))


@dp.message(F.text == '🟢 Онлайн')
@dp.message(Command('online'))
async def online_cmd(message: Message):
    async with lock:
        n_wait, n_chats = len(waiting), len(partners) // 2
    await message.answer(f'🟢 <b>Зараз у боті</b>\n🔎 Шукають співрозмовника: {n_wait}\n💬 Активних розмов: {n_chats}\n\n🔒 Без розкриття особистостей.')


@dp.message(F.text == '🔐 Приватність')
@dp.message(Command('privacy'))
async def privacy_menu(message: Message):
    uid = message.from_user.id
    u = await user_get(uid)
    if not u or not u['registered']:
        await message.answer('Спочатку введи /start.')
        return
    opts = [('show_age', '🎂 Показувати вік'), ('show_region', '📍 Показувати область'), ('show_interests', '🎯 Показувати інтереси'), ('show_goal', '❤️ Показувати мету'), ('allow_contact', '💕 Дозволяти запити на знайомство')]
    await message.answer('🔐 <b>НАЛАШТУВАННЯ ПРИВАТНОСТІ</b>\nДані можна враховувати в підборі, навіть якщо приховано їх від співрозмовника.', reply_markup=inline([[(('✅ ' if u.get(key, 1) else '❌ ') + label, 'privacy:' + key)] for key,label in opts]))


@dp.callback_query(F.data.startswith('privacy:'))
async def privacy_toggle(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    key = call.data.split(':', 1)[1]
    if key not in ('show_age', 'show_region', 'show_interests', 'show_goal', 'allow_contact') or await is_banned(uid):
        await call.answer('Недоступно', show_alert=True)
        return
    u = await user_get(uid)
    if not u or not u['registered']:
        await call.answer('Спочатку зареєструйся', show_alert=True)
        return
    await user_update(uid, **{key: 0 if u.get(key) else 1})
    await call.answer('Налаштування збережено')
    opts = [('show_age', '🎂 Показувати вік'), ('show_region', '📍 Показувати область'), ('show_interests', '🎯 Показувати інтереси'), ('show_goal', '❤️ Показувати мету'), ('allow_contact', '💕 Дозволяти запити на знайомство')]
    u = await user_get(uid)
    await call.message.edit_reply_markup(reply_markup=inline([[(('✅ ' if u.get(k) else '❌ ') + label, 'privacy:' + k)] for k,label in opts]))
    await call.message.answer('🔐 Налаштування приватності оновлено. ' + ('Увімкнено' if u.get(key) else 'Вимкнено') + ': ' + dict(opts)[key])


def reward_for_streak(streak):
    if streak <= 7:
        return REWARD_FIRST_WEEK[max(1, streak)-1]
    if streak <= 14:
        return 20
    if streak <= 30:
        return 25
    if streak <= 60:
        return 30
    if streak <= 90:
        return 35
    if streak <= 180:
        return 40
    if streak <= 365:
        return 45
    return 50


def bonus_shop_markup():
    return inline([[(f'⭐ {days} дн. — {price:,} балів'.replace(',', ' '), f'bonusbuy:{key}')]
                   for key, (days, price) in REWARD_PLANS.items()])


@dp.message(F.text == '🎁 Щоденний бонус')
@dp.message(Command('daily'))
async def daily_bonus(message: Message):
    uid = message.from_user.id
    if await is_banned(uid):
        await message.answer('⛔ Доступ обмежено.')
        return
    today = datetime.now(KYIV).date()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        cursor = await db.execute('SELECT registered, bonus_points, bonus_last, bonus_streak, bonus_shields, bonus_last_shield_at FROM users WHERE id=?', (uid,))
        row = await cursor.fetchone()
        if not row or not row['registered']:
            await db.rollback()
            await message.answer('Спочатку зареєструйся через /start.')
            return
        previous = row['bonus_last']
        if previous == today.isoformat():
            await db.rollback()
            await message.answer(f'🎁 Сьогодні нагороду вже отримано. Баланс: {row["bonus_points"] or 0} балів. Серія: {row["bonus_streak"] or 0} днів.', reply_markup=bonus_shop_markup())
            return
        try:
            gap = (today - datetime.fromisoformat(previous).date()).days if previous else None
        except ValueError:
            gap = None
        old_streak = max(0, row['bonus_streak'] or 0)
        shields = max(0, row['bonus_shields'] or 0)
        last_shield_at = max(0, row['bonus_last_shield_at'] or 0)
        note = ''
        if gap == 1:
            streak = old_streak + 1
        elif gap == 2 and shields > 0:
            streak = old_streak + 1
            shields -= 1
            note = '\n🛡 Один пропуск захищено!'
        elif gap == 2:
            streak = max(1, old_streak // 2 + 1)
            note = '\n⚠️ Серію зменшено через пропуск.'
        elif gap == 3:
            streak = max(1, old_streak // 4 + 1)
            note = '\n⚠️ Серію зменшено через два пропуски.'
        else:
            streak = 1
            if previous:
                note = '\n⚠️ Серію розпочато заново.'
        # Один захист за кожні 30 досягнутих днів без повторного отримання
        # за той самий поріг після зниження серії. Максимум 1 у запасі.
        if streak >= 30 and streak // 30 > last_shield_at:
            shields = min(1, shields + 1)
            last_shield_at = streak // 30
            note += '\n🛡 Отримано захист одного пропуску!'
        reward = reward_for_streak(streak)
        await db.execute('UPDATE users SET bonus_points=COALESCE(bonus_points,0)+?, bonus_last=?, bonus_streak=?, bonus_shields=?, bonus_last_shield_at=? WHERE id=?', (reward, today.isoformat(), streak, shields, last_shield_at, uid))
        await db.commit()
        balance = (row['bonus_points'] or 0) + reward
    await message.answer(f'🎁 +{reward} балів!\n🔥 Серія: {streak} днів\n💰 Баланс: {balance} балів\n🛡 Захист пропуску: {shields}{note}\n\n🛍 Обміняти бали на Premium:', reply_markup=bonus_shop_markup())


@dp.message(F.text == '🛍 Магазин бонусів')
@dp.message(Command('bonusshop'))
async def bonus_shop(message: Message):
    uid = message.from_user.id
    user = await user_get(uid)
    if not user or not user['registered'] or await is_banned(uid):
        await message.answer('Спочатку зареєструйся через /start.')
        return
    await message.answer(f'🛍 <b>МАГАЗИН БОНУСІВ</b>\n\n💰 Твій баланс: <b>{user.get("bonus_points") or 0}</b> балів\n\n⭐ 1 день — 200 балів\n⭐ 7 днів — 2 500 балів\n⭐ 30 днів — 15 000 балів\n\nPremium продовжується від поточної дати завершення, якщо він уже активний. Після покупки повернути бали не можна.', reply_markup=bonus_shop_markup())


@dp.callback_query(F.data.startswith('bonusbuy:'))
async def bonus_buy(call: CallbackQuery):
    uid = call.from_user.id
    key = call.data.split(':', 1)[1]
    if key not in REWARD_PLANS or await is_banned(uid):
        await call.answer('Недоступно', show_alert=True)
        return
    days, price = REWARD_PLANS[key]
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        cur = await db.execute('SELECT registered, bonus_points, premium_until FROM users WHERE id=?', (uid,))
        user = await cur.fetchone()
        if not user or not user['registered']:
            await db.rollback()
            await call.answer('Спочатку зареєструйся', show_alert=True)
            return
        if (user['bonus_points'] or 0) < price:
            await db.rollback()
            await call.answer(f'Недостатньо балів. Потрібно {price}.', show_alert=True)
            return
        base = now_utc()
        if user['premium_until']:
            try:
                previous = datetime.fromisoformat(user['premium_until'])
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=timezone.utc)
                base = max(base, previous)
            except ValueError:
                pass
        until = (base + timedelta(days=days)).isoformat()
        await db.execute('UPDATE users SET bonus_points=bonus_points-?, premium_until=? WHERE id=? AND bonus_points>=?', (price, until, uid, price))
        await db.commit()
        balance = (user['bonus_points'] or 0) - price
    await call.answer('Premium активовано!')
    await call.message.answer(f'✅ <b>Premium на {days} дн. активовано!</b>\n⭐ Діє до: {escape(until[:16])} UTC\n💰 Списано: {price} балів\n💰 Залишок: {balance} балів')


@dp.callback_query(F.data.startswith('rate:'))
async def rating_cb(call: CallbackQuery):
    uid = call.from_user.id
    try:
        _, peer, value = call.data.split(':')
        peer = int(peer)
    except (ValueError, TypeError):
        await call.answer('Помилка')
        return
    if value not in ('great', 'good', 'bad', 'skip') or peer == uid:
        await call.answer('Невірна оцінка')
        return
    # Перевіряємо, що цей партнер дійсно був у завершеній розмові.
    if not (recent.get(uid, {}).get('partner') == peer) or recent.get(uid, {}).get('rating_done'):
        await call.answer('Час оцінювання минув', show_alert=True)
        return
    if value != 'skip':
        await write('INSERT INTO chat_ratings(rater_id,partner_id,rating) VALUES(?,?,?)', (uid, peer, value))
    recent[uid]['rating_done'] = True
    await call.answer('Дякуємо!')
    await call.message.edit_reply_markup(reply_markup=None)


@dp.message(Command('stats'))
async def stats_cmd(message: Message):
    if not admin(message.from_user.id):
        return
    users = await one('SELECT COUNT(*) n, SUM(CASE WHEN premium_until>? THEN 1 ELSE 0 END) premium FROM users WHERE registered=1', (iso_now(),))
    pairs = await one('SELECT COUNT(*) n FROM match_events')
    daily = await one("SELECT COUNT(*) n FROM match_events WHERE created_at>=datetime('now','-1 day')")
    reports = await one("SELECT COUNT(*) n FROM moderation_reports WHERE status='pending'")
    ratings = await all_rows('SELECT rating, COUNT(*) n FROM chat_ratings GROUP BY rating')
    async with lock:
        queue, chats = len(waiting), len(partners)//2
    await message.answer('📊 <b>АНАЛІТИКА ІНКОГНІТО</b>\n\n'
                         f'👤 Зареєстровано: {users["n"]}\n⭐ Активний Premium: {users["premium"] or 0}\n'
                         f'💞 Пар загалом: {pairs["n"]}\n📅 Пар за 24 години: {daily["n"]}\n'
                         f'🔎 У черзі: {queue}\n💬 Активних чатів: {chats}\n⚠️ Нових скарг: {reports["n"]}\n'
                         '👍 Оцінки: ' + ', '.join(f'{r["rating"]}: {r["n"]}' for r in ratings))


@dp.message(Command('matchmode'))
async def matchmode_cmd(message: Message):
    u = await user_get(message.from_user.id)
    if not u or not u['registered']:
        await message.answer('Спочатку введи /start.')
        return
    current = u.get('match_mode') or 'balanced'
    await message.answer('⚙️ <b>РЕЖИМ ПІДБОРУ</b>\nПошук не послаблює Premium-фільтри без дозволу.', reply_markup=inline([[(('✅ ' if current == k else '') + name, 'mode:' + k)] for k,name in [('balanced','🧠 Збалансований'),('strict','🎯 Суворий'),('fast','🎲 Швидкий')]]))


@dp.callback_query(F.data.startswith('mode:'))
async def mode_cb(call: CallbackQuery, bot: Bot):
    key = call.data.split(':', 1)[1]
    if key not in ('balanced', 'strict', 'fast'):
        await call.answer('Помилка')
        return
    await user_update(call.from_user.id, match_mode=key)
    await call.answer('Збережено')
    await call.message.edit_text('✅ Режим підбору: ' + {'balanced':'Збалансований','strict':'Суворий','fast':'Швидкий'}[key])
    await refresh_waiting(bot, call.from_user.id)

# Нові функції 3.3 додаються до обробника повідомлень.
@dp.message()
async def relay(message: Message, bot: Bot):
    if message.chat.type != "private":
        return

    uid = message.from_user.id

    if not await allowed(bot, uid):
        return

    user = await user_get(uid)
    if not user or not user["registered"]:
        await message.answer("👋 Спочатку введи /start.")
        return

    if message.text and message.text.startswith("/"):
        await message.answer(
            "❔ Невідома команда.\n\n"
            "/next — новий пошук\n"
            "/stop — завершити\n"
            "/report — поскаржитися"
        )
        return

    if not message.text:
        await message.answer(
            "📩 Поки підтримуються лише текстові повідомлення."
        )
        return

    if len(message.text) > 3000:
        await message.answer(
            "⚠️ Повідомлення занадто довге."
        )
        return

    current = time.monotonic()
    if current - last_message.get(uid, 0) < 0.7:
        await message.answer(
            "⏳ Надсилай повідомлення трохи повільніше."
        )
        return

    last_message[uid] = current
    burst = message_bursts[uid]
    while burst and current - burst[0] > 30:
        burst.popleft()
    if len(burst) >= 20:
        await message.answer('🛡️ Забагато повідомлень за короткий час. Зачекай 30 секунд.')
        return
    burst.append(current)

    async with lock:
        other = partners.get(uid)

        if other is None:
            if uid in waiting:
                text = "⏳ Ще шукаємо співрозмовника."
            else:
                text = "🔎 Спочатку знайди співрозмовника."

            await message.answer(text)
            return

        if await is_banned(other):
            await message.answer(
                "⚠️ Співрозмовник зараз недоступний.\n"
                "Натисни /next."
            )
            return

        try:
            # Відправляємо текст від імені бота.
            await bot.send_message(
                other,
                message.text,
                parse_mode=None,
            )
        except TelegramAPIError:
            await message.answer(
                "⚠️ Не вдалося доставити повідомлення."
            )
            return

        # Записуємо лише успішно доставлений текст.
        # У базу даних він НЕ потрапляє.
        incoming[other].append({
            "text": message.text,
            "time": iso_now(),
        })


async def main():
    await init_db()
    await cleanup_database()
    # Після перезапуску всі анонімні чати в пам'яті завершені.
    await write("UPDATE contact_requests SET status='expired', resolved_at=? WHERE status='pending'", (iso_now(),))

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    async with bot:
        await bot.set_my_commands([
            BotCommand(
                command="start",
                description="🎭 Головне меню",
            ),
            BotCommand(
                command="next",
                description="⏭ Наступний співрозмовник",
            ),
            BotCommand(
                command="stop",
                description="🛑 Завершити розмову",
            ),
            BotCommand(
                command="report",
                description="⚠️ Поскаржитися",
            ),
        ])

        cleanup_task = asyncio.create_task(cleanup_loop())

        try:
            logging.info("Інкогніто 3.8.2 запущено")
            await dp.start_polling(bot)
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
