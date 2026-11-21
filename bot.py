import asyncio
import html
import io
import json
import logging
import os
import re
import ssl
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict
from urllib.parse import quote
import aiohttp
import aiosqlite
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from aiogram import BaseMiddleware, Bot, Dispatcher, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    TelegramObject,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- ВСЕ КНОПКИ СИНЕГО ЦВЕТА (Telegram Bot API style="primary") ---
_OrigInlineKeyboardButton = InlineKeyboardButton
_HAS_BTN_STYLE = "style" in getattr(_OrigInlineKeyboardButton, "model_fields", {})


def InlineKeyboardButton(*args, **kwargs):
    if _HAS_BTN_STYLE:
        kwargs.setdefault("style", "primary")
    return _OrigInlineKeyboardButton(*args, **kwargs)

# --- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (.env) ---
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# --- ОСНОВНЫЕ НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = 8560054316  # Ваш Telegram ID
ADMIN_USERNAME = "wraxeZ"    # Заполнится автоматически при запуске (или впишите свой ник без @)
BOT_USERNAME = ""      # Заполнится автоматически при запуске

# --- НАСТРОЙКИ ОБЯЗАТЕЛЬНОЙ ПОДПИСКИ НА КАНАЛ ---
# Впишите @username вашего канала (или его ID вида -1001234567890) и ссылку на него.
# ВАЖНО: добавьте бота в администраторы этого канала, чтобы проверка работала!
CHECK_SUB_ENABLED = True
REQUIRED_CHANNEL_ID = "@FANPULSE_Channel"
REQUIRED_CHANNEL_URL = "https://t.me/FANPULSE_Channel"

# Курс 1 Звезды Telegram в рублях (для отображения ориентировочной суммы в ⭐️)
RUB_PER_STAR = 1.0

# Основной прокси (если он выдаст Bad Gateway, бот автоматически переключится на прямое соединение)
PROXY_URL = os.getenv("PROXY_URL", "socks5://127.0.0.1:10808")

# --- НАСТРОЙКИ CRYPTOBOT (CRYPTO PAY API) ---
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTO_ASSET = "USDT"
USD_TO_RUB = 82.0

# Процент реферального вознаграждения на баланс от покупок приглашённых
REFERRAL_PERCENT = 20

# Тарифы PRO-подписки (код: (Название, Дней, Цена в рублях))
SUB_PLANS = {
    "7d": ("PRO на 7 дней", 7, 99.0),
    "30d": ("PRO на 30 дней", 30, 299.0),
    "90d": ("PRO на 90 дней", 90, 790.0),
}

# Каталог отслеживаемых разделов на FunPay
TARGET_URLS = {
    "ChatGPT": "https://funpay.com/lots/1355/",
    "Midjourney": "https://funpay.com/lots/1531/",
    "Claude": "https://funpay.com/lots/3172/",
    "Gemini": "https://funpay.com/lots/3401/",
    "Suno": "https://funpay.com/lots/2582/",
    "ElevenLabs": "https://funpay.com/lots/3733/",
}

BASE_MIN_PRICE = 10.0
BASE_MAX_PRICE = 100000.0
DEFAULT_EXCLUDES = ["аренда", "гайд", "мануал", "инструкция", "схема", "способ", "пустой", "промокод", "купон", "курс"]

# --- ПРАВИЛА СЕГМЕНТАЦИИ ТОВАРОВ ---
SEGMENTS_CONFIG = [
    ("🔑 API / Ключи", ["api", "апи", "ключ", "key", "токен", "token", "баланс", "кредит", "credit"]),
    ("👥 Общие / Инвайты", ["общ", "shared", "шеринг", "слот", "место", "инвайт", "invite", "team", "семья"]),
    ("🧑‍💻 Готовые аккаунты", ["аккаунт", "акк", "account", "почт", "mail", "авторег", "саморег", "логин"]),
    ("💳 Подписка на ваш акк", ["на ваш", "оформл", "продлен", "оплат", "пополн", "подписк", "plus", "pro", "ultra"]),
]
DEFAULT_SEGMENT = "📦 Прочее"

DB_NAME = "funpay_ai_monitor.db"
LAST_SCAN_TIME = "ожидание первого цикла..."

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout
)
logger = logging.getLogger("FunPayBot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# --- СОСТОЯНИЯ ВВОДА (FSM) ---
class DashboardInput(StatesGroup):
    waiting_min_price = State()
    waiting_max_price = State()
    waiting_min_reviews = State()
    waiting_new_preset = State()
    waiting_search_query = State()
    waiting_stop_word = State()
    waiting_seller_nick = State()
    waiting_support_msg = State()
    waiting_topup_amount = State()
    adm_reply_user = State()
    adm_topup_data = State()
    adm_grant_pro_data = State()
    adm_broadcast_msg = State()


# --- ПРОВЕРКА ОБЯЗАТЕЛЬНОЙ ПОДПИСКИ НА КАНАЛ ---
def get_valid_channel_url() -> str:
    raw = str(REQUIRED_CHANNEL_URL or "").strip()
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    if raw.startswith("t.me/"):
        return f"https://{raw}"
    clean_id = str(REQUIRED_CHANNEL_ID).lstrip("@").strip()
    return f"https://t.me/{clean_id}"


async def is_user_subscribed(user_id: int) -> bool:
    if not CHECK_SUB_ENABLED or user_id == ADMIN_ID:
        return True
    if not REQUIRED_CHANNEL_ID or "ваш_канал" in str(REQUIRED_CHANNEL_ID):
        return True

    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except TelegramBadRequest as e:
        logger.warning(
            f"⚠️ Не удалось проверить подписку в {REQUIRED_CHANNEL_ID} (добавьте бота в админы канала!): {e}"
        )
        return True
    except Exception as e:
        logger.warning(f"⚠️ Ошибка API при проверке подписки: {e}")
        return True


def render_sub_gate():
    text = (
        f"🔒 <b>ДОСТУП ОГРАНИЧЕН</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"Для использования <b>FunPay AI Terminal</b> необходимо подписаться на наш официальный Telegram-канал.\n\n"
        f"<blockquote>"
        f"1. Нажмите кнопку <b>«📢 Перейти в канал»</b> и подпишитесь.\n"
        f"2. Вернитесь сюда и нажмите <b>«✅ Проверить подписку»</b>."
        f"</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Перейти в канал", url=get_valid_channel_url())],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")],
    ])
    return text, kb


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if not user or user.is_bot:
            return await handler(event, data)

        if isinstance(event, types.CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        if not await is_user_subscribed(user.id):
            if isinstance(event, types.Message) and event.text and event.text.startswith("/start "):
                args = event.text.split(maxsplit=1)[1].strip()
                if args.startswith("ref_"):
                    try:
                        ref_id = int(args.replace("ref_", "").strip())
                        await get_user_profile(user, inc_req=False, referrer_id=ref_id)
                    except ValueError:
                        pass

            text, kb = render_sub_gate()
            if isinstance(event, types.Message):
                try:
                    await event.delete()
                except Exception:
                    pass
                await event.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            elif isinstance(event, types.CallbackQuery):
                await switch_screen(event.message, text, kb)
                await event.answer("Сначала подпишитесь на канал!", show_alert=True)
            return

        return await handler(event, data)


dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def detect_segment(title: str, sub_params: str = "") -> str:
    combined = f"{title} {sub_params}".lower()
    for segment_name, keywords in SEGMENTS_CONFIG:
        if any(kw in combined for kw in keywords):
            return segment_name
    return DEFAULT_SEGMENT


def chunk_buttons(buttons: list, row_width: int = 3) -> list:
    return [buttons[i:i + row_width] for i in range(0, len(buttons), row_width)]


def build_admin_chat_url(prefill_text: str = "") -> str:
    clean_uname = ADMIN_USERNAME.lstrip("@").strip()
    if clean_uname:
        if prefill_text:
            return f"https://t.me/{clean_uname}?text={quote(prefill_text)}"
        return f"https://t.me/{clean_uname}"
    return f"tg://user?id={ADMIN_ID}"


async def switch_screen(message: types.Message, text: str, reply_markup: InlineKeyboardMarkup = None) -> int:
    chat_id = message.chat.id
    try:
        await message.delete()
    except Exception:
        pass
    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    return sent.message_id


# --- АВТОПОДБОР РАБОЧЕГО ПОДКЛЮЧЕНИЯ К TELEGRAM ---
async def connect_telegram_with_fallback() -> types.User:
    candidates = [None]
    if PROXY_URL:
        candidates.append(PROXY_URL)
        if "127.0.0.1:10808" in PROXY_URL:
            candidates.append("http://127.0.0.1:10808")
            candidates.append("http://127.0.0.1:10809")

    while True:
        for proxy_candidate in candidates:
            mode_label = f"прокси {proxy_candidate}" if proxy_candidate else "прямое соединение (без прокси)"
            try:
                if bot.session:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass

                bot.session = AiohttpSession(proxy=proxy_candidate, timeout=10) if proxy_candidate else AiohttpSession(timeout=10)
                logger.info(f"🔌 Пробую подключиться к Telegram через {mode_label}...")
                me = await bot.get_me()
                logger.info(f"✅ Успешное подключение к Telegram через {mode_label}!")
                return me
            except Exception as e:
                logger.warning(f"⚠️ Не удалось через {mode_label}: {e}")

        logger.error("⏳ Все варианты подключения к Telegram недоступны (Bad Gateway / Timeout). Проверьте VPN-клиент. Повтор через 5 сек...")
        await asyncio.sleep(5)


# --- БАЗА ДАННЫХ И АВТОМИГРАЦИЯ ---
async def ensure_column(db: aiosqlite.Connection, table: str, column: str, col_def: str):
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in await cursor.fetchall()]
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        logger.info(f"🛠️ Автомиграция БД: добавлена колонка '{column}' в '{table}'.")


async def init_db():
    logger.info("🗄️ Проверяю и обновляю структуру SQLite...")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lots_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                segment TEXT,
                lot_id TEXT,
                title TEXT,
                price REAL,
                seller TEXT,
                is_online INTEGER,
                auto_delivery INTEGER DEFAULT 0,
                is_promo INTEGER DEFAULT 0,
                reviews_count INTEGER DEFAULT 0,
                rating_stars INTEGER DEFAULT 0,
                seller_age TEXT DEFAULT '',
                seller_url TEXT DEFAULT '',
                stock_amount TEXT DEFAULT '',
                sub_params TEXT DEFAULT '',
                scraped_at DATETIME
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS last_snapshot (
                lot_id TEXT PRIMARY KEY,
                category TEXT,
                segment TEXT,
                title TEXT,
                price REAL,
                seller TEXT,
                is_online INTEGER,
                auto_delivery INTEGER DEFAULT 0,
                is_promo INTEGER DEFAULT 0,
                reviews_count INTEGER DEFAULT 0,
                rating_stars INTEGER DEFAULT 0,
                seller_age TEXT DEFAULT '',
                seller_url TEXT DEFAULT '',
                stock_amount TEXT DEFAULT '',
                sub_params TEXT DEFAULT '',
                url TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS category_stats (
                category TEXT PRIMARY KEY,
                total_lots INTEGER DEFAULT 0,
                online_lots INTEGER DEFAULT 0,
                total_sellers INTEGER DEFAULT 0,
                online_sellers INTEGER DEFAULT 0,
                auto_lots INTEGER DEFAULT 0,
                promo_lots INTEGER DEFAULT 0,
                avg_price REAL DEFAULT 0.0,
                updated_at DATETIME
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sold_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                segment TEXT,
                title TEXT,
                price REAL,
                seller TEXT,
                auto_delivery INTEGER DEFAULT 0,
                sold_at DATETIME
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tg_users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at DATETIME,
                requests_count INTEGER DEFAULT 0,
                saved_tags TEXT DEFAULT '',
                only_online INTEGER DEFAULT 0,
                only_auto INTEGER DEFAULT 0,
                min_reviews INTEGER DEFAULT 0,
                min_price REAL DEFAULT 100.0,
                max_price REAL DEFAULT 15000.0,
                enabled_cats TEXT DEFAULT 'ALL',
                custom_excludes TEXT DEFAULT '',
                tracked_sellers TEXT DEFAULT '',
                balance REAL DEFAULT 0.0,
                pro_until DATETIME DEFAULT NULL,
                referrer_id INTEGER DEFAULT NULL,
                ref_earned REAL DEFAULT 0.0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cryptobot_invoices (
                invoice_id TEXT PRIMARY KEY,
                tg_id INTEGER,
                plan_code TEXT,
                amount_crypto REAL,
                asset TEXT,
                status TEXT DEFAULT 'active',
                created_at DATETIME
            )
        """)

        for tbl in ("lots_history", "last_snapshot"):
            await ensure_column(db, tbl, "segment", f"TEXT DEFAULT '{DEFAULT_SEGMENT}'")
            await ensure_column(db, tbl, "auto_delivery", "INTEGER DEFAULT 0")
            await ensure_column(db, tbl, "is_promo", "INTEGER DEFAULT 0")
            await ensure_column(db, tbl, "reviews_count", "INTEGER DEFAULT 0")
            await ensure_column(db, tbl, "rating_stars", "INTEGER DEFAULT 0")
            await ensure_column(db, tbl, "seller_age", "TEXT DEFAULT ''")
            await ensure_column(db, tbl, "seller_url", "TEXT DEFAULT ''")
            await ensure_column(db, tbl, "stock_amount", "TEXT DEFAULT ''")
            await ensure_column(db, tbl, "sub_params", "TEXT DEFAULT ''")

        await ensure_column(db, "sold_events", "segment", f"TEXT DEFAULT '{DEFAULT_SEGMENT}'")
        await ensure_column(db, "sold_events", "auto_delivery", "INTEGER DEFAULT 0")

        await ensure_column(db, "tg_users", "only_online", "INTEGER DEFAULT 0")
        await ensure_column(db, "tg_users", "only_auto", "INTEGER DEFAULT 0")
        await ensure_column(db, "tg_users", "min_reviews", "INTEGER DEFAULT 0")
        await ensure_column(db, "tg_users", "min_price", "REAL DEFAULT 100.0")
        await ensure_column(db, "tg_users", "max_price", "REAL DEFAULT 15000.0")
        await ensure_column(db, "tg_users", "enabled_cats", "TEXT DEFAULT 'ALL'")
        await ensure_column(db, "tg_users", "custom_excludes", "TEXT DEFAULT ''")
        await ensure_column(db, "tg_users", "tracked_sellers", "TEXT DEFAULT ''")
        await ensure_column(db, "tg_users", "balance", "REAL DEFAULT 0.0")
        await ensure_column(db, "tg_users", "pro_until", "DATETIME DEFAULT NULL")
        await ensure_column(db, "tg_users", "referrer_id", "INTEGER DEFAULT NULL")
        await ensure_column(db, "tg_users", "ref_earned", "REAL DEFAULT 0.0")

        await db.commit()
    logger.info("✅ База данных готова.")


# --- ГЛУБОКИЙ ПАРСЕР ВИТРИНЫ И ПРОДАВЦОВ ---
def check_item_online(item) -> int:
    if str(item.get("data-online", "")).strip() == "1":
        return 1
    if item.find(attrs={"data-online": "1"}) is not None:
        return 1

    if "online" in item.get("class", []):
        return 1
    if item.select_one(".online") is not None:
        return 1

    return 0


async def fetch_html_with_fallback(url: str) -> str | None:
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cookie": "cy=rub",
        "Referer": "https://funpay.com/",
    }

    def _direct_urllib_no_proxy():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx)
        )
        req = urllib.request.Request(url, headers=req_headers)
        with opener.open(req, timeout=14) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="ignore")
        return None

    try:
        html_direct = await asyncio.to_thread(_direct_urllib_no_proxy)
        if html_direct and "tc-item" in html_direct:
            return html_direct
    except Exception as e:
        logger.debug(f"Прямой запрос без прокси не прошёл: {e}")

    try:
        async with AsyncSession(proxies={"http": "", "https": ""}, trust_env=False, verify=False) as s:
            resp = await s.get(url, headers=req_headers, impersonate="chrome120", timeout=14)
            if resp.status_code == 200 and resp.text:
                return resp.text
    except Exception as e:
        logger.debug(f"curl_cffi без прокси не прошёл: {e}")

    if PROXY_URL:
        try:
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(PROXY_URL, rdns=True)
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=req_headers) as client:
                async with client.get(url) as resp:
                    if resp.status == 200:
                        return await resp.text()
        except Exception as e:
            logger.debug(f"aiohttp через {PROXY_URL} не прошёл: {e}")

    def _system_proxy_urllib():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=14, context=ctx) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="ignore")
        return None

    try:
        html_sys = await asyncio.to_thread(_system_proxy_urllib)
        if html_sys:
            return html_sys
    except Exception as e:
        logger.warning(f"⚠️ Все 4 способа подключения к {url} отклонены: {e}")

    return None


async def fetch_category(category: str, url: str):
    logger.info(f"🌐 [{category}] Сканирование {url} ...")
    html_text = await fetch_html_with_fallback(url)
    if not html_text:
        logger.error(f"💥 [{category}] Не удалось получить страницу. Проверьте, открывается ли funpay.com в браузере.")
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    items = soup.select("a.tc-item") or soup.select(".tc-item")
    if not items:
        page_title = soup.title.text.strip() if soup.title else "без заголовка"
        logger.warning(f"⚠️ [{category}] Лоты .tc-item не найдены (заголовок: «{page_title}»)")
        return []

    lots_dict = {}
    for item in items:
        href = item.get("href", "")
        if not href:
            link_el = item.select_one("a[href*='lots/offer']")
            href = link_el.get("href", "") if link_el else ""

        lot_id = href.split("id=")[-1] if "id=" in href else ""
        title_el = item.select_one(".tc-desc-text")
        price_block = item.select_one(".tc-price")
        price_el = item.select_one(".tc-price div") or price_block
        seller_el = item.select_one(".media-user-name")

        if not (lot_id and title_el and price_el):
            continue

        title_clean = title_el.text.strip()

        price_clean = None
        if price_block and price_block.get("data-s"):
            try:
                price_clean = float(price_block.get("data-s"))
            except ValueError:
                price_clean = None

        if price_clean is None:
            raw_price_txt = price_el.text.replace("\xa0", "").replace(" ", "").replace(",", ".")
            m_price = re.search(r"\d+(?:\.\d+)?", raw_price_txt)
            if not m_price:
                continue
            try:
                price_clean = float(m_price.group(0))
            except ValueError:
                continue

        if price_clean < BASE_MIN_PRICE or price_clean > BASE_MAX_PRICE:
            continue

        classes = item.get("class", [])
        is_online = check_item_online(item)
        is_promo = 1 if ("offer-promo" in classes or "promo" in classes) else 0
        auto_el = item.select_one(".tc-desc-icon") or item.select_one(".auto-dlv")
        auto_delivery = 1 if (auto_el is not None or str(item.get("data-auto", "")) == "1") else 0

        param_parts = []
        for sel in (".tc-server", ".tc-side", ".tc-type"):
            el = item.select_one(sel)
            if el and el.text.strip():
                param_parts.append(el.text.strip())
        sub_params = " | ".join(param_parts)

        amount_el = item.select_one(".tc-amount")
        stock_amount = amount_el.text.strip() if amount_el else ""

        reviews_count = 0
        rev_el = item.select_one(".media-user-reviews")
        if rev_el:
            digits = re.sub(r"\D", "", rev_el.text)
            if digits:
                reviews_count = int(digits)

        rating_stars = 0
        rating_el = item.select_one(".rating-stars")
        if rating_el:
            for cls in rating_el.get("class", []):
                if cls.startswith("rating-"):
                    star_d = re.sub(r"\D", "", cls)
                    if star_d:
                        rating_stars = int(star_d)

        age_el = item.select_one(".media-user-info")
        seller_age = age_el.text.strip() if age_el else ""

        seller_url = ""
        avatar_el = item.select_one(".avatar-photo")
        if avatar_el and avatar_el.get("data-href"):
            seller_url = avatar_el.get("data-href")

        lots_dict[lot_id] = {
            "lot_id": lot_id,
            "category": category,
            "segment": detect_segment(title_clean, f"{sub_params} {stock_amount}"),
            "title": title_clean,
            "price": price_clean,
            "seller": seller_el.text.strip() if seller_el else "Unknown",
            "is_online": is_online,
            "auto_delivery": auto_delivery,
            "is_promo": is_promo,
            "reviews_count": reviews_count,
            "rating_stars": rating_stars,
            "seller_age": seller_age,
            "seller_url": seller_url,
            "stock_amount": stock_amount,
            "sub_params": sub_params,
            "url": href
        }

    lots_list = list(lots_dict.values())
    unique_sellers = {x["seller"].lower() for x in lots_list}
    online_sellers = {x["seller"].lower() for x in lots_list if x["is_online"] == 1}
    logger.info(
        f"✅ [{category}] Лотов: {len(lots_list)} | Продавцов: {len(unique_sellers)} (🟢 онлайн: {len(online_sellers)} чел.)"
    )
    return lots_list


async def fetch_lot_details(lot_url: str) -> dict:
    html_text = await fetch_html_with_fallback(lot_url)
    if not html_text:
        return {"desc": "Не удалось загрузить страницу лота", "params": ""}

    soup = BeautifulSoup(html_text, "html.parser")
    param_items = []
    for block in soup.select(".param-item"):
        h = block.select_one("h5")
        v = block.select_one("div")
        if h and v:
            param_items.append(f"{h.text.strip()}: {v.text.strip()}")

    desc_el = soup.select_one(".param-item .param-desc") or soup.select_one("div[data-id='desc']")
    if not desc_el:
        for block in soup.select(".param-item"):
            h = block.select_one("h5")
            if h and "описан" in h.text.lower():
                desc_el = block.select_one("div")
                break

    desc_text = desc_el.get_text("\n", strip=True) if desc_el else "Описание не указано"
    return {
        "params": " | ".join(param_items[:5]),
        "desc": desc_text[:750]
    }


async def monitor_job():
    global LAST_SCAN_TIME
    logger.info("🔄 --- Плановый обход каталога FunPay ---")
    now = datetime.now()
    async with aiosqlite.connect(DB_NAME) as db:
        for category, url in TARGET_URLS.items():
            lots = await fetch_category(category, url)
            if not lots:
                await asyncio.sleep(3)
                continue

            cursor = await db.execute(
                "SELECT lot_id, segment, title, price, seller, is_online, auto_delivery FROM last_snapshot WHERE category = ?",
                (category,)
            )
            prev_rows = await cursor.fetchall()
            if prev_rows:
                curr_ids = {x["lot_id"] for x in lots}
                sold_records = []
                for old_id, old_seg, old_title, old_price, old_seller, old_online, old_auto in prev_rows:
                    if old_id not in curr_ids and old_online == 1:
                        sold_records.append((category, old_seg or DEFAULT_SEGMENT, old_title, old_price, old_seller, old_auto or 0, now))
                if sold_records:
                    await db.executemany("""
                        INSERT INTO sold_events (category, segment, title, price, seller, auto_delivery, sold_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, sold_records)

            history_records = [
                (
                    category, l["segment"], l["lot_id"], l["title"], l["price"],
                    l["seller"], l["is_online"], l["auto_delivery"], l["is_promo"],
                    l["reviews_count"], l["rating_stars"], l["seller_age"],
                    l["seller_url"], l["stock_amount"], l["sub_params"], now
                )
                for l in lots
            ]
            await db.executemany("""
                INSERT INTO lots_history (
                    category, segment, lot_id, title, price, seller,
                    is_online, auto_delivery, is_promo, reviews_count, rating_stars,
                    seller_age, seller_url, stock_amount, sub_params, scraped_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, history_records)

            await db.execute("DELETE FROM last_snapshot WHERE category = ?", (category,))
            snapshot_records = [
                (
                    l["lot_id"], category, l["segment"], l["title"], l["price"],
                    l["seller"], l["is_online"], l["auto_delivery"], l["is_promo"],
                    l["reviews_count"], l["rating_stars"], l["seller_age"],
                    l["seller_url"], l["stock_amount"], l["sub_params"], l["url"]
                )
                for l in lots
            ]
            await db.executemany("""
                INSERT OR REPLACE INTO last_snapshot (
                    lot_id, category, segment, title, price, seller,
                    is_online, auto_delivery, is_promo, reviews_count, rating_stars,
                    seller_age, seller_url, stock_amount, sub_params, url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, snapshot_records)

            all_sellers = {x["seller"].lower() for x in lots}
            online_sellers = {x["seller"].lower() for x in lots if x["is_online"] == 1}
            online_lots = sum(x["is_online"] for x in lots)
            auto_lots = sum(x["auto_delivery"] for x in lots)
            promo_lots = sum(x["is_promo"] for x in lots)
            avg_price = sum(x["price"] for x in lots) / len(lots)

            await db.execute("""
                INSERT OR REPLACE INTO category_stats (
                    category, total_lots, online_lots, total_sellers, online_sellers,
                    auto_lots, promo_lots, avg_price, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                category, len(lots), online_lots, len(all_sellers), len(online_sellers),
                auto_lots, promo_lots, round(avg_price, 1), now
            ))

            await db.commit()
            await asyncio.sleep(4)

    LAST_SCAN_TIME = datetime.now().strftime("%H:%M:%S")
    logger.info(f"💤 Сбор завершён в {LAST_SCAN_TIME}.\n")


# --- АКТИВАЦИЯ ПОДПИСКИ PRO И РЕФЕРАЛЬНОЕ НАЧИСЛЕНИЕ ---
async def activate_pro_for_user(tg_id: int, plan_code: str) -> tuple[str, datetime]:
    title, days, price_rub = SUB_PLANS[plan_code]
    now = datetime.now()
    referrer_id = None

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT pro_until, referrer_id FROM tg_users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        base_dt = now
        if row:
            referrer_id = row[1]
            if row[0]:
                try:
                    old_dt = datetime.fromisoformat(str(row[0]))
                    if old_dt > now:
                        base_dt = old_dt
                except Exception:
                    pass

        new_pro_until = base_dt + timedelta(days=days)
        await db.execute("UPDATE tg_users SET pro_until = ? WHERE tg_id = ?", (new_pro_until, tg_id))

        bonus_rub = round(price_rub * REFERRAL_PERCENT / 100, 2)
        if referrer_id and referrer_id != tg_id and bonus_rub > 0:
            await db.execute(
                "UPDATE tg_users SET balance = balance + ?, ref_earned = ref_earned + ? WHERE tg_id = ?",
                (bonus_rub, bonus_rub, referrer_id)
            )
        await db.commit()

    if referrer_id and referrer_id != tg_id and bonus_rub > 0:
        try:
            await bot.send_message(
                chat_id=referrer_id,
                text=(
                    f"🤝 <b>Реферальное начисление!</b>\n"
                    f"<code>──────────────────────────</code>\n"
                    f"Ваш реферал оформил подписку <b>{html.escape(title)}</b>.\n"
                    f"На ваш баланс зачислено: <code>+{bonus_rub:.0f} ₽</code> ({REFERRAL_PERCENT}%)."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

    return title, new_pro_until


# --- CRYPTOBOT API: СОЗДАНИЕ СЧЁТА И АВТОВЫДАЧА ПОДПИСКИ / ПОПОЛНЕНИЯ БАЛАНСА ---
async def create_cryptobot_invoice(tg_id: int, plan_code: str) -> tuple[str | None, float]:
    if not CRYPTOBOT_TOKEN or "ВАШ_API_ТОКЕН" in CRYPTOBOT_TOKEN:
        return None, 0.0

    if plan_code.startswith("topup:"):
        try:
            price_rub = float(plan_code.split(":", 1)[1])
        except ValueError:
            return None, 0.0
        if price_rub < 10:
            return None, 0.0
        title = f"Пополнение баланса на {price_rub:.0f} ₽"
    elif plan_code in SUB_PLANS:
        title, days, price_rub = SUB_PLANS[plan_code]
    else:
        return None, 0.0

    amount_crypto = round(max(0.05, price_rub / USD_TO_RUB), 2)

    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "asset": CRYPTO_ASSET,
        "amount": str(amount_crypto),
        "description": f"{title} · FunPay AI Terminal",
        "payload": f"{tg_id}:{plan_code}",
        "expires_in": 1800
    }

    async with AsyncSession() as http_session:
        try:
            resp = await http_session.post(
                "https://pay.crypt.bot/api/createInvoice",
                json=payload,
                headers=headers,
                timeout=12
            )
            data = resp.json()
            if data.get("ok"):
                inv = data["result"]
                inv_id = str(inv["invoice_id"])
                pay_url = inv.get("bot_invoice_url") or inv.get("pay_url")

                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("""
                        INSERT OR REPLACE INTO cryptobot_invoices
                        (invoice_id, tg_id, plan_code, amount_crypto, asset, status, created_at)
                        VALUES (?, ?, ?, ?, ?, 'active', ?)
                    """, (inv_id, tg_id, plan_code, amount_crypto, CRYPTO_ASSET, datetime.now()))
                    await db.commit()

                return pay_url, amount_crypto
            else:
                logger.error(f"CryptoBot ошибка создания счёта: {data}")
        except Exception as e:
            logger.error(f"CryptoBot сбой запроса: {e}")

    return None, 0.0


async def check_cryptobot_payments_job():
    if not CRYPTOBOT_TOKEN or "ВАШ_API_ТОКЕН" in CRYPTOBOT_TOKEN:
        return

    cutoff = datetime.now() - timedelta(minutes=35)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT invoice_id, tg_id, plan_code, amount_crypto, asset
            FROM cryptobot_invoices
            WHERE status = 'active' AND created_at >= ?
        """, (cutoff,))
        pending = await cursor.fetchall()

    if not pending:
        return

    pending_map = {str(r[0]): r for r in pending}
    ids_str = ",".join(pending_map.keys())
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}

    async with AsyncSession() as http_session:
        try:
            resp = await http_session.get(
                f"https://pay.crypt.bot/api/getInvoices?invoice_ids={ids_str}",
                headers=headers,
                timeout=12
            )
            data = resp.json()
            if not data.get("ok"):
                return

            items = data.get("result", {}).get("items", [])
            for inv in items:
                inv_id = str(inv.get("invoice_id"))
                status = inv.get("status")
                if inv_id not in pending_map:
                    continue

                _, tg_id, plan_code, amount_crypto, asset = pending_map[inv_id]

                if status == "paid":
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute(
                            "UPDATE cryptobot_invoices SET status = 'paid' WHERE invoice_id = ?",
                            (inv_id,)
                        )
                        await db.commit()

                    if str(plan_code).startswith("topup:"):
                        topup_rub = float(str(plan_code).split(":", 1)[1])
                        async with aiosqlite.connect(DB_NAME) as db:
                            await db.execute(
                                "UPDATE tg_users SET balance = balance + ? WHERE tg_id = ?",
                                (topup_rub, tg_id)
                            )
                            await db.commit()

                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💎 Перейти к тарифам PRO", callback_data="m:sub")],
                            [InlineKeyboardButton(text="⚡ Открыть терминал", callback_data="m:main")],
                        ])
                        try:
                            await bot.send_message(
                                chat_id=tg_id,
                                text=(
                                    f"✅ <b>Баланс успешно пополнен через CryptoBot!</b>\n"
                                    f"<code>──────────────────────────</code>\n"
                                    f"<blockquote>"
                                    f"▪ Зачислено на баланс: <b>+{topup_rub:.0f} ₽</b>\n"
                                    f"▪ Оплачено: <code>{amount_crypto} {asset}</code>"
                                    f"</blockquote>"
                                ),
                                reply_markup=kb,
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                        try:
                            await bot.send_message(
                                chat_id=ADMIN_ID,
                                text=(
                                    f"🦋 <b>Новое пополнение баланса через CryptoBot!</b>\n"
                                    f"▪ Пользователь: <code>{tg_id}</code>\n"
                                    f"▪ Сумма: <b>+{topup_rub:.0f} ₽</b> ({amount_crypto} {asset})"
                                ),
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                        continue

                    title, new_pro_until = await activate_pro_for_user(tg_id, plan_code)

                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Открыть терминал PRO", callback_data="m:main")]
                    ])
                    try:
                        await bot.send_message(
                            chat_id=tg_id,
                            text=(
                                f"✅ <b>Оплата через CryptoBot подтверждена!</b>\n"
                                f"<code>──────────────────────────</code>\n"
                                f"<blockquote>"
                                f"▪ Тариф: <b>{html.escape(title)}</b>\n"
                                f"▪ Оплачено: <code>{amount_crypto} {asset}</code>\n"
                                f"▪ PRO активен до: <b>{new_pro_until.strftime('%d.%m.%Y %H:%M')}</b>"
                                f"</blockquote>\n\n"
                                f"Все функции PRO (ценовые стратегии, лучшее время, слежка и графики) разблокированы."
                            ),
                            reply_markup=kb,
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                    try:
                        await bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                f"🦋 <b>Новая оплата через CryptoBot!</b>\n"
                                f"▪ Покупатель: <code>{tg_id}</code>\n"
                                f"▪ Тариф: <b>{html.escape(title)}</b> ({amount_crypto} {asset})"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                elif status == "expired":
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute(
                            "UPDATE cryptobot_invoices SET status = 'expired' WHERE invoice_id = ?",
                            (inv_id,)
                        )
                        await db.commit()
        except Exception as e:
            logger.warning(f"CryptoBot ошибка фоновой проверки: {e}")


# --- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ, БАЛАНС И ПРОВЕРКА ПОДПИСКИ ---
async def get_user_profile(user: types.User, inc_req: bool = False, referrer_id: int = None) -> dict:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT joined_at, requests_count, saved_tags, only_online, only_auto, min_reviews,
                   min_price, max_price, enabled_cats, custom_excludes, tracked_sellers,
                   balance, pro_until, referrer_id, ref_earned
            FROM tg_users WHERE tg_id = ?
        """, (user.id,))
        row = await cursor.fetchone()

        if not row:
            now = datetime.now()
            default_ex_str = ",".join(DEFAULT_EXCLUDES)
            valid_ref = referrer_id if (referrer_id and referrer_id != user.id) else None
            await db.execute("""
                INSERT INTO tg_users (
                    tg_id, username, full_name, joined_at, requests_count, saved_tags,
                    only_online, only_auto, min_reviews, min_price, max_price, enabled_cats,
                    custom_excludes, tracked_sellers, balance, pro_until, referrer_id, ref_earned
                )
                VALUES (?, ?, ?, ?, ?, '', 0, 0, 0, 100.0, 15000.0, 'ALL', ?, '', 0.0, NULL, ?, 0.0)
            """, (user.id, user.username or "", user.full_name, now, 1 if inc_req else 0, default_ex_str, valid_ref))
            await db.commit()

            if valid_ref:
                try:
                    await bot.send_message(
                        chat_id=valid_ref,
                        text=f"👤 По вашей реферальной ссылке зарегистрировался новый пользователь: <b>{html.escape(user.full_name)}</b>!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            return {
                "joined_at": now,
                "requests_count": 1 if inc_req else 0,
                "saved_tags": [],
                "only_online": False,
                "only_auto": False,
                "min_reviews": 0,
                "min_price": 100.0,
                "max_price": 15000.0,
                "enabled_cats": list(TARGET_URLS.keys()),
                "excludes": list(DEFAULT_EXCLUDES),
                "tracked_sellers": [],
                "balance": 0.0,
                "pro_until": None,
                "is_pro": False,
                "referrer_id": valid_ref,
                "ref_earned": 0.0,
            }

        if inc_req:
            await db.execute("""
                UPDATE tg_users SET username = ?, full_name = ?, requests_count = requests_count + 1
                WHERE tg_id = ?
            """, (user.username or "", user.full_name, user.id))
            await db.commit()

    (joined_raw, req_cnt, tags_str, only_on, only_auto, min_rev,
     min_p, max_p, cats_str, ex_str, sellers_str, balance_val, pro_until_raw, ref_id_val, ref_earned_val) = row

    all_cats = list(TARGET_URLS.keys())
    if not cats_str or cats_str == "ALL":
        active_cats = all_cats
    else:
        active_cats = [c for c in cats_str.split(",") if c in all_cats]

    tags_list = [t.strip() for t in (tags_str or "").split(",") if t.strip()]
    ex_list = [e.strip() for e in (ex_str or "").split(",") if e.strip()]
    sellers_list = [s.strip() for s in (sellers_str or "").split(",") if s.strip()]

    pro_until_dt = None
    is_pro = False
    if pro_until_raw:
        try:
            pro_until_dt = datetime.fromisoformat(str(pro_until_raw))
            if pro_until_dt > datetime.now():
                is_pro = True
        except Exception:
            pass

    return {
        "joined_at": datetime.fromisoformat(str(joined_raw)),
        "requests_count": req_cnt + (1 if inc_req else 0),
        "saved_tags": tags_list,
        "only_online": bool(only_on),
        "only_auto": bool(only_auto),
        "min_reviews": int(min_rev or 0),
        "min_price": float(min_p or 100.0),
        "max_price": float(max_p or 15000.0),
        "enabled_cats": active_cats,
        "excludes": ex_list,
        "tracked_sellers": sellers_list,
        "balance": float(balance_val or 0.0),
        "pro_until": pro_until_dt,
        "is_pro": is_pro,
        "referrer_id": ref_id_val,
        "ref_earned": float(ref_earned_val or 0.0),
    }


def pass_user_filters(title: str, price: float, is_online: int, auto_delivery: int, reviews_count: int, sub_params: str, cfg: dict) -> bool:
    if cfg["only_online"] and is_online != 1:
        return False
    if cfg["only_auto"] and auto_delivery != 1:
        return False
    if reviews_count < cfg["min_reviews"]:
        return False
    if price < cfg["min_price"] or price > cfg["max_price"]:
        return False
    combined_l = f"{title} {sub_params}".lower()
    if cfg["excludes"] and any(bad in combined_l for bad in cfg["excludes"]):
        return False
    return True


# --- ЭКРАН БЛОКИРОВКИ ДЛЯ БЕСПЛАТНЫХ АККАУНТОВ (PAYWALL) ---
def render_paywall_view(feature_name: str, cfg: dict):
    text = (
        f"🔒 <b>Раздел «{html.escape(feature_name)}»</b>\n"
        f"<i>Доступно по подписке PRO</i>\n\n"
        f"<blockquote>"
        f"▸ <b>Рекомендации по цене</b> — расчёт цены быстрого входа, рабочей цены и наценки за автовыдачу ⚡ по каждой услуге\n"
        f"▸ <b>Сигналы повышения/снижения цены</b> — умные подсказки по живой конкуренции, демперам и темпу продаж\n"
        f"▸ <b>Лучшее время публикации</b> — окна минимальной конкуренции и пика покупок для каждого раздела и услуги\n"
        f"▸ <b>Слежка за конкурентами</b> — до 8 продавцов, статус онлайна и чтение полных описаний\n"
        f"▸ <b>График 24ч</b> — визуальная кривая спроса и активности продавцов"
        f"</blockquote>\n\n"
        f"Баланс кошелька: <code>{cfg['balance']:.0f} ₽</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Перейти к тарифам PRO", callback_data="m:sub")],
        [InlineKeyboardButton(text="💳 Пополнение", callback_data="m:topup")],
        [InlineKeyboardButton(text="← Назад в меню", callback_data="m:main")],
    ])
    return text, kb


# --- PRO-АНАЛИТИЧЕСКИЙ ДВИЖОК: ВРЕМЯ ПУБЛИКАЦИИ И ДИНАМИЧЕСКОЕ ЦЕНООБРАЗОВАНИЕ ---
async def get_hourly_windows(db: aiosqlite.Connection, category: str = None, segment: str = None) -> dict:
    """
    Возвращает лучшие часы для публикации/поднятия лотов и часы пиковой конкуренции
    для конкретного (category, segment), с откатом на category, а затем на весь рынок.
    """
    scope_label = "по выбранной услуге"
    comp_rows = []
    sales_rows = []

    if category and segment:
        cursor = await db.execute("""
            SELECT strftime('%H', scraped_at) as hour, AVG(is_online) * 100 as online_pct
            FROM lots_history WHERE category = ? AND segment = ?
            GROUP BY hour ORDER BY online_pct ASC
        """, (category, segment))
        comp_rows = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT strftime('%H', sold_at) as hour, COUNT(*) as sales_cnt
            FROM sold_events WHERE category = ? AND segment = ?
            GROUP BY hour ORDER BY sales_cnt DESC
        """, (category, segment))
        sales_rows = await cursor.fetchall()

    if len(comp_rows) < 2 and category:
        scope_label = f"по разделу {category}"
        cursor = await db.execute("""
            SELECT strftime('%H', scraped_at) as hour, AVG(is_online) * 100 as online_pct
            FROM lots_history WHERE category = ?
            GROUP BY hour ORDER BY online_pct ASC
        """, (category,))
        comp_rows = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT strftime('%H', sold_at) as hour, COUNT(*) as sales_cnt
            FROM sold_events WHERE category = ?
            GROUP BY hour ORDER BY sales_cnt DESC
        """, (category,))
        sales_rows = await cursor.fetchall()

    if not comp_rows:
        scope_label = "по всему каталогу"
        cursor = await db.execute("""
            SELECT strftime('%H', scraped_at) as hour, AVG(is_online) * 100 as online_pct
            FROM lots_history GROUP BY hour ORDER BY online_pct ASC
        """)
        comp_rows = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT strftime('%H', sold_at) as hour, COUNT(*) as sales_cnt
            FROM sold_events GROUP BY hour ORDER BY sales_cnt DESC
        """)
        sales_rows = await cursor.fetchall()

    if not comp_rows:
        return {
            "ready": False,
            "scope": scope_label,
            "best_publish": [],
            "peak_demand": [],
            "peak_comp": [],
            "current_hour_warn": False,
        }

    sales_dict = {h: cnt for h, cnt in sales_rows}
    scored = []
    for h, online_pct in comp_rows:
        s_cnt = sales_dict.get(h, 0)
        # Чем больше продаж и ниже доля конкурентов в сети — тем выше индекс привлекательности часа
        score = (s_cnt * 12.0) - online_pct
        scored.append((score, h, online_pct, s_cnt))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_publish = [(h, on_pct, s_cnt) for _, h, on_pct, s_cnt in scored[:3]]
    peak_comp = sorted(comp_rows, key=lambda x: x[1], reverse=True)[:3]
    peak_demand = sales_rows[:3]

    curr_h = datetime.now().strftime("%H")
    peak_comp_hours = {h for h, _ in peak_comp}
    current_hour_warn = curr_h in peak_comp_hours and len(peak_comp) >= 2

    return {
        "ready": True,
        "scope": scope_label,
        "best_publish": best_publish,
        "peak_demand": peak_demand,
        "peak_comp": peak_comp,
        "current_hour_warn": current_hour_warn,
    }


def build_pro_pricing_advisor(
    matched: list,
    sales_24h: int,
    auto_sales_24h: int,
    min_user_price: float,
    current_hour_warn: bool,
) -> dict:
    """
    Формирует PRO-рекомендации по ценам (вход / рабочая / автовыдача)
    и персональные подсказки, когда повысить, а когда снизить цену.
    matched: [(title, price, seller, is_online, auto_delivery, reviews_count, sub_params, url), ...]
    """
    all_prices = sorted(x[1] for x in matched)
    total_sellers = len({x[2].lower() for x in matched})
    online_items = sorted([x for x in matched if x[3] == 1], key=lambda x: x[1])
    online_prices = [x[1] for x in online_items]
    online_people = len({x[2].lower() for x in online_items})
    online_auto_cnt = sum(x[4] for x in online_items)

    median_all = all_prices[len(all_prices) // 2]
    avg_all = sum(all_prices) / len(all_prices)
    online_ratio = online_people / max(1, total_sellers)

    has_dumper = False
    if len(online_prices) >= 2 and online_prices[0] < online_prices[1] * 0.78:
        has_dumper = True

    # 1. Расчёт ценовых уровней
    if online_prices:
        min_on = online_prices[0]
        second_on = online_prices[1] if len(online_prices) >= 2 else min_on

        if has_dumper:
            # Игнорируем разового демпера при расчёте рабочей цены
            entry_price = round(max(min_user_price, second_on * 0.96))
            work_price = round(max(entry_price, (second_on + median_all) / 2))
        else:
            entry_price = round(max(min_user_price, min_on * 0.97))
            ref_mid = online_prices[1] if len(online_prices) >= 3 else median_all
            work_price = round(max(entry_price, min(median_all, ref_mid * 1.03)))

        auto_price = round(max(work_price * 1.08, min_on * 1.12))
    else:
        min_on = None
        entry_price = round(max(min_user_price, median_all * 0.92))
        work_price = round(max(entry_price, median_all * 1.05))
        auto_price = round(work_price * 1.12)

    margin_work_pct = ((median_all - work_price) / median_all * 100) if median_all > 0 else 0.0

    # 2. Подсказки: КОГДА ПОВЫСИТЬ ЦЕНУ
    raise_tips = []
    if online_people == 0:
        raise_tips.append(
            f"🟢 <b>Прямо сейчас в сети 0 конкурентов!</b> Можно смело ставить цену <b>на +10–15% выше медианы</b> "
            f"(до <code>{round(median_all * 1.15)} ₽</code>) — покупатели заберут у того, кто онлайн."
        )
    elif online_people <= 2 or online_ratio < 0.25:
        raise_tips.append(
            f"🟢 <b>Низкий онлайн в нише ({online_people} из {total_sellers} чел.):</b> конкуренция слабая, "
            f"повышайте цену ближе к <code>{work_price}–{round(median_all * 1.08)} ₽</code>."
        )

    if has_dumper and len(online_prices) >= 2:
        raise_tips.append(
            f"🛡 <b>Обнаружен демпер (<code>{online_prices[0]:.0f} ₽</code> против <code>{online_prices[1]:.0f} ₽</code>):</b> "
            f"не опускайтесь под него — у него быстро закончится товар, ставьте <code>{entry_price} ₽</code>."
        )

    if online_auto_cnt == 0 and online_people > 0:
        raise_tips.append(
            f"⚡ <b>У онлайн-конкурентов нет автовыдачи:</b> подключите автовыдачу и ставьте на <b>+10–15% дороже</b> (<code>{auto_price} ₽</code>)."
        )
    elif sales_24h >= 3 and online_people <= 3:
        raise_tips.append(
            f"🔥 <b>Высокий темп выкупа ({sales_24h} прод./24ч):</b> спрос превышает предложение в сети — протестируйте повышение цены на <b>+5–10%</b>."
        )

    if not raise_tips:
        raise_tips.append(
            f"▸ Повышайте цену на <b>+8–15%</b> в ночные/утренние часы (когда в сети остаётся ≤2 продавцов) или при включённой автовыдаче ⚡."
        )

    # 3. Подсказки: КОГДА СНИЗИТЬ ЦЕНУ
    lower_tips = []
    if online_people >= 4 or (online_ratio >= 0.6 and total_sellers >= 3):
        lower_tips.append(
            f"🔴 <b>Высокая конкуренция онлайн ({online_people} чел. в сети):</b> чтобы забирать трафик с верха витрины, "
            f"снизьте цену до <code>{entry_price} ₽</code> (на 3% ниже минимальной в сети)."
        )

    if sales_24h == 0 and len(matched) >= 4:
        lower_tips.append(
            f"📉 <b>Застой продаж за 24ч (0 уходов):</b> рынок перегрет по цене — временно опустите лот до <code>{entry_price} ₽</code> для разгона карточки."
        )

    if current_hour_warn:
        lower_tips.append(
            f"⏰ <b>Сейчас час пиковой активности продавцов:</b> либо снижайте цену на 3–5%, либо перенесите поднятие лота на свободное окно."
        )

    if not lower_tips:
        lower_tips.append(
            f"▸ Снижайте цену на <b>3–5%</b> только если в сети одновременно ≥4 конкурентов с автовыдачей или если лот не покупали более 24 часов."
        )

    return {
        "min_on_str": f"{min_on:.0f} ₽" if min_on is not None else "—",
        "median_all": median_all,
        "avg_all": avg_all,
        "online_people": online_people,
        "total_sellers": total_sellers,
        "entry_price": entry_price,
        "work_price": work_price,
        "auto_price": auto_price,
        "margin_work_pct": margin_work_pct,
        "raise_tips": raise_tips,
        "lower_tips": lower_tips,
    }


# --- ГЕНЕРАЦИЯ ГРАФИКА 24Ч (MATPLOTLIB) ---
async def build_24h_chart_image() -> bytes | None:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT strftime('%H', scraped_at) as hour, AVG(is_online) * 100 as online_pct
            FROM lots_history GROUP BY hour ORDER BY hour ASC
        """)
        comp_rows = dict(await cursor.fetchall())

        cursor = await db.execute("""
            SELECT strftime('%H', sold_at) as hour, COUNT(*) as sales_cnt
            FROM sold_events GROUP BY hour ORDER BY hour ASC
        """)
        sales_rows = dict(await cursor.fetchall())

    if not comp_rows:
        return None

    hours = [f"{h:02d}" for h in range(24)]
    online_vals = [comp_rows.get(h, 0.0) for h in hours]
    sales_vals = [sales_rows.get(h, 0) for h in hours]

    plt.style.use("dark_background")
    fig, ax1 = plt.subplots(figsize=(10, 4.8), dpi=140)
    fig.patch.set_facecolor("#11161d")
    ax1.set_facecolor("#161d26")

    ax1.bar(hours, sales_vals, color="#2ecc71", alpha=0.45, width=0.6)
    ax1.set_ylabel("Уходы лотов с витрины (шт.)", color="#2ecc71", fontsize=10, fontweight="bold")
    ax1.tick_params(axis="y", labelcolor="#2ecc71")
    ax1.set_xlabel("Час суток", color="#b0bec5", fontsize=10)
    ax1.grid(axis="y", linestyle="--", alpha=0.18)

    ax2 = ax1.twinx()
    ax2.plot(hours, online_vals, color="#38bdf8", marker="o", linewidth=2.3, markersize=5)
    ax2.set_ylabel("Конкуренты онлайн (%)", color="#38bdf8", fontsize=10, fontweight="bold")
    ax2.tick_params(axis="y", labelcolor="#38bdf8")
    ax2.set_ylim(0, max(100, max(online_vals) * 1.15))

    plt.title("FunPay AI: Спрос покупателей vs Онлайн конкурентов (по часам)", fontsize=12, fontweight="bold", color="#f8fafc", pad=14)
    fig.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# --- ГЕНЕРАТОРЫ ЭКРАНОВ ---

# 1. ГЛАВНОЕ МЕНЮ
async def render_main_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT COUNT(*), SUM(is_online), SUM(auto_delivery),
                   COUNT(DISTINCT LOWER(seller)),
                   COUNT(DISTINCT CASE WHEN is_online = 1 THEN LOWER(seller) END)
            FROM last_snapshot
        """)
        row = await cursor.fetchone()
        total_lots = row[0] or 0
        total_online_lots = row[1] or 0
        total_auto = row[2] or 0
        total_sellers = row[3] or 0
        online_sellers = row[4] or 0

        cursor = await db.execute("""
            SELECT category,
                   COUNT(DISTINCT CASE WHEN is_online = 1 THEN LOWER(seller) END),
                   COUNT(DISTINCT LOWER(seller))
            FROM last_snapshot
            GROUP BY category
        """)
        cat_online_map = {r[0]: (r[1] or 0, r[2] or 0) for r in await cursor.fetchall()}

    if cfg["is_pro"]:
        sub_line = f"💎 <b>PRO</b> <i>(до {cfg['pro_until'].strftime('%d.%m %H:%M')})</i>"
        spy_btn = f"🕵️ Слежка · {len(cfg['tracked_sellers'])}"
    else:
        sub_line = "⚪ <b>FREE</b> <i>(базовый)</i>"
        spy_btn = "🔒 Слежка"

    cat_badges = []
    for cat_name in TARGET_URLS.keys():
        on_s, tot_s = cat_online_map.get(cat_name, (0, 0))
        cat_badges.append(f"{cat_name}: <b>{on_s}</b>/{tot_s}")
    cats_online_str = " · ".join(cat_badges)

    mode_parts = ["Онлайн" if cfg["only_online"] else "Все"]
    if cfg["only_auto"]:
        mode_parts.append("Автовыдача")
    if cfg["min_reviews"] > 0:
        mode_parts.append(f"от {cfg['min_reviews']} отз.")

    text = (
        f"<b>FUNPAY AI · ТЕРМИНАЛ</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Тариф: {sub_line}\n"
        f"▪ Баланс: <code>{cfg['balance']:.0f} ₽</code>\n"
        f"▪ Продавцов в сети: 🟢 <b>{online_sellers} чел.</b> из {total_sellers}\n"
        f"▪ Лотов в каталоге: <b>{total_lots}</b> <i>({total_online_lots} онлайн · {total_auto} авто)</i>\n"
        f"▪ Срез от <code>{LAST_SCAN_TIME}</code>"
        f"</blockquote>\n\n"
        f"<b>Людей онлайн по разделам:</b>\n"
        f"<i>{cats_online_str}</i>\n\n"
        f"<b>Активный фильтр:</b> {' · '.join(mode_parts)} · <code>{cfg['min_price']:.0f}–{cfg['max_price']:.0f} ₽</code>"
    )

    support_url = build_admin_chat_url("Привет! У меня вопрос по FunPay AI Terminal.")

    rows = [
        [
            InlineKeyboardButton(text="📊 Аналитика", callback_data="m:an"),
            InlineKeyboardButton(text="🔥 Витрина", callback_data="m:lt:0:0"),
        ],
        [
            InlineKeyboardButton(text="🏆 Топ продавцов", callback_data="m:tp:0"),
            InlineKeyboardButton(text=spy_btn, callback_data="m:sp"),
        ],
        [
            InlineKeyboardButton(text="🔎 Поиск", callback_data="in:srch"),
            InlineKeyboardButton(text="⚙️ Фильтры", callback_data="m:fl"),
        ],
        [
            InlineKeyboardButton(text="💎 Подписка PRO", callback_data="m:sub"),
            InlineKeyboardButton(text="💳 Пополнение", callback_data="m:topup"),
        ],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="m:pr"),
            InlineKeyboardButton(text="💬 Поддержка", url=support_url),
        ],
        [
            InlineKeyboardButton(text="↻ Обновить", callback_data="m:main"),
        ],
    ]

    if user.id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="👑 Панель администратора", callback_data="m:adm")])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# 1.1 ШАГ 1: ВЫБОР ТАРИФА PRO
async def render_sub_store(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)

    if cfg["is_pro"]:
        status_line = f"💎 Активен до <b>{cfg['pro_until'].strftime('%d.%m.%Y %H:%M')}</b>"
    else:
        status_line = "⚪ Не активен <i>(тариф FREE)</i>"

    text = (
        f"<b>ПОДПИСКА PRO · ВЫБОР ТАРИФА</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Статус: {status_line}\n"
        f"▪ Баланс кошелька: <code>{cfg['balance']:.0f} ₽</code>"
        f"</blockquote>\n\n"
        f"<b>Что открывает подписка PRO:</b>\n"
        f"▸ <b>Рекомендации по цене</b> — расчёт цены быстрого входа, рабочей цены и наценки за автовыдачу ⚡ для каждой услуги\n"
        f"▸ <b>Подсказки по повышению и снижению цены</b> — сигналы по демперам, темпу выкупа и загруженности витрины\n"
        f"▸ <b>Лучшее время публикации</b> — окна минимальной конкуренции отдельно для каждого раздела и каждой услуги\n"
        f"▸ <b>Слежка за конкурентами</b> (до 8 продавцов) и чтение полных описаний лотов\n"
        f"▸ <b>Почасовые графики спроса</b> и конкуренции (24ч)\n\n"
        f"<i>Выберите тариф для оплаты или пополните баланс Звездами:</i>"
    )

    buttons = []
    for code, (title, days, price) in SUB_PLANS.items():
        stars_cnt = int(round(price / RUB_PER_STAR))
        buttons.append([
            InlineKeyboardButton(
                text=f"💎 {title} · {price:.0f} ₽ (~{stars_cnt} ⭐️)",
                callback_data=f"sub:plan:{code}"
            )
        ])

    support_url = build_admin_chat_url("Привет! У меня вопрос по FunPay AI Terminal.")
    buttons.append([
        InlineKeyboardButton(text="💳 Пополнение", callback_data="m:topup")
    ])
    buttons.append([
        InlineKeyboardButton(text="💬 Поддержка", url=support_url),
        InlineKeyboardButton(text="← Меню", callback_data="m:main"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# 1.1.1 ОБЩИЙ РАЗДЕЛ «ПОПОЛНЕНИЕ» (CRYPTOBOT + ЗВЕЗДЫ)
async def render_topup_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    topup_stars_url = build_admin_chat_url(
        f"Привет! Хочу пополнить баланс / оплатить Звездами ⭐️ в FunPay AI Terminal.\nМой ID: {user.id}"
    )
    support_url = build_admin_chat_url("Привет! У меня вопрос по пополнению в FunPay AI Terminal.")

    text = (
        f"<b>💳 ПОПОЛНЕНИЕ БАЛАНСА</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Ваш текущий баланс: <code>{cfg['balance']:.0f} ₽</code>\n"
        f"▪ Курс CryptoBot: <code>1 {CRYPTO_ASSET} ≈ {USD_TO_RUB:.0f} ₽</code>\n"
        f"▪ Курс Telegram Stars: <code>1 ⭐️ ≈ {RUB_PER_STAR:.0f} ₽</code>"
        f"</blockquote>\n\n"
        f"<b>Выберите удобный способ пополнения:</b>\n"
        f"▸ <b>🦋 Оплата CryptoBot</b> — автоматическое создание счёта в <code>{CRYPTO_ASSET}</code> и моментальное зачисление на баланс.\n"
        f"▸ <b>⭐️ Оплата Звездами</b> — откроется чат с администратором для быстрого пополнения Telegram Stars."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🦋 Оплата CryptoBot ({CRYPTO_ASSET})", callback_data="m:topup_cb")],
        [InlineKeyboardButton(text="⭐️ Оплата Звездами", url=topup_stars_url)],
        [
            InlineKeyboardButton(text="💎 Тарифы PRO", callback_data="m:sub"),
            InlineKeyboardButton(text="💬 Поддержка", url=support_url),
        ],
        [InlineKeyboardButton(text="← Главное меню", callback_data="m:main")],
    ])
    return text, kb


async def render_topup_cryptobot_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    text = (
        f"<b>🦋 ПОПОЛНЕНИЕ ЧЕРЕЗ CRYPTOBOT</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Текущий баланс: <code>{cfg['balance']:.0f} ₽</code>\n"
        f"▪ Валюта оплаты: <b>{CRYPTO_ASSET}</b>"
        f"</blockquote>\n\n"
        f"Выберите сумму пополнения (или введите свою сумму):"
    )
    amounts = [99, 299, 500, 790, 1000, 2000]
    amt_buttons = []
    for rub in amounts:
        usdt = round(rub / USD_TO_RUB, 2)
        amt_buttons.append(
            InlineKeyboardButton(text=f"+{rub} ₽ (~{usdt} {CRYPTO_ASSET})", callback_data=f"cb:topup:{rub}")
        )
    rows = chunk_buttons(amt_buttons, 2)
    rows.append([InlineKeyboardButton(text="✏️ Ввести свою сумму", callback_data="in:topup")])
    rows.append([InlineKeyboardButton(text="← Назад в Пополнение", callback_data="m:topup")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# 1.1.2 ШАГ 2: СТРАНИЦА ОФОРМЛЕНИЯ ВЫБРАННОГО ТАРИФА
async def render_plan_checkout(user: types.User, plan_code: str):
    if plan_code not in SUB_PLANS:
        return await render_sub_store(user)

    cfg = await get_user_profile(user, inc_req=False)
    title, days, price = SUB_PLANS[plan_code]
    amount_crypto = round(price / USD_TO_RUB, 2)
    stars_cnt = int(round(price / RUB_PER_STAR))

    stars_pay_url = build_admin_chat_url(
        f"Привет! Хочу оплатить тариф «{title}» ({price:.0f} ₽ / ~{stars_cnt} ⭐️) Звездами.\nМой ID в боте: {user.id}"
    )

    text = (
        f"<b>ОФОРМЛЕНИЕ · {html.escape(title.upper())}</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Тариф: <b>{html.escape(title)}</b> <i>({days} дн.)</i>\n"
        f"▪ Стоимость: <code>{price:.0f} ₽</code> <i>(~{amount_crypto} {CRYPTO_ASSET} / ~{stars_cnt} ⭐️)</i>\n"
        f"▪ Ваш баланс: <code>{cfg['balance']:.0f} ₽</code>"
        f"</blockquote>\n\n"
        f"<i>Выберите удобный способ оплаты ниже (при выборе Звезд откроется чат с администратором):</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐️ Оплатить Звездами (~{stars_cnt} ⭐️)", url=stars_pay_url)],
        [InlineKeyboardButton(text=f"🦋 Оплатить CryptoBot ({amount_crypto} {CRYPTO_ASSET})", callback_data=f"cb:pay:{plan_code}")],
        [InlineKeyboardButton(text=f"💳 Оплатить с баланса ({price:.0f} ₽)", callback_data=f"buy:pro:{plan_code}")],
        [InlineKeyboardButton(text="← К выбору тарифа", callback_data="m:sub")],
    ])
    return text, kb


# 1.2 АДМИН-ПАНЕЛЬ
async def render_admin_panel():
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM tg_users")
        total_users = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM tg_users WHERE pro_until > ?", (datetime.now(),))
        active_pros = (await cursor.fetchone())[0]

        cursor = await db.execute("""
            SELECT tg_id, username, full_name, balance, pro_until 
            FROM tg_users ORDER BY joined_at DESC LIMIT 6
        """)
        recent_users = await cursor.fetchall()

    users_lines = []
    for uid, uname, fname, bal, pro_u in recent_users:
        u_tag = f"@{html.escape(uname)}" if uname else html.escape(fname[:14])
        pro_mark = "💎" if (pro_u and datetime.fromisoformat(str(pro_u)) > datetime.now()) else "·"
        users_lines.append(f"{pro_mark} <code>{uid}</code> · {u_tag} — <code>{bal:.0f} ₽</code>")

    op_state = f"🟢 ВКЛ ({html.escape(str(REQUIRED_CHANNEL_ID))})" if CHECK_SUB_ENABLED else "⚪ ВЫКЛ"

    text = (
        f"<b>АДМИН-ПАНЕЛЬ</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Пользователей: <b>{total_users}</b>\n"
        f"▪ Активных PRO: <b>{active_pros}</b>\n"
        f"▪ Обязательная подписка: {op_state}"
        f"</blockquote>\n\n"
        f"<b>Последние аккаунты:</b>\n" + "\n".join(users_lines)
    )

    op_btn_text = "📢 ОП канала: ВКЛ" if CHECK_SUB_ENABLED else "📢 ОП канала: ВЫКЛ"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Начислить баланс", callback_data="in:adm_bal"),
            InlineKeyboardButton(text="💎 Выдать PRO", callback_data="in:adm_pro"),
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка всем", callback_data="in:adm_bc"),
            InlineKeyboardButton(text=op_btn_text, callback_data="adm:toggle_op"),
        ],
        [
            InlineKeyboardButton(text="+1000 ₽ себе", callback_data="adm:self_1000"),
            InlineKeyboardButton(text="PRO 30д себе", callback_data="adm:self_pro"),
        ],
        [InlineKeyboardButton(text="← Главное меню", callback_data="m:main")],
    ])
    return text, kb


# --- 2. ТРЁХУРОВНЕВАЯ АНАЛИТИКА С PRO-СТРАТЕГИЯМИ ЦЕН И ВРЕМЕНИ ---
async def render_analytics_categories(user: types.User):
    cfg = await get_user_profile(user, inc_req=True)
    cats = cfg["enabled_cats"]
    if not cats:
        text = "⚠️ <b>Все разделы отключены в фильтрах.</b>\nВключите хотя бы одну нейросеть в настройках."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Открыть фильтры", callback_data="m:fl")],
            [InlineKeyboardButton(text="← Главное меню", callback_data="m:main")],
        ])
        return text, kb

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT category,
                   COUNT(DISTINCT CASE WHEN is_online = 1 THEN LOWER(seller) END),
                   COUNT(DISTINCT LOWER(seller)),
                   COUNT(*)
            FROM last_snapshot
            GROUP BY category
        """)
        stats_map = {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in await cursor.fetchall()}

    pro_tag = "💎 PRO" if cfg["is_pro"] else "FREE"
    text = (
        f"<b>АНАЛИТИКА · ВЫБОР РАЗДЕЛА</b> <i>[{pro_tag}]</i>\n"
        f"<code>──────────────────────────</code>\n"
        f"Выберите нейросеть, чтобы увидеть лучшее время публикации по разделу и перейти к ценовым рекомендациям по услугам:\n\n"
    )

    cat_buttons = []
    for idx, c_name in enumerate(cats):
        on_s, tot_s, tot_l = stats_map.get(c_name, (0, 0, 0))
        cat_buttons.append([
            InlineKeyboardButton(
                text=f"📊 {c_name} · 🟢 {on_s}/{tot_s} чел. ({tot_l} лот.)",
                callback_data=f"an:cat:{idx}"
            )
        ])

    chart_btn = "📈 График 24ч" if cfg["is_pro"] else "🔒 График 24ч"
    cat_buttons.append([
        InlineKeyboardButton(text="🕒 Лучшие часы", callback_data="m:bt:-1"),
        InlineKeyboardButton(text=chart_btn, callback_data="ch:24h"),
    ])
    cat_buttons.append([InlineKeyboardButton(text="← Главное меню", callback_data="m:main")])

    return text, InlineKeyboardMarkup(inline_keyboard=cat_buttons)


async def render_analytics_segments(user: types.User, cat_idx: int = 0):
    cfg = await get_user_profile(user, inc_req=True)
    cats = cfg["enabled_cats"]
    if not cats:
        return await render_analytics_categories(user)

    cat_idx = cat_idx % len(cats)
    category = cats[cat_idx]
    day_ago = datetime.now() - timedelta(hours=24)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT segment, title, price, seller, is_online, auto_delivery, reviews_count, sub_params, is_promo
            FROM last_snapshot WHERE category = ?
        """, (category,))
        raw_rows = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT segment, COUNT(*) FROM sold_events
            WHERE category = ? AND sold_at >= ? GROUP BY segment
        """, (category, day_ago))
        sales_map = dict(await cursor.fetchall())

        hourly_info = await get_hourly_windows(db, category=category, segment=None)

    tot_sellers_cat = len({r[3].lower() for r in raw_rows})
    on_sellers_cat = len({r[3].lower() for r in raw_rows if r[4] == 1})
    promo_lots_cat = sum(r[8] for r in raw_rows)
    avg_price_cat = (sum(r[2] for r in raw_rows) / len(raw_rows)) if raw_rows else 0.0
    total_sales_cat = sum(sales_map.values())

    filtered_rows = [
        r for r in raw_rows
        if pass_user_filters(r[1], r[2], r[4], r[5], r[6], r[7], cfg)
    ]

    ordered_segments = [s[0] for s in SEGMENTS_CONFIG] + [DEFAULT_SEGMENT]
    seg_counts = {}
    for seg, title, price, seller, is_on, auto_d, rev_c, sub_p, _ in filtered_rows:
        s_name = seg or DEFAULT_SEGMENT
        seg_counts.setdefault(s_name, {"lots": 0, "people": set()})
        seg_counts[s_name]["lots"] += 1
        if is_on == 1:
            seg_counts[s_name]["people"].add(seller.lower())

    pro_tag = "💎 PRO" if cfg["is_pro"] else "FREE"
    text = (
        f"<b>АНАЛИТИКА · {html.escape(category.upper())}</b> <i>[{pro_tag}]</i>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ В сети в разделе: 🟢 <b>{on_sellers_cat} чел.</b> из {tot_sellers_cat} продавцов\n"
        f"▪ Средний чек: <code>{avg_price_cat:.0f} ₽</code> · Промо-лотов: <b>{promo_lots_cat}</b>\n"
        f"▪ Продаж за 24ч: <b>{total_sales_cat}</b> · Подходит под фильтр: <b>{len(filtered_rows)}</b>"
        f"</blockquote>\n\n"
    )

    if cfg["is_pro"]:
        if hourly_info["ready"] and hourly_info["best_publish"]:
            win_list = [f"<code>{h}:00–{h}:59</code> (онлайн {pct:.0f}%)" for h, pct, _ in hourly_info["best_publish"][:2]]
            best_win_str = ", ".join(win_list)
        else:
            best_win_str = "<i>накапливается почасовая история...</i>"

        online_share = (on_sellers_cat / max(1, tot_sellers_cat)) * 100
        if on_sellers_cat <= 2 or online_share < 25:
            section_tip = "📈 <b>Сигнал раздела:</b> мало продавцов онлайн — удачный момент держать цену выше средней."
        elif online_share >= 60 and total_sales_cat == 0:
            section_tip = "📉 <b>Сигнал раздела:</b> высокая плотность продавцов в сети — выигрывает автовыдача ⚡ и конкурентная цена."
        else:
            section_tip = "⚖️ <b>Сигнал раздела:</b> сбалансированный рынок — ориентируйтесь на рабочую цену внутри услуги."

        text += (
            f"<b>💎 PRO-сводка по разделу {html.escape(category)}:</b>\n"
            f"▸ 🕒 <b>Лучшее время публикации:</b> {best_win_str}\n"
            f"▸ {section_tip}\n\n"
        )
    else:
        text += (
            f"<i>🔒 Лучшее время публикации для {html.escape(category)} и ценовые рекомендации по услугам — в подписке PRO.</i>\n\n"
        )

    text += f"<i>Выберите конкретную услугу внутри {html.escape(category)}:</i>"

    buttons = []
    for seg_idx, seg_name in enumerate(ordered_segments):
        info = seg_counts.get(seg_name)
        lots_c = info["lots"] if info else 0
        ppl_c = len(info["people"]) if info else 0
        sales_c = sales_map.get(seg_name, 0)
        buttons.append([
            InlineKeyboardButton(
                text=f"{seg_name} · {lots_c} лот. · 🟢{ppl_c} · 🛒{sales_c}",
                callback_data=f"an:seg:{cat_idx}:{seg_idx}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(text=f"🕒 Часы {category}", callback_data=f"m:bt:{cat_idx}"),
        InlineKeyboardButton(text="← Разделы", callback_data="m:an"),
    ])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="m:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_analytics_segment_detail(user: types.User, cat_idx: int, seg_idx: int):
    cfg = await get_user_profile(user, inc_req=True)
    cats = cfg["enabled_cats"]
    if not cats:
        return await render_analytics_categories(user)

    cat_idx = cat_idx % len(cats)
    category = cats[cat_idx]
    ordered_segments = [s[0] for s in SEGMENTS_CONFIG] + [DEFAULT_SEGMENT]
    seg_idx = seg_idx % len(ordered_segments)
    seg_name = ordered_segments[seg_idx]
    day_ago = datetime.now() - timedelta(hours=24)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT title, price, seller, is_online, auto_delivery, reviews_count, sub_params, url
            FROM last_snapshot WHERE category = ? AND segment = ?
        """, (category, seg_name))
        raw_rows = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT COUNT(*), SUM(auto_delivery) FROM sold_events
            WHERE category = ? AND segment = ? AND sold_at >= ?
        """, (category, seg_name, day_ago))
        s_row = await cursor.fetchone()
        sales_24h = s_row[0] or 0
        auto_sales_24h = s_row[1] or 0

        hourly_info = await get_hourly_windows(db, category=category, segment=seg_name)

    matched = [
        r for r in raw_rows
        if pass_user_filters(r[0], r[1], r[3], r[4], r[5], r[6], cfg)
    ]

    pro_tag = "💎 PRO" if cfg["is_pro"] else "FREE"
    text = (
        f"<b>{html.escape(category.upper())} · {html.escape(seg_name.upper())}</b> <i>[{pro_tag}]</i>\n"
        f"<code>──────────────────────────</code>\n"
    )

    if not matched:
        text += (
            f"<i>В этой услуге пока нет лотов, подходящих под ваши фильтры "
            f"({cfg['min_price']:.0f}–{cfg['max_price']:.0f} ₽).</i>"
        )
    else:
        all_prices = sorted(x[1] for x in matched)
        online_items = sorted([x for x in matched if x[3] == 1], key=lambda x: x[1])
        online_people = len({x[2].lower() for x in online_items})
        auto_cnt = sum(x[4] for x in matched)
        median_all = all_prices[len(all_prices) // 2]
        avg_all = sum(all_prices) / len(all_prices)

        if cfg["is_pro"]:
            adv = build_pro_pricing_advisor(
                matched=matched,
                sales_24h=sales_24h,
                auto_sales_24h=auto_sales_24h,
                min_user_price=cfg["min_price"],
                current_hour_warn=hourly_info["current_hour_warn"],
            )

            text += (
                f"<blockquote>"
                f"▪ Лотов по услуге: <b>{len(matched)}</b> <i>(автовыдача: ⚡{auto_cnt})</i>\n"
                f"▪ Продавцов в сети: 🟢 <b>{online_people} чел.</b> из {adv['total_sellers']}\n"
                f"▪ Мин. онлайн / Медиана: <code>{adv['min_on_str']}</code> / <code>{median_all:.0f} ₽</code>\n"
                f"▪ Продаж за 24ч: <b>{sales_24h}</b> <i>(с автовыдачей: {auto_sales_24h})</i>"
                f"</blockquote>\n\n"
                f"<b>💰 Рекомендации по цене (PRO):</b>\n"
                f"▸ Быстрый вход (набор отзывов): <code>{adv['entry_price']} ₽</code>\n"
                f"▸ Рабочая цена (оптимум): <code>{adv['work_price']} ₽</code> <i>(маржа {adv['margin_work_pct']:+.0f}%)</i>\n"
                f"▸ С автовыдачей ⚡: <code>{adv['auto_price']} ₽</code>\n\n"
            )

            # Лучшее время публикации именно для этой услуги
            text += f"<b>🕒 Лучшее время публикации ({html.escape(hourly_info['scope'])}):</b>\n"
            if hourly_info["ready"] and hourly_info["best_publish"]:
                for h, on_pct, s_cnt in hourly_info["best_publish"][:2]:
                    text += f"▸ <code>{h}:00–{h}:59</code> — онлайн <b>{on_pct:.0f}%</b> · выкупов: <b>{s_cnt}</b>\n"
                if hourly_info["peak_comp"]:
                    worst_h, worst_pct = hourly_info["peak_comp"][0]
                    text += f"▸ ⚠️ Пик конкурентов: <code>{worst_h}:00–{worst_h}:59</code> ({worst_pct:.0f}% в сети)\n"
            else:
                text += "▸ <i>Почасовая база по этой услуге ещё собирается — проверьте позже.</i>\n"

            # Подсказки когда повысить / снизить цену
            text += "\n<b>📈 Когда лучше повысить цену:</b>\n"
            for tip in adv["raise_tips"][:2]:
                text += f"{tip}\n"

            text += "\n<b>📉 Когда лучше снизить цену:</b>\n"
            for tip in adv["lower_tips"][:2]:
                text += f"{tip}\n"

            if online_items:
                text += "\n<b>Топ-3 конкурента в сети по минимальной цене:</b>\n"
                for title, price, seller, _, auto_d, rev_c, _, url in online_items[:3]:
                    a_icon = "⚡" if auto_d == 1 else ""
                    text += f"▸ <code>{price:.0f} ₽</code>{a_icon} — <a href='{url}'>{html.escape(title[:34])}</a> <i>({html.escape(seller)} · ⭐{rev_c})</i>\n"
        else:
            text += (
                f"<blockquote>"
                f"▪ Лотов по услуге: <b>{len(matched)}</b>\n"
                f"▪ Продавцов в сети: 🟢 <b>{online_people} чел.</b>\n"
                f"▪ Медиана / Средняя: <code>{median_all:.0f} ₽</code> / <code>{avg_all:.0f} ₽</code>\n"
                f"▪ Продаж за 24ч: <b>{sales_24h}</b>"
                f"</blockquote>\n\n"
                f"🔒 <b>Доступно в подписке PRO:</b>\n"
                f"▸ Расчёт цены быстрого входа, рабочей цены и цены под автовыдачу ⚡\n"
                f"▸ Лучшее время публикации и поднятия лота именно для этой услуги\n"
                f"▸ Подсказки, когда выгоднее повысить, а когда снизить цену"
            )

    kb_rows = []
    if not cfg["is_pro"]:
        kb_rows.append([InlineKeyboardButton(text="💎 Открыть рекомендации PRO", callback_data="m:sub")])
    kb_rows.append([
        InlineKeyboardButton(text=f"← К услугам {category}", callback_data=f"an:cat:{cat_idx}"),
        InlineKeyboardButton(text="↻ Обновить", callback_data=f"an:seg:{cat_idx}:{seg_idx}"),
    ])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="m:main")])

    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


# 2.1 ЛУЧШИЕ ЧАСЫ (ПО ВСЕМУ КАТАЛОГУ И ПО КАЖДОМУ РАЗДЕЛУ)
async def render_best_time(user: types.User, cat_idx: int = -1):
    cfg = await get_user_profile(user, inc_req=False)
    cats = cfg["enabled_cats"]

    selected_cat = None
    if 0 <= cat_idx < len(cats):
        selected_cat = cats[cat_idx]
    else:
        cat_idx = -1

    async with aiosqlite.connect(DB_NAME) as db:
        hourly_info = await get_hourly_windows(db, category=selected_cat, segment=None)

    scope_title = selected_cat.upper() if selected_cat else "ВСЕ РАЗДЕЛЫ"
    pro_tag = "💎 PRO" if cfg["is_pro"] else "FREE"

    text = (
        f"<b>ЛУЧШЕЕ ВРЕМЯ ПУБЛИКАЦИИ · {html.escape(scope_title)}</b> <i>[{pro_tag}]</i>\n"
        f"<code>──────────────────────────</code>\n"
    )

    if not hourly_info["ready"]:
        text += "<i>Почасовая статистика ещё накапливается. Проверьте через пару часов.</i>"
    else:
        if cfg["is_pro"] and hourly_info["best_publish"]:
            best_lines = [
                f"▸ <code>{h}:00–{h}:59</code> — онлайн <b>{pct:.1f}%</b> · выкупов: <b>{cnt}</b>"
                for h, pct, cnt in hourly_info["best_publish"]
            ]
            text += "<b>💎 Оптимальные часы для публикации и поднятия лотов:</b>\n" + "\n".join(best_lines) + "\n\n"
        elif not cfg["is_pro"]:
            text += "<i>🔒 Персональный подбор окон публикации и поднятия по разделам — в подписке PRO.</i>\n\n"

        comp_sorted = sorted(hourly_info["peak_comp"], key=lambda x: x[1])
        # Берём самые свободные часы
        async with aiosqlite.connect(DB_NAME) as db:
            if selected_cat:
                c_cur = await db.execute("""
                    SELECT strftime('%H', scraped_at) as hour, AVG(is_online) * 100 as online_pct
                    FROM lots_history WHERE category = ?
                    GROUP BY hour ORDER BY online_pct ASC LIMIT 4
                """, (selected_cat,))
            else:
                c_cur = await db.execute("""
                    SELECT strftime('%H', scraped_at) as hour, AVG(is_online) * 100 as online_pct
                    FROM lots_history GROUP BY hour ORDER BY online_pct ASC LIMIT 4
                """)
            low_comp = await c_cur.fetchall()

        comp_lines = [f"▸ <code>{h}:00–{h}:59</code> — в сети <b>{pct:.1f}%</b> продавцов" for h, pct in low_comp]
        text += "<b>Минимум конкурентов онлайн:</b>\n" + "\n".join(comp_lines) + "\n\n"

        text += "<b>Пик активности покупателей:</b>\n"
        if not hourly_info["peak_demand"]:
            text += "<i>Данные по продажам пока собираются</i>"
        else:
            dem_lines = [f"▸ <code>{h}:00–{h}:59</code> — зафиксировано <b>{cnt}</b> уходов" for h, cnt in hourly_info["peak_demand"]]
            text += "\n".join(dem_lines)

    nav_rows = []
    if cats:
        filter_btns = [
            InlineKeyboardButton(
                text="▸ Все" if cat_idx == -1 else "Все",
                callback_data="m:bt:-1"
            )
        ]
        for idx, c_name in enumerate(cats):
            lbl = f"▸ {c_name}" if idx == cat_idx else c_name
            filter_btns.append(InlineKeyboardButton(text=lbl, callback_data=f"m:bt:{idx}"))
        nav_rows.extend(chunk_buttons(filter_btns, 3))

    chart_btn = "📈 Построить график 24ч" if cfg["is_pro"] else "🔒 График 24ч (PRO)"
    back_an_cb = f"an:cat:{cat_idx}" if cat_idx >= 0 else "m:an"
    nav_rows.append([
        InlineKeyboardButton(text=chart_btn, callback_data="ch:24h"),
        InlineKeyboardButton(text="← К аналитике", callback_data=back_an_cb),
    ])
    nav_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="m:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=nav_rows)


# 2.2 ТОП ПРОДАВЦОВ
async def render_top_sellers(user: types.User, cat_idx: int = 0):
    cfg = await get_user_profile(user, inc_req=True)
    cats = cfg["enabled_cats"]
    if not cats:
        return "⚠️ Все разделы отключены в фильтрах.", InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="m:main")]]
        )

    cat_idx = cat_idx % len(cats)
    category = cats[cat_idx]
    period_ago = datetime.now() - timedelta(hours=72)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT seller, COUNT(*) as sales_cnt, ROUND(AVG(price)) as avg_price, ROUND(SUM(price)) as total_rev
            FROM sold_events
            WHERE category = ? AND sold_at >= ?
            GROUP BY seller
            ORDER BY sales_cnt DESC, total_rev DESC
            LIMIT 7
        """, (category, period_ago))
        top_rows = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT seller, MAX(is_online), MAX(reviews_count), MAX(seller_age)
            FROM last_snapshot WHERE category = ? GROUP BY seller
        """, (category,))
        seller_meta = {r[0]: (r[1], r[2] or 0, r[3] or "") for r in await cursor.fetchall()}

    text = (
        f"<b>ТОП ПРОДАВЦОВ · {html.escape(category.upper())}</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<i>Рейтинг по уходам лотов за 72 часа:</i>\n\n"
    )

    if not top_rows:
        text += "<i>Статистика продаж по этому разделу ещё накапливается.</i>\n"
    else:
        lines = []
        for rank, (seller, cnt, avg_p, rev) in enumerate(top_rows, start=1):
            is_on, rev_cnt, age = seller_meta.get(seller, (0, 0, ""))
            dot = "🟢" if is_on == 1 else "⚪"
            spy_mark = " 🕵️" if seller.lower() in [s.lower() for s in cfg["tracked_sellers"]] else ""
            age_part = f" · {age}" if age else ""
            lines.append(
                f"<b>{rank}.</b> {dot} <b>{html.escape(seller)}</b>{spy_mark} <i>(⭐ {rev_cnt}{age_part})</i>\n"
                f"    Продаж: <b>{cnt}</b> · Ср. чек: <code>{avg_p:.0f} ₽</code> · Оборот: ~<code>{rev:.0f} ₽</code>"
            )
        text += "\n".join(lines)

    nav_rows = []
    if len(cats) > 1:
        cat_btns = []
        for idx, c_name in enumerate(cats):
            label = f"▸ {c_name}" if idx == cat_idx else c_name
            cat_btns.append(InlineKeyboardButton(text=label, callback_data=f"m:tp:{idx}"))
        nav_rows.extend(chunk_buttons(cat_btns, 3))

    spy_btn = "🕵️ Взять на слежку" if cfg["is_pro"] else "🔒 Слежка (PRO)"
    nav_rows.append([
        InlineKeyboardButton(text=spy_btn, callback_data="in:spy"),
        InlineKeyboardButton(text="↻ Обновить", callback_data=f"m:tp:{cat_idx}"),
    ])
    nav_rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="m:main")])

    return text, InlineKeyboardMarkup(inline_keyboard=nav_rows)


# 2.3 СЛЕЖКА ЗА КОНКУРЕНТАМИ
async def render_spy_list(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    if not cfg["is_pro"]:
        return render_paywall_view("Слежка за конкурентами", cfg)

    sellers = cfg["tracked_sellers"]

    text = (
        f"<b>СЛЕЖКА ЗА КОНКУРЕНТАМИ</b> <i>({len(sellers)}/8)</i>\n"
        f"<code>──────────────────────────</code>\n"
        f"Выберите продавца для просмотра его витрины, отзывов, автовыдачи и описаний лотов:\n\n"
    )

    buttons = []
    if not sellers:
        text += "<i>Список отслеживаемых продавцов пуст.</i>"
    else:
        async with aiosqlite.connect(DB_NAME) as db:
            for idx, nick in enumerate(sellers[:8]):
                cursor = await db.execute(
                    "SELECT COUNT(*), MAX(is_online), MAX(reviews_count) FROM last_snapshot WHERE LOWER(seller) = LOWER(?)",
                    (nick,)
                )
                lots_cnt, is_on, rev_cnt = await cursor.fetchone()
                lots_cnt = lots_cnt or 0
                rev_cnt = rev_cnt or 0
                dot = "🟢" if is_on == 1 else "⚪"
                buttons.append([
                    InlineKeyboardButton(text=f"{dot} {nick} · {lots_cnt} лот. · ⭐{rev_cnt}", callback_data=f"sp:view:{idx}"),
                    InlineKeyboardButton(text="✕", callback_data=f"sp:del:{idx}"),
                ])

    if len(sellers) < 8:
        buttons.append([InlineKeyboardButton(text="+ Добавить конкурента", callback_data="in:spy")])
    buttons.append([InlineKeyboardButton(text="← Главное меню", callback_data="m:main")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_spy_detail(user: types.User, seller_idx: int):
    cfg = await get_user_profile(user, inc_req=True)
    if not cfg["is_pro"]:
        return render_paywall_view("Слежка за конкурентами", cfg)

    sellers = cfg["tracked_sellers"]
    if seller_idx >= len(sellers):
        return await render_spy_list(user)

    nick = sellers[seller_idx]
    period_ago = datetime.now() - timedelta(hours=72)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT category, segment, title, price, is_online, auto_delivery,
                   reviews_count, seller_age, seller_url, stock_amount, sub_params, url
            FROM last_snapshot
            WHERE LOWER(seller) = LOWER(?)
            ORDER BY category ASC, price ASC
        """, (nick,))
        active_lots = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT category, title, price, strftime('%d.%m %H:%M', sold_at)
            FROM sold_events
            WHERE LOWER(seller) = LOWER(?) AND sold_at >= ?
            ORDER BY id DESC LIMIT 5
        """, (nick, period_ago))
        recent_sales = await cursor.fetchall()

    is_online = any(r[4] == 1 for r in active_lots)
    rev_cnt = max((r[6] or 0) for r in active_lots) if active_lots else 0
    seller_age = next((r[7] for r in active_lots if r[7]), "не указан")
    seller_url = next((r[8] for r in active_lots if r[8]), "")
    status_str = "🟢 В сети" if is_online else "⚪ Оффлайн"

    nick_display = f"<a href='{seller_url}'>{html.escape(nick)}</a>" if seller_url else html.escape(nick)

    text = (
        f"<b>ДОСЬЕ · {nick_display}</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Статус: <b>{status_str}</b> · Отзывов: <b>{rev_cnt}</b>\n"
        f"▪ Стаж: <i>{html.escape(seller_age)}</i> · Лотов: <b>{len(active_lots)}</b>"
        f"</blockquote>\n\n"
        f"<b>Активные лоты (топ-8 по цене):</b>\n"
    )

    if not active_lots:
        text += "<i>Активных лотов в каталоге не найдено</i>\n"
    else:
        for cat, seg, title, price, is_on, auto_d, _, _, _, stock_amt, sub_p, url in active_lots[:8]:
            auto_icon = "⚡" if auto_d == 1 else ""
            extra = []
            if sub_p:
                extra.append(sub_p)
            if stock_amt:
                extra.append(f"ост: {stock_amt}")
            sub_str = f" <i>[{' · '.join(html.escape(x) for x in extra)}]</i>" if extra else ""
            text += f"▸ <code>{price:.0f} ₽</code> {auto_icon} <b>[{html.escape(cat)}]</b> <a href='{url}'>{html.escape(title[:38])}</a>{sub_str}\n"

    text += f"\n<b>Продажи за 72 часа:</b>\n"
    if not recent_sales:
        text += "<i>Уходов лотов не зафиксировано</i>\n"
    else:
        for cat, title, price, sold_dt in recent_sales:
            text += f"▸ <code>{sold_dt}</code> · <code>{price:.0f} ₽</code> [{html.escape(cat)}] {html.escape(title[:36])}\n"

    kb_rows = []
    if active_lots:
        kb_rows.append([InlineKeyboardButton(text="📄 Читать описание топ-лота", callback_data=f"sp:desc:{seller_idx}")])
    kb_rows.append([
        InlineKeyboardButton(text="↻ Обновить", callback_data=f"sp:view:{seller_idx}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sp:del:{seller_idx}"),
    ])
    kb_rows.append([InlineKeyboardButton(text="← К списку конкурентов", callback_data="m:sp")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


# 3. ВИТРИНА ЛОТОВ
async def render_lots_showcase(user: types.User, cat_idx: int = 0, page: int = 0, search_query: str = None):
    cfg = await get_user_profile(user, inc_req=True)
    cats = cfg["enabled_cats"]
    if not cats:
        return "⚠️ Все разделы отключены в фильтрах.", InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="m:main")]]
        )

    cat_idx = cat_idx % len(cats)
    category = cats[cat_idx]
    per_page = 6

    async with aiosqlite.connect(DB_NAME) as db:
        if search_query:
            placeholders = ",".join("?" for _ in cats)
            cursor = await db.execute(f"""
                SELECT category, segment, title, price, seller, is_online, auto_delivery,
                       reviews_count, stock_amount, sub_params, url 
                FROM last_snapshot WHERE category IN ({placeholders}) ORDER BY price ASC
            """, cats)
            raw_rows = await cursor.fetchall()
            matched = [
                r for r in raw_rows
                if pass_user_filters(r[2], r[3], r[5], r[6], r[7], r[9], cfg)
                and search_query.lower() in f"{r[2]} {r[9]}".lower()
            ]
        else:
            cursor = await db.execute("""
                SELECT category, segment, title, price, seller, is_online, auto_delivery,
                       reviews_count, stock_amount, sub_params, url 
                FROM last_snapshot WHERE category = ? ORDER BY price ASC
            """, (category,))
            raw_rows = await cursor.fetchall()
            matched = [
                r for r in raw_rows
                if pass_user_filters(r[2], r[3], r[5], r[6], r[7], r[9], cfg)
            ]

    total_items = len(matched)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    page_items = matched[page * per_page : (page + 1) * per_page]

    header_title = f"ПОИСК · «{html.escape(search_query.upper())}»" if search_query else f"ВИТРИНА · {html.escape(category.upper())}"
    flags = []
    if cfg["only_online"]:
        flags.append("Онлайн")
    if cfg["only_auto"]:
        flags.append("Автовыдача")
    if cfg["min_reviews"] > 0:
        flags.append(f"от {cfg['min_reviews']} отз.")
    flags_str = " · ".join(flags) if flags else "Все лоты"

    text = (
        f"<b>{header_title}</b> <i>({page + 1}/{total_pages})</i>\n"
        f"<code>──────────────────────────</code>\n"
        f"<i>Найдено: {total_items} · {flags_str} · {cfg['min_price']:.0f}–{cfg['max_price']:.0f} ₽</i>\n\n"
    )

    if not page_items:
        text += "<i>Подходящих лотов не найдено. Попробуйте изменить фильтры или запрос.</i>\n"
    else:
        items_lines = []
        for cat, seg, title, price, seller, is_on, auto_d, rev_cnt, stock_amt, sub_p, url in page_items:
            dot = "🟢" if is_on else "⚪"
            auto_icon = " ⚡" if auto_d == 1 else ""
            cat_prefix = f"<b>[{html.escape(cat)}]</b> " if search_query else ""
            meta_extra = []
            if sub_p and cfg["is_pro"]:
                meta_extra.append(sub_p)
            if stock_amt and cfg["is_pro"]:
                meta_extra.append(f"ост: {stock_amt}")
            sub_tag = f" <i>({' · '.join(html.escape(x) for x in meta_extra)})</i>" if meta_extra else ""
            items_lines.append(
                f"▸ <code>{price:.0f} ₽</code>{auto_icon} — {cat_prefix}<a href='{url}'>{html.escape(title[:40])}</a>{sub_tag}\n"
                f"   {dot} {html.escape(seller)} <i>(⭐ {rev_cnt})</i>"
            )
        text += "\n".join(items_lines)

    kb_rows = []
    if not search_query and len(cats) > 1:
        cat_btns = []
        for idx, c_name in enumerate(cats):
            label = f"▸ {c_name}" if idx == cat_idx else c_name
            cat_btns.append(InlineKeyboardButton(text=label, callback_data=f"m:lt:{idx}:0"))
        kb_rows.extend(chunk_buttons(cat_btns, 3))

    if not search_query and total_pages > 1:
        prev_p = (page - 1) % total_pages
        next_p = (page + 1) % total_pages
        kb_rows.append([
            InlineKeyboardButton(text="←", callback_data=f"m:lt:{cat_idx}:{prev_p}"),
            InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="noop"),
            InlineKeyboardButton(text="→", callback_data=f"m:lt:{cat_idx}:{next_p}"),
        ])

    if search_query and len(cfg["saved_tags"]) < 6 and search_query.lower() not in cfg["saved_tags"]:
        kb_rows.append([InlineKeyboardButton(text=f"★ В пресеты: «{search_query[:18]}»", callback_data=f"ps:add:{search_query[:25]}")])

    on_btn = "🟢 Онлайн: ВКЛ" if cfg["only_online"] else "⚪ Онлайн: ВЫКЛ"
    auto_btn = "⚡ Авто: ВКЛ" if cfg["only_auto"] else "⚪ Авто: ВЫКЛ"
    kb_rows.append([
        InlineKeyboardButton(text=on_btn, callback_data=f"tg:on:lt:{cat_idx}"),
        InlineKeyboardButton(text=auto_btn, callback_data=f"tg:au:lt:{cat_idx}"),
    ])
    kb_rows.append([
        InlineKeyboardButton(text="🔎 Поиск", callback_data="in:srch"),
        InlineKeyboardButton(text="← Меню", callback_data="m:main"),
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


# 4. ИНТЕРАКТИВНЫЕ ФИЛЬТРЫ
async def render_filters_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    on_status = "🟢 Включён" if cfg["only_online"] else "⚪ Выключен"
    auto_status = "⚡ Включён" if cfg["only_auto"] else "⚪ Выключен"
    cats_str = ", ".join(cfg["enabled_cats"]) if cfg["enabled_cats"] else "ни одного"
    ex_str = ", ".join(cfg["excludes"][:6]) + ("..." if len(cfg["excludes"]) > 6 else "") if cfg["excludes"] else "нет"

    text = (
        f"<b>НАСТРОЙКА ФИЛЬТРОВ</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Только онлайн: <b>{on_status}</b>\n"
        f"▪ Автовыдача (⚡): <b>{auto_status}</b>\n"
        f"▪ Отзывы продавца: <code>от {cfg['min_reviews']}</code>\n"
        f"▪ Бюджет: <code>{cfg['min_price']:.0f} – {cfg['max_price']:.0f} ₽</code>"
        f"</blockquote>\n\n"
        f"▪ <b>Разделы:</b> {html.escape(cats_str)}\n"
        f"▪ <b>Стоп-слова ({len(cfg['excludes'])}):</b> <i>{html.escape(ex_str)}</i>"
    )

    on_btn = "🟢 Онлайн: ВКЛ" if cfg["only_online"] else "⚪ Онлайн: ВЫКЛ"
    auto_btn = "⚡ Авто: ВКЛ" if cfg["only_auto"] else "⚪ Авто: ВЫКЛ"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=on_btn, callback_data="tg:on:fl:0"),
            InlineKeyboardButton(text=auto_btn, callback_data="tg:au:fl:0"),
        ],
        [
            InlineKeyboardButton(text="100–1000 ₽", callback_data="pr:set:100:1000"),
            InlineKeyboardButton(text="500–3000 ₽", callback_data="pr:set:500:3000"),
            InlineKeyboardButton(text="1500–10к ₽", callback_data="pr:set:1500:10000"),
        ],
        [
            InlineKeyboardButton(text=f"Мин: {cfg['min_price']:.0f} ₽", callback_data="in:min"),
            InlineKeyboardButton(text=f"Макс: {cfg['max_price']:.0f} ₽", callback_data="in:max"),
            InlineKeyboardButton(text=f"Отз: ≥{cfg['min_reviews']}", callback_data="in:rev"),
        ],
        [
            InlineKeyboardButton(text="📂 Разделы", callback_data="m:ct"),
            InlineKeyboardButton(text="🚫 Стоп-слова", callback_data="m:ex"),
        ],
        [InlineKeyboardButton(text="← Главное меню", callback_data="m:main")],
    ])
    return text, kb


async def render_categories_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    all_cats = list(TARGET_URLS.keys())

    text = (
        f"<b>КАТАЛОГ НЕЙРОСЕТЕЙ</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"Нажмите на раздел, чтобы включить или скрыть его в выдаче и аналитике:"
    )

    cat_btns = []
    for idx, cat_name in enumerate(all_cats):
        is_on = cat_name in cfg["enabled_cats"]
        icon = "🟢" if is_on else "⚪"
        cat_btns.append(InlineKeyboardButton(text=f"{icon} {cat_name}", callback_data=f"tg:cat:{idx}"))

    rows = chunk_buttons(cat_btns, 2)
    rows.append([InlineKeyboardButton(text="← Назад в фильтры", callback_data="m:fl")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_excludes_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    ex_display = ", ".join(f"<code>{html.escape(w)}</code>" for w in cfg["excludes"]) if cfg["excludes"] else "список пуст"

    text = (
        f"<b>СТОП-СЛОВА</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"Лоты с этими словами скрываются из выдачи:\n\n"
        f"<blockquote>{ex_display}</blockquote>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+ Добавить слово", callback_data="in:ex"),
            InlineKeyboardButton(text="↻ Сбросить", callback_data="rst:ex"),
        ],
        [InlineKeyboardButton(text="← Назад в фильтры", callback_data="m:fl")],
    ])
    return text, kb


# 5. ПРОФИЛЬ TELEGRAM, ПОПОЛНЕНИЕ ЗВЕЗДАМИ, РЕФЕРАЛЬНАЯ СИСТЕМА И ДОКУМЕНТЫ
async def render_profile_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    joined_dt = cfg["joined_at"]
    days_in_bot = (datetime.now() - joined_dt).days
    role = "Администратор" if user.id == ADMIN_ID else "Пользователь"
    username_str = f"@{html.escape(user.username)}" if user.username else "не указан"
    tags = cfg["saved_tags"]

    if cfg["is_pro"]:
        sub_str = f"💎 PRO <i>(до {cfg['pro_until'].strftime('%d.%m.%Y %H:%M')})</i>"
    else:
        sub_str = "⚪ Базовый (FREE)"

    text = (
        f"<b>ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Аккаунт: <b>{html.escape(user.full_name)}</b> <i>({username_str})</i>\n"
        f"▪ ID: <code>{user.id}</code> · {role}\n"
        f"▪ Баланс: <code>{cfg['balance']:.0f} ₽</code> · {sub_str}\n"
        f"▪ В системе с <code>{joined_dt.strftime('%d.%m.%Y')}</code> ({days_in_bot} дн.) · Запросов: <b>{cfg['requests_count']}</b>"
        f"</blockquote>\n\n"
        f"<b>Сохранённые пресеты поиска ({len(tags)}/6):</b>"
    )

    buttons = []
    for idx, tag in enumerate(tags[:6]):
        buttons.append([
            InlineKeyboardButton(text=f"🔎 {tag}", callback_data=f"ps:run:{idx}"),
            InlineKeyboardButton(text="✕", callback_data=f"ps:del:{idx}"),
        ])

    if len(tags) < 6:
        buttons.append([InlineKeyboardButton(text="+ Добавить пресет", callback_data="in:tag")])

    support_url = build_admin_chat_url("Привет! У меня вопрос по FunPay AI Terminal.")
    buttons.append([
        InlineKeyboardButton(text="💳 Пополнение", callback_data="m:topup"),
        InlineKeyboardButton(text="💎 Подписка PRO", callback_data="m:sub"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🤝 Реферальная система", callback_data="m:ref"),
        InlineKeyboardButton(text="💬 Поддержка", url=support_url),
    ])
    buttons.append([
        InlineKeyboardButton(text="🔒 Конфиденциальность", callback_data="m:priv"),
        InlineKeyboardButton(text="📄 Соглашение", callback_data="m:terms"),
    ])
    buttons.append([InlineKeyboardButton(text="← Главное меню", callback_data="m:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_referral_menu(user: types.User):
    cfg = await get_user_profile(user, inc_req=False)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM tg_users WHERE referrer_id = ?", (user.id,))
        ref_count = (await cursor.fetchone())[0]

    bot_uname = BOT_USERNAME or "Looker_fp_bot"
    ref_link = f"https://t.me/{bot_uname}?start=ref_{user.id}"
    share_text = quote("Аналитика FunPay по нейросетям, живой онлайн продавцов и слежка за конкурентами:")
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={share_text}"

    text = (
        f"<b>РЕФЕРАЛЬНАЯ СИСТЕМА</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"Приглашайте друзей и продавцов FunPay по вашей персональной ссылке и получайте "
        f"<b>{REFERRAL_PERCENT}%</b> на баланс кошелька с каждой их покупки подписки PRO.\n\n"
        f"<blockquote>"
        f"▪ Приглашено пользователей: <b>{ref_count} чел.</b>\n"
        f"▪ Заработано всего: <code>{cfg['ref_earned']:.0f} ₽</code>\n"
        f"▪ Доступно на балансе: <code>{cfg['balance']:.0f} ₽</code>"
        f"</blockquote>\n\n"
        f"<b>Ваша реферальная ссылка</b> <i>(нажмите, чтобы скопировать)</i>:\n"
        f"<code>{ref_link}</code>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
        [InlineKeyboardButton(text="← Назад в профиль", callback_data="m:pr")],
    ])
    return text, kb


def render_privacy_policy():
    text = (
        f"<b>ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"<b>1. Какие данные сохраняются</b>\n"
        f"Бот сохраняет исключительно технические идентификаторы Telegram (User ID, юзернейм, имя профиля), "
        f"настройки фильтров, историю внутреннего баланса и срок действия PRO-подписки.\n\n"
        f"<b>2. Безопасность аккаунтов</b>\n"
        f"Сервис не запрашивает логины, пароли, токены или доступ к вашим аккаунтам на площадке FunPay. "
        f"Весь анализ выполняется по открытым данным витрины.\n\n"
        f"<b>3. Передача третьим лицам</b>\n"
        f"Персональные данные пользователей не передаются третьим лицам и используются только для работы функционала терминала."
        f"</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад в профиль", callback_data="m:pr")]
    ])
    return text, kb


def render_terms_of_use():
    text = (
        f"<b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"<b>1. Назначение сервиса</b>\n"
        f"FunPay AI Terminal является аналитическим инструментом для мониторинга открытых предложений и ценовой динамики.\n\n"
        f"<b>2. Подписка PRO и баланс</b>\n"
        f"Пополнение баланса и оплата подписки PRO активируют расширенный доступ на выбранный период. "
        f"Средства с внутреннего баланса используются для оплаты тарифов внутри бота.\n\n"
        f"<b>3. Ограничение ответственности</b>\n"
        f"Рекомендуемые цены входа носят справочный характер. Пользователь самостоятельно принимает решения при торговле на сторонних площадках."
        f"</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад в профиль", callback_data="m:pr")]
    ])
    return text, kb


# --- ОБРАБОТЧИКИ КОМАНД И НАВИГАЦИИ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()

    ref_id = None
    if command.args and command.args.startswith("ref_"):
        try:
            ref_id = int(command.args.replace("ref_", "").strip())
        except ValueError:
            ref_id = None

    await get_user_profile(message.from_user, inc_req=True, referrer_id=ref_id)

    cleanup_msg = await message.answer("🔄", reply_markup=ReplyKeyboardRemove())
    try:
        await cleanup_msg.delete()
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass

    text, kb = await render_main_menu(message.from_user)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "check_sub")
async def cb_check_subscription(callback: types.CallbackQuery, state: FSMContext):
    if not await is_user_subscribed(callback.from_user.id):
        await callback.answer("❌ Вы ещё не подписались на канал! Подпишитесь и нажмите кнопку снова.", show_alert=True)
        return

    await state.clear()
    await get_user_profile(callback.from_user, inc_req=True)
    await callback.answer("✅ Подписка подтверждена! Добро пожаловать.")
    text, kb = await render_main_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)


@dp.message(F.text.in_({"📊 Сводка", "🔥 Выгодные лоты", "🕒 Лучшее время", "👤 Профиль", "⚡ Панель управления"}))
async def remove_legacy_keyboard(message: types.Message, state: FSMContext):
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    msg = await message.answer("🔄", reply_markup=ReplyKeyboardRemove())
    try:
        await msg.delete()
    except Exception:
        pass
    text, kb = await render_main_menu(message.from_user)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "del_msg")
async def cb_del_msg(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "m:main")
async def cb_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_main_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:sub")
async def cb_sub_store(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_sub_store(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:topup")
async def cb_topup_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_topup_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:topup_cb")
async def cb_topup_cb_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_topup_cryptobot_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("cb:topup:"))
async def cb_cryptobot_topup(callback: types.CallbackQuery):
    try:
        price_rub = float(callback.data.split(":")[2])
    except Exception:
        await callback.answer("Некорректная сумма")
        return

    if not CRYPTOBOT_TOKEN or "ВАШ_API_ТОКЕН" in CRYPTOBOT_TOKEN:
        await callback.answer("Токен CryptoBot не настроен в файле bot.py / .env", show_alert=True)
        return

    await callback.answer("Создаю счёт в CryptoBot...")
    pay_url, amount_crypto = await create_cryptobot_invoice(callback.from_user.id, f"topup:{price_rub:.0f}")
    if not pay_url:
        await callback.answer("Ошибка создания счёта. Проверьте токен CryptoBot.", show_alert=True)
        return

    text = (
        f"🦋 <b>Пополнение баланса через CryptoBot</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Зачисление на баланс: <b>+{price_rub:.0f} ₽</b>\n"
        f"▪ К оплате: <code>{amount_crypto} {CRYPTO_ASSET}</code>\n"
        f"▪ Время на оплату: <b>30 минут</b>"
        f"</blockquote>\n\n"
        f"Нажмите кнопку ниже — откроется счёт в <b>@CryptoBot</b>. После оплаты средства зачислятся на ваш баланс автоматически в течение 10 секунд."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🦋 Перейти к оплате ({amount_crypto} {CRYPTO_ASSET})", url=pay_url)],
        [InlineKeyboardButton(text="← Назад к выбору суммы", callback_data="m:topup_cb")],
    ])
    await switch_screen(callback.message, text, kb)


@dp.callback_query(F.data.startswith("sub:plan:"))
async def cb_select_plan(callback: types.CallbackQuery):
    plan_code = callback.data.split(":")[2]
    text, kb = await render_plan_checkout(callback.from_user, plan_code)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("cb:pay:"))
async def cb_cryptobot_pay(callback: types.CallbackQuery):
    plan_code = callback.data.split(":")[2]
    if plan_code not in SUB_PLANS:
        await callback.answer("Тариф не найден")
        return

    if not CRYPTOBOT_TOKEN or "ВАШ_API_ТОКЕН" in CRYPTOBOT_TOKEN:
        await callback.answer("Токен CryptoBot не настроен в файле bot.py", show_alert=True)
        return

    await callback.answer("Создаю сделку в CryptoBot...")
    pay_url, amount_crypto = await create_cryptobot_invoice(callback.from_user.id, plan_code)
    if not pay_url:
        await callback.answer("Ошибка создания счёта. Проверьте токен CryptoBot.", show_alert=True)
        return

    title, days, price_rub = SUB_PLANS[plan_code]
    text = (
        f"🦋 <b>Оплата через CryptoBot</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Тариф: <b>{html.escape(title)}</b>\n"
        f"▪ Сумма: <code>{amount_crypto} {CRYPTO_ASSET}</code> <i>(~{price_rub:.0f} ₽)</i>\n"
        f"▪ Время на оплату: <b>30 минут</b>"
        f"</blockquote>\n\n"
        f"Нажмите кнопку ниже — откроется сделка в <b>@CryptoBot</b>. После подтверждения перевода подписка активируется автоматически в течение 10 секунд."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🦋 Перейти к оплате ({amount_crypto} {CRYPTO_ASSET})", url=pay_url)],
        [InlineKeyboardButton(text="← Назад к выбору способа", callback_data=f"sub:plan:{plan_code}")],
    ])
    await switch_screen(callback.message, text, kb)


@dp.callback_query(F.data.startswith("buy:pro:"))
async def cb_buy_pro(callback: types.CallbackQuery):
    plan_code = callback.data.split(":")[2]
    if plan_code not in SUB_PLANS:
        await callback.answer("Тариф не найден")
        return

    title, days, price = SUB_PLANS[plan_code]
    cfg = await get_user_profile(callback.from_user, inc_req=False)

    if cfg["balance"] < price:
        missing = price - cfg["balance"]
        await callback.answer(f"Недостаточно средств на балансе! Не хватает {missing:.0f} ₽", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE tg_users SET balance = balance - ? WHERE tg_id = ?",
            (price, callback.from_user.id)
        )
        await db.commit()

    await activate_pro_for_user(callback.from_user.id, plan_code)

    await callback.answer(f"✅ Активирован тариф «{title}»!", show_alert=True)
    text, kb = await render_sub_store(callback.from_user)
    await switch_screen(callback.message, text, kb)


# --- АДМИН-ОБРАБОТЧИКИ ---
@dp.callback_query(F.data == "m:adm")
async def cb_admin_panel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await state.clear()
    text, kb = await render_admin_panel()
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "adm:toggle_op")
async def cb_adm_toggle_op(callback: types.CallbackQuery):
    global CHECK_SUB_ENABLED
    if callback.from_user.id != ADMIN_ID:
        return
    CHECK_SUB_ENABLED = not CHECK_SUB_ENABLED
    status_msg = "включена" if CHECK_SUB_ENABLED else "отключена"
    await callback.answer(f"Обязательная подписка {status_msg}")
    text, kb = await render_admin_panel()
    await switch_screen(callback.message, text, kb)


@dp.callback_query(F.data == "adm:self_1000")
async def cb_adm_self_1000(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET balance = balance + 1000 WHERE tg_id = ?", (ADMIN_ID,))
        await db.commit()
    await callback.answer("✅ Начислено +1000 ₽ на ваш баланс")
    text, kb = await render_admin_panel()
    await switch_screen(callback.message, text, kb)


@dp.callback_query(F.data == "adm:self_pro")
async def cb_adm_self_pro(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cfg = await get_user_profile(callback.from_user, inc_req=False)
    base_dt = cfg["pro_until"] if (cfg["is_pro"] and cfg["pro_until"]) else datetime.now()
    new_pro = base_dt + timedelta(days=30)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET pro_until = ? WHERE tg_id = ?", (new_pro, ADMIN_ID))
        await db.commit()
    await callback.answer("✅ Вам выдан PRO на 30 дней")
    text, kb = await render_admin_panel()
    await switch_screen(callback.message, text, kb)


@dp.callback_query(F.data.startswith("sup:rep:"))
async def cb_admin_reply_support(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён")
        return
    target_uid = int(callback.data.split(":")[2])
    await state.update_data(reply_target_uid=target_uid)
    await state.set_state(DashboardInput.adm_reply_user)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Отмена", callback_data="del_msg")]
    ])
    await callback.message.answer(
        f"↩️ Введите ответ для пользователя <code>{target_uid}</code>:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(DashboardInput.adm_reply_user)
async def process_admin_reply(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return
    data = await state.get_data()
    target_uid = data.get("reply_target_uid")
    await state.clear()

    if not target_uid:
        return

    reply_text = (
        f"💬 <b>Ответ от поддержки:</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>{html.escape(message.text)}</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать ещё", callback_data="in:sup")],
        [InlineKeyboardButton(text="🏠 Открыть терминал", callback_data="m:main")],
    ])
    try:
        await bot.send_message(chat_id=target_uid, text=reply_text, reply_markup=kb, parse_mode="HTML")
        await message.answer(f"✅ Ответ доставлен пользователю <code>{target_uid}</code>.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось доставить сообщение: {e}")


# --- ОБРАБОТЧИКИ ТРЁХУРОВНЕВОЙ АНАЛИТИКИ ---
@dp.callback_query(F.data == "m:an")
async def cb_analytics_root(callback: types.CallbackQuery):
    text, kb = await render_analytics_categories(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("an:cat:"))
async def cb_analytics_cat(callback: types.CallbackQuery):
    cat_idx = int(callback.data.split(":")[2])
    text, kb = await render_analytics_segments(callback.from_user, cat_idx)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("an:seg:"))
async def cb_analytics_seg(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    cat_idx, seg_idx = int(parts[2]), int(parts[3])
    text, kb = await render_analytics_segment_detail(callback.from_user, cat_idx, seg_idx)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:bt")
async def cb_best_time_root(callback: types.CallbackQuery):
    text, kb = await render_best_time(callback.from_user, cat_idx=-1)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("m:bt:"))
async def cb_best_time_cat(callback: types.CallbackQuery):
    cat_idx = int(callback.data.split(":")[2])
    text, kb = await render_best_time(callback.from_user, cat_idx=cat_idx)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "ch:24h")
async def cb_send_24h_chart(callback: types.CallbackQuery):
    cfg = await get_user_profile(callback.from_user, inc_req=False)
    if not cfg["is_pro"]:
        text, kb = render_paywall_view("График спроса и конкуренции 24ч", cfg)
        await switch_screen(callback.message, text, kb)
        await callback.answer("Доступно в PRO")
        return

    await callback.answer("Строю график...")
    img_bytes = await build_24h_chart_image()
    if not img_bytes:
        await callback.message.answer("Недостаточно почасовых данных для построения графика.")
        return

    photo = BufferedInputFile(img_bytes, filename="funpay_24h_chart.png")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Закрыть график", callback_data="del_msg")]
    ])
    await callback.message.answer_photo(
        photo=photo,
        caption="📈 <b>Динамика спроса и конкуренции по часам суток</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("m:tp:"))
async def cb_top_sellers(callback: types.CallbackQuery):
    cat_idx = int(callback.data.split(":")[2])
    text, kb = await render_top_sellers(callback.from_user, cat_idx)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:sp")
async def cb_spy_list(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_spy_list(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("sp:view:"))
async def cb_spy_view(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[2])
    text, kb = await render_spy_detail(callback.from_user, idx)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("sp:desc:"))
async def cb_spy_lot_desc(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[2])
    cfg = await get_user_profile(callback.from_user, inc_req=True)
    if not cfg["is_pro"]:
        text, kb = render_paywall_view("Чтение описаний лотов конкурентов", cfg)
        await switch_screen(callback.message, text, kb)
        await callback.answer()
        return

    if idx >= len(cfg["tracked_sellers"]):
        await callback.answer("Продавец не найден")
        return

    nick = cfg["tracked_sellers"][idx]
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT category, title, price, url FROM last_snapshot
            WHERE LOWER(seller) = LOWER(?) ORDER BY price ASC LIMIT 1
        """, (nick,))
        lot_row = await cursor.fetchone()

    if not lot_row:
        await callback.answer("У продавца нет активных лотов")
        return

    await callback.answer("Загружаю карточку лота...")
    cat, title, price, url = lot_row
    details = await fetch_lot_details(url)

    text = (
        f"<b>КАРТОЧКА ЛОТА · {html.escape(nick)}</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"▪ Раздел: <b>{html.escape(cat)}</b> · Цена: <code>{price:.0f} ₽</code>\n"
        f"▪ Лот: <a href='{url}'>{html.escape(title)}</a>\n"
    )
    if details["params"]:
        text += f"▪ Параметры: <i>{html.escape(details['params'])}</i>\n"
    text += f"\n<b>Описание:</b>\n<blockquote>{html.escape(details['desc'])}</blockquote>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад в досье", callback_data=f"sp:view:{idx}")]
    ])
    await switch_screen(callback.message, text, kb)


@dp.callback_query(F.data.startswith("sp:del:"))
async def cb_spy_del(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[2])
    cfg = await get_user_profile(callback.from_user, inc_req=False)
    sellers = list(cfg["tracked_sellers"])
    if idx < len(sellers):
        removed = sellers.pop(idx)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tg_users SET tracked_sellers = ? WHERE tg_id = ?", (",".join(sellers), callback.from_user.id))
            await db.commit()
        await callback.answer(f"Продавец {removed} удалён из слежки")
    text, kb = await render_spy_list(callback.from_user)
    await switch_screen(callback.message, text, kb)


@dp.callback_query(F.data.startswith("m:lt:"))
async def cb_lots(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    cat_idx, page = int(parts[2]), int(parts[3])
    text, kb = await render_lots_showcase(callback.from_user, cat_idx, page)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:fl")
async def cb_filters(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_filters_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:ct")
async def cb_categories(callback: types.CallbackQuery):
    text, kb = await render_categories_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:ex")
async def cb_excludes(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_excludes_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:pr")
async def cb_profile(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_profile_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:ref")
async def cb_referral(callback: types.CallbackQuery):
    text, kb = await render_referral_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:priv")
async def cb_privacy(callback: types.CallbackQuery):
    text, kb = render_privacy_policy()
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "m:terms")
async def cb_terms(callback: types.CallbackQuery):
    text, kb = render_terms_of_use()
    await switch_screen(callback.message, text, kb)
    await callback.answer()


# --- ПЕРЕКЛЮЧАТЕЛИ И БЫСТРЫЕ ПРЕСЕТЫ ЦЕН ---
@dp.callback_query(F.data.startswith("tg:on:"))
async def cb_toggle_online(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    source_screen = parts[2]
    cat_idx = int(parts[3])

    cfg = await get_user_profile(callback.from_user, inc_req=False)
    new_val = 0 if cfg["only_online"] else 1

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET only_online = ? WHERE tg_id = ?", (new_val, callback.from_user.id))
        await db.commit()

    if source_screen == "lt":
        text, kb = await render_lots_showcase(callback.from_user, cat_idx, 0)
    else:
        text, kb = await render_filters_menu(callback.from_user)

    await switch_screen(callback.message, text, kb)
    await callback.answer("Режим «Только онлайн» обновлён")


@dp.callback_query(F.data.startswith("tg:au:"))
async def cb_toggle_auto(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    source_screen = parts[2]
    cat_idx = int(parts[3])

    cfg = await get_user_profile(callback.from_user, inc_req=False)
    new_val = 0 if cfg["only_auto"] else 1

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET only_auto = ? WHERE tg_id = ?", (new_val, callback.from_user.id))
        await db.commit()

    if source_screen == "lt":
        text, kb = await render_lots_showcase(callback.from_user, cat_idx, 0)
    else:
        text, kb = await render_filters_menu(callback.from_user)

    await switch_screen(callback.message, text, kb)
    await callback.answer("Фильтр «Только автовыдача» обновлён")


@dp.callback_query(F.data.startswith("pr:set:"))
async def cb_price_preset(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    min_p, max_p = float(parts[2]), float(parts[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE tg_users SET min_price = ?, max_price = ? WHERE tg_id = ?",
            (min_p, max_p, callback.from_user.id)
        )
        await db.commit()
    text, kb = await render_filters_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer(f"Диапазон: {min_p:.0f}–{max_p:.0f} ₽")


@dp.callback_query(F.data.startswith("tg:cat:"))
async def cb_toggle_cat(callback: types.CallbackQuery):
    cat_idx = int(callback.data.split(":")[2])
    all_cats = list(TARGET_URLS.keys())
    if cat_idx >= len(all_cats):
        await callback.answer()
        return

    target_cat = all_cats[cat_idx]
    cfg = await get_user_profile(callback.from_user, inc_req=False)
    active = list(cfg["enabled_cats"])

    if target_cat in active:
        active.remove(target_cat)
    else:
        active.append(target_cat)

    save_str = ",".join(active) if active else "NONE"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET enabled_cats = ? WHERE tg_id = ?", (save_str, callback.from_user.id))
        await db.commit()

    text, kb = await render_categories_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "rst:ex")
async def cb_reset_excludes(callback: types.CallbackQuery):
    default_str = ",".join(DEFAULT_EXCLUDES)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET custom_excludes = ? WHERE tg_id = ?", (default_str, callback.from_user.id))
        await db.commit()
    text, kb = await render_excludes_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)
    await callback.answer("Стоп-слова сброшены")


# --- ПРЕСЕТЫ В ПРОФИЛЕ ---
@dp.callback_query(F.data.startswith("ps:add:"))
async def cb_quick_add_preset(callback: types.CallbackQuery):
    new_tag = callback.data.split(":", 2)[2].strip().lower()
    cfg = await get_user_profile(callback.from_user, inc_req=False)
    tags = list(cfg["saved_tags"])
    if new_tag and new_tag not in tags and len(tags) < 6:
        tags.append(new_tag)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tg_users SET saved_tags = ? WHERE tg_id = ?", (",".join(tags), callback.from_user.id))
            await db.commit()
        await callback.answer(f"Пресет «{new_tag}» сохранён!")
    else:
        await callback.answer("Пресет уже есть или достигнут лимит (6)")


@dp.callback_query(F.data.startswith("ps:run:"))
async def cb_run_preset(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[2])
    cfg = await get_user_profile(callback.from_user, inc_req=True)
    if idx >= len(cfg["saved_tags"]):
        await callback.answer("Пресет не найден")
        return

    tag = cfg["saved_tags"][idx]
    text, kb = await render_lots_showcase(callback.from_user, cat_idx=0, page=0, search_query=tag)
    kb.inline_keyboard[-1] = [InlineKeyboardButton(text="← Вернуться в профиль", callback_data="m:pr")]
    await switch_screen(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("ps:del:"))
async def cb_del_preset(callback: types.CallbackQuery):
    idx = int(callback.data.split(":")[2])
    cfg = await get_user_profile(callback.from_user, inc_req=False)
    tags = list(cfg["saved_tags"])
    if idx < len(tags):
        removed = tags.pop(idx)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tg_users SET saved_tags = ? WHERE tg_id = ?", (",".join(tags), callback.from_user.id))
            await db.commit()
        await callback.answer(f"Пресет «{removed}» удалён")
    text, kb = await render_profile_menu(callback.from_user)
    await switch_screen(callback.message, text, kb)


# --- ИНТЕРАКТИВНЫЙ ВВОД ЗНАЧЕНИЙ (С УДАЛЕНИЕМ СТАРОГО ЭКРАНА) ---
@dp.callback_query(F.data.startswith("in:"))
async def cb_start_input(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "spy":
        cfg = await get_user_profile(callback.from_user, inc_req=False)
        if not cfg["is_pro"]:
            text, kb = render_paywall_view("Слежка за конкурентами", cfg)
            await switch_screen(callback.message, text, kb)
            await callback.answer("Доступно в PRO")
            return

    if action in ("adm_bal", "adm_pro", "adm_bc") and callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён")
        return

    prompts = {
        "min": (DashboardInput.waiting_min_price, "💰 Введите новую <b>минимальную цену</b> в рублях (например <code>300</code>):", "m:fl"),
        "max": (DashboardInput.waiting_max_price, "💰 Введите новую <b>максимальную цену</b> в рублях (например <code>5000</code>):", "m:fl"),
        "rev": (DashboardInput.waiting_min_reviews, "⭐ Введите <b>минимальное количество отзывов</b> у продавца (например <code>50</code>):", "m:fl"),
        "tag": (DashboardInput.waiting_new_preset, "🔖 Отправьте <b>ключевое слово</b> для нового пресета (например: <code>ultra</code>):", "m:pr"),
        "srch": (DashboardInput.waiting_search_query, "🔎 Введите <b>слово для поиска</b> по всему каталогу (например: <code>veo</code>):", "m:main"),
        "ex": (DashboardInput.waiting_stop_word, "🚫 Введите новое <b>стоп-слово</b> для исключения (например: <code>общий</code>):", "m:ex"),
        "spy": (DashboardInput.waiting_seller_nick, "🕵️ Введите точный <b>никнейм продавца</b> на FunPay:", "m:sp"),
        "topup": (DashboardInput.waiting_topup_amount, "💳 Введите <b>сумму пополнения баланса</b> в рублях (от <code>50</code> ₽):", "m:topup_cb"),
        "sup": (DashboardInput.waiting_support_msg, "💬 <b>Обращение в поддержку</b>\n\nНапишите ваш вопрос одним сообщением — оно сразу уйдёт администратору:", "m:main"),
        "adm_bal": (DashboardInput.adm_topup_data, "💰 Отправьте <b>Telegram_ID и сумму</b> через пробел.\nПример: <code>123456789 1000</code>:", "m:adm"),
        "adm_pro": (DashboardInput.adm_grant_pro_data, "💎 Отправьте <b>Telegram_ID и количество дней PRO</b> через пробел.\nПример: <code>123456789 30</code> (<code>0</code> — снять PRO):", "m:adm"),
        "adm_bc": (DashboardInput.adm_broadcast_msg, "📢 <b>Массовая рассылка всем пользователям</b>\n\nОтправьте текст сообщения, которое получат все пользователи бота:", "m:adm"),
    }

    target_state, prompt_text, back_cb = prompts[action]
    await state.set_state(target_state)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✕ Отмена", callback_data=back_cb)]])
    new_msg_id = await switch_screen(callback.message, prompt_text, kb)
    await state.update_data(dash_msg_id=new_msg_id)
    await callback.answer()


async def finalize_fsm_screen(message: types.Message, state: FSMContext, render_coro):
    data = await state.get_data()
    dash_msg_id = data.get("dash_msg_id")
    await state.clear()

    try:
        await message.delete()
    except Exception:
        pass

    if dash_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=dash_msg_id)
        except Exception:
            pass

    text, kb = await render_coro
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(DashboardInput.waiting_support_msg)
async def process_support_msg(message: types.Message, state: FSMContext):
    if not message.text:
        return

    user = message.from_user
    u_name = f"@{html.escape(user.username)}" if user.username else "без юзернейма"
    admin_alert = (
        f"📩 <b>Новое обращение в поддержку</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"▪ От: <b>{html.escape(user.full_name)}</b> ({u_name})\n"
        f"▪ ID: <code>{user.id}</code>\n\n"
        f"<blockquote>{html.escape(message.text)}</blockquote>"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить пользователю", callback_data=f"sup:rep:{user.id}")]
    ])

    try:
        await bot.send_message(chat_id=ADMIN_ID, text=admin_alert, reply_markup=admin_kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка отправки в поддержку: {e}")

    data = await state.get_data()
    dash_msg_id = data.get("dash_msg_id")
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    if dash_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=dash_msg_id)
        except Exception:
            pass

    confirm_text = (
        f"✅ <b>Сообщение отправлено администратору!</b>\n\n"
        f"Ожидайте ответа прямо в этом чате."
    )
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Вернуться в терминал", callback_data="m:main")]
    ])
    await message.answer(confirm_text, reply_markup=confirm_kb, parse_mode="HTML")


@dp.message(DashboardInput.adm_broadcast_msg)
async def process_adm_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return

    broadcast_text = message.text
    data = await state.get_data()
    dash_msg_id = data.get("dash_msg_id")
    await state.clear()

    try:
        await message.delete()
    except Exception:
        pass
    if dash_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=dash_msg_id)
        except Exception:
            pass

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT tg_id FROM tg_users")
        all_uids = [r[0] for r in await cursor.fetchall()]

    sent_ok = 0
    sent_fail = 0
    formatted_msg = (
        f"📢 <b>Уведомление от администрации</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"{broadcast_text}"
    )
    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Открыть терминал", callback_data="m:main")]
    ])

    for uid in all_uids:
        try:
            await bot.send_message(chat_id=uid, text=formatted_msg, reply_markup=user_kb, parse_mode="HTML")
            sent_ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            sent_fail += 1

    report_text = (
        f"📢 <b>Рассылка завершена</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Доставлено: <b>{sent_ok}</b>\n"
        f"▪ Не доставлено (заблокировали бота): <b>{sent_fail}</b>"
        f"</blockquote>"
    )
    report_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← В админ-панель", callback_data="m:adm")]
    ])
    await message.answer(report_text, reply_markup=report_kb, parse_mode="HTML")


@dp.message(DashboardInput.adm_topup_data)
async def process_adm_topup(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return
    try:
        parts = message.text.strip().split()
        target_id = int(parts[0])
        amount = float(parts[1].replace(",", "."))
    except Exception:
        await message.delete()
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT tg_id FROM tg_users WHERE tg_id = ?", (target_id,))
        if not await cursor.fetchone():
            await db.execute("""
                INSERT INTO tg_users (tg_id, username, full_name, joined_at, balance)
                VALUES (?, '', 'User', ?, ?)
            """, (target_id, datetime.now(), amount))
        else:
            await db.execute("UPDATE tg_users SET balance = MAX(0, balance + ?) WHERE tg_id = ?", (amount, target_id))
        await db.commit()

    await finalize_fsm_screen(message, state, render_admin_panel())


@dp.message(DashboardInput.adm_grant_pro_data)
async def process_adm_grant_pro(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return
    try:
        parts = message.text.strip().split()
        target_id = int(parts[0])
        days = int(parts[1])
    except Exception:
        await message.delete()
        return

    new_pro_val = None if days <= 0 else (datetime.now() + timedelta(days=days))
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT tg_id FROM tg_users WHERE tg_id = ?", (target_id,))
        if not await cursor.fetchone():
            await db.execute("""
                INSERT INTO tg_users (tg_id, username, full_name, joined_at, pro_until)
                VALUES (?, '', 'User', ?, ?)
            """, (target_id, datetime.now(), new_pro_val))
        else:
            await db.execute("UPDATE tg_users SET pro_until = ? WHERE tg_id = ?", (new_pro_val, target_id))
        await db.commit()

    await finalize_fsm_screen(message, state, render_admin_panel())


@dp.message(DashboardInput.waiting_topup_amount)
async def process_topup_amount(message: types.Message, state: FSMContext):
    try:
        price_rub = float((message.text or "").replace(",", ".").strip())
        if price_rub < 50:
            raise ValueError
    except Exception:
        await message.delete()
        return

    data = await state.get_data()
    dash_msg_id = data.get("dash_msg_id")
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    if dash_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=dash_msg_id)
        except Exception:
            pass

    pay_url, amount_crypto = await create_cryptobot_invoice(message.from_user.id, f"topup:{price_rub:.0f}")
    if not pay_url:
        text, kb = await render_topup_cryptobot_menu(message.from_user)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    text = (
        f"🦋 <b>Пополнение баланса через CryptoBot</b>\n"
        f"<code>──────────────────────────</code>\n"
        f"<blockquote>"
        f"▪ Зачисление на баланс: <b>+{price_rub:.0f} ₽</b>\n"
        f"▪ К оплате: <code>{amount_crypto} {CRYPTO_ASSET}</code>\n"
        f"▪ Время на оплату: <b>30 минут</b>"
        f"</blockquote>\n\n"
        f"Нажмите кнопку ниже — откроется счёт в <b>@CryptoBot</b>. После оплаты средства зачислятся на ваш баланс автоматически в течение 10 секунд."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🦋 Перейти к оплате ({amount_crypto} {CRYPTO_ASSET})", url=pay_url)],
        [InlineKeyboardButton(text="← Назад к выбору суммы", callback_data="m:topup_cb")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(DashboardInput.waiting_min_price)
async def process_min_price(message: types.Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", ".").strip())
        if val < 0:
            raise ValueError
    except Exception:
        await message.delete()
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET min_price = ? WHERE tg_id = ?", (val, message.from_user.id))
        await db.commit()
    await finalize_fsm_screen(message, state, render_filters_menu(message.from_user))


@dp.message(DashboardInput.waiting_max_price)
async def process_max_price(message: types.Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", ".").strip())
        if val <= 0:
            raise ValueError
    except Exception:
        await message.delete()
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET max_price = ? WHERE tg_id = ?", (val, message.from_user.id))
        await db.commit()
    await finalize_fsm_screen(message, state, render_filters_menu(message.from_user))


@dp.message(DashboardInput.waiting_min_reviews)
async def process_min_reviews(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
    except Exception:
        await message.delete()
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tg_users SET min_reviews = ? WHERE tg_id = ?", (val, message.from_user.id))
        await db.commit()
    await finalize_fsm_screen(message, state, render_filters_menu(message.from_user))


@dp.message(DashboardInput.waiting_new_preset)
async def process_new_preset(message: types.Message, state: FSMContext):
    if not message.text:
        return
    new_tag = message.text.strip().lower().replace(",", "")[:25]
    cfg = await get_user_profile(message.from_user, inc_req=False)
    tags = list(cfg["saved_tags"])
    if new_tag and new_tag not in tags and len(tags) < 6:
        tags.append(new_tag)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tg_users SET saved_tags = ? WHERE tg_id = ?", (",".join(tags), message.from_user.id))
            await db.commit()
    await finalize_fsm_screen(message, state, render_profile_menu(message.from_user))


@dp.message(DashboardInput.waiting_stop_word)
async def process_stop_word(message: types.Message, state: FSMContext):
    if not message.text:
        return
    word = message.text.strip().lower().replace(",", "")[:25]
    cfg = await get_user_profile(message.from_user, inc_req=False)
    ex = list(cfg["excludes"])
    if word and word not in ex:
        ex.append(word)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tg_users SET custom_excludes = ? WHERE tg_id = ?", (",".join(ex), message.from_user.id))
            await db.commit()
    await finalize_fsm_screen(message, state, render_excludes_menu(message.from_user))


@dp.message(DashboardInput.waiting_seller_nick)
async def process_seller_nick(message: types.Message, state: FSMContext):
    if not message.text:
        return
    nick = message.text.strip().replace(",", "")[:35]
    cfg = await get_user_profile(message.from_user, inc_req=False)
    sellers = list(cfg["tracked_sellers"])
    if nick and nick.lower() not in [s.lower() for s in sellers] and len(sellers) < 8:
        sellers.append(nick)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tg_users SET tracked_sellers = ? WHERE tg_id = ?", (",".join(sellers), message.from_user.id))
            await db.commit()
    await finalize_fsm_screen(message, state, render_spy_list(message.from_user))


@dp.message(DashboardInput.waiting_search_query)
async def process_search_query(message: types.Message, state: FSMContext):
    if not message.text:
        return
    query = message.text.strip()[:40]
    await finalize_fsm_screen(message, state, render_lots_showcase(message.from_user, cat_idx=0, page=0, search_query=query))


# --- ЗАПУСК ---
async def main():
    global BOT_USERNAME, ADMIN_USERNAME
    logger.info("🚀 Запускаю FunPay AI Terminal...")

    me = await connect_telegram_with_fallback()
    BOT_USERNAME = me.username
    logger.info(f"🤖 Подключено: @{me.username} (ID: {me.id})")

    if not ADMIN_USERNAME:
        try:
            adm_chat = await bot.get_chat(ADMIN_ID)
            if adm_chat.username:
                ADMIN_USERNAME = adm_chat.username
                logger.info(f"👑 Контакт администратора для оплаты Звездами: @{ADMIN_USERNAME}")
        except Exception:
            pass

    try:
        await bot.set_my_commands([
            types.BotCommand(command="start", description="⚡ Открыть терминал"),
        ])
    except Exception:
        pass

    await init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(monitor_job, "interval", minutes=4)
    scheduler.add_job(check_cryptobot_payments_job, "interval", seconds=10)
    scheduler.start()

    asyncio.create_task(monitor_job())
    logger.info("👂 Готов к работе. Отправьте /start в Telegram.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен.")