from __future__ import annotations

import asyncio
import hashlib
from html import escape as html_escape
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import uvicorn

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
CATALOG_PATH = ROOT / "menu_catalog.json"
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data" if Path("/app/data").exists() else str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "restaurant.db")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()}
PORT = int(os.getenv("PORT", "3000"))
RESTAURANT_TZ = ZoneInfo("Europe/Moscow")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("restaurant-assistant")

bot_app: Application | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    return con


def init_db() -> None:
    with db_connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'waiter',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(telegram_id),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_shifts_employee_status ON shifts(employee_id, status);
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS menu_items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            section TEXT NOT NULL,
            composition TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            extra_info TEXT NOT NULL DEFAULT '',
            wow TEXT NOT NULL DEFAULT '',
            speech TEXT NOT NULL DEFAULT '',
            image TEXT,
            price REAL,
            is_active INTEGER NOT NULL DEFAULT 1,
            stock_status TEXT NOT NULL DEFAULT 'available',
            stop_reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT,
            updated_by INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_menu_section_category ON menu_items(section, category, name);
        CREATE INDEX IF NOT EXISTS idx_menu_stock ON menu_items(stock_status);
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            item_name TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            employee_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_item_created ON audit_log(item_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_number TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES employees(telegram_id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            comment TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_orders_owner_status ON orders(created_by, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL REFERENCES menu_items(id),
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(order_id, item_id, note)
        );
        CREATE INDEX IF NOT EXISTS idx_order_lines_order ON order_lines(order_id);
        """)

        # Mark old catalogue entries inactive before importing the current source. Existing
        # orders/audit history stays intact, while removed menu entries disappear from search.
        columns = {r[1] for r in con.execute('PRAGMA table_info(menu_items)').fetchall()}
        if 'is_active' not in columns:
            con.execute('ALTER TABLE menu_items ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1')
        employee_columns = {r[1] for r in con.execute('PRAGMA table_info(employees)').fetchall()}
        if 'display_name' not in employee_columns:
            con.execute("ALTER TABLE employees ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        con.execute('UPDATE menu_items SET is_active=0')
        if CATALOG_PATH.exists():
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            for item in catalog.get("items", []):
                con.execute("""
                    INSERT INTO menu_items
                    (id,name,category,section,composition,description,extra_info,wow,speech,image,price,is_active,stock_status,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,1,'available',?)
                    ON CONFLICT(id) DO UPDATE SET
                      name=excluded.name, category=excluded.category, section=excluded.section,
                      composition=excluded.composition, description=excluded.description,
                      extra_info=excluded.extra_info, wow=excluded.wow, speech=excluded.speech,
                      image=excluded.image, price=COALESCE(excluded.price, menu_items.price), is_active=1
                """, (
                    item["id"], item["name"], item["category"], item["section"],
                    item.get("composition", ""), item.get("description", ""), item.get("extra_info", ""),
                    item.get("wow", ""), item.get("speech", ""), item.get("image"), item.get("price"), now_iso()
                ))
        con.commit()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def get_employee(telegram_id: int) -> dict | None:
    with db_connect() as con:
        return row_dict(con.execute("SELECT * FROM employees WHERE telegram_id=?", (telegram_id,)).fetchone())


def get_active_shift(telegram_id: int) -> dict | None:
    with db_connect() as con:
        return row_dict(con.execute("SELECT * FROM shifts WHERE employee_id=? AND status='active' ORDER BY id DESC LIMIT 1", (telegram_id,)).fetchone())


def validate_webapp_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram Mini App initData per Telegram's HMAC-SHA-256 procedure."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        supplied_hash = data.pop("hash", None)
        if not supplied_hash:
            return None
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        computed = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, supplied_hash):
            return None
        auth_date = int(data.get("auth_date", "0"))
        if auth_date <= 0 or abs(int(time.time()) - auth_date) > 60 * 60 * 24:
            return None
        user = json.loads(data.get("user", "{}"))
        telegram_id = int(user["id"])
        return {"id": telegram_id, "username": user.get("username", ""),
                "first_name": user.get("first_name", ""), "last_name": user.get("last_name", "")}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def is_admin_id(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def is_command_admin(telegram_id: int) -> bool:
    if is_admin_id(telegram_id):
        return True
    employee = get_employee(telegram_id)
    return bool(employee and employee.get('status') == 'approved' and employee.get('role') in ('admin','manager'))


def get_admin_ids() -> set[int]:
    ids = set(ADMIN_IDS)
    with db_connect() as con:
        rows = con.execute("SELECT telegram_id FROM employees WHERE status='approved' AND role IN ('admin','manager')").fetchall()
    ids.update(int(r[0]) for r in rows)
    return ids


def add_or_update_employee(tg_user: dict) -> dict:
    """Create/update a Telegram employee profile without assuming optional fields exist.

    Telegram users are not required to set a last name or username. SQLite columns
    with NOT NULL constraints must receive strings rather than None.
    """
    tid = int(tg_user["id"])
    username = str(tg_user.get("username") or "").strip()
    first_name = str(tg_user.get("first_name") or "").strip()
    last_name = str(tg_user.get("last_name") or "").strip()
    is_admin = is_admin_id(tid)
    with db_connect() as con:
        existing = con.execute("SELECT * FROM employees WHERE telegram_id=?", (tid,)).fetchone()
        if existing is None:
            con.execute("""INSERT INTO employees (telegram_id,username,first_name,last_name,display_name,role,status,created_at,approved_at)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                        (tid, username, first_name, last_name, "",
                         "admin" if is_admin else "waiter", "approved" if is_admin else "pending", now_iso(), now_iso() if is_admin else None))
        else:
            con.execute("UPDATE employees SET username=?, first_name=?, last_name=? WHERE telegram_id=?",
                        (username, first_name, last_name, tid))
            if is_admin:
                con.execute("UPDATE employees SET role='admin',status='approved',approved_at=COALESCE(approved_at,?) WHERE telegram_id=?", (now_iso(), tid))
            elif existing["status"] == "rejected":
                con.execute("UPDATE employees SET status='pending' WHERE telegram_id=?", (tid,))
        con.commit()
    return get_employee(tid) or {}


def require_approved(tid: int) -> dict:
    employee = get_employee(tid)
    if not employee or employee.get("status") != "approved":
        raise HTTPException(status_code=403, detail="approval_pending")
    return employee


def require_active_shift(tid: int) -> tuple[dict, dict]:
    employee = require_approved(tid)
    shift = get_active_shift(tid)
    if not shift:
        raise HTTPException(status_code=409, detail="no_active_shift")
    return employee, shift


async def current_employee(request: Request) -> dict:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_webapp_init_data(init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(status_code=401, detail="telegram_auth_required")
    employee = require_approved(user["id"])
    return employee


def can_access_order(employee: dict, order: sqlite3.Row | dict | None) -> bool:
    if not order: return False
    return int(order["created_by"]) == int(employee["telegram_id"]) or employee["role"] in ("manager", "admin")


def order_payload(con: sqlite3.Connection, order_id: int) -> dict | None:
    order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order: return None
    lines = con.execute("SELECT l.*, m.category, m.image, m.price, m.stock_status FROM order_lines l JOIN menu_items m ON m.id=l.item_id WHERE l.order_id=? ORDER BY l.id", (order_id,)).fetchall()
    result = dict(order)
    result["lines"] = [dict(l) for l in lines]
    result["total"] = None if any(l["price"] is None for l in lines) else sum((l["price"] or 0) * l["quantity"] for l in lines)
    result["items_count"] = sum(l["quantity"] for l in lines)
    return result


# --- Telegram bot commands ---
def role_label(role: str) -> str:
    return {"admin":"Администратор", "manager":"Управляющий", "waiter":"Официант", "trainee":"Стажёр"}.get(role, role)


def employee_label(employee: dict | sqlite3.Row | None) -> str:
    if not employee: return "Сотрудник"
    display = str(employee.get("display_name", "") if isinstance(employee, dict) else employee["display_name"] or "").strip() if (isinstance(employee, dict) or "display_name" in employee.keys()) else ""
    if display: return display
    first = employee.get("first_name", "") if isinstance(employee, dict) else employee["first_name"]
    last = employee.get("last_name", "") if isinstance(employee, dict) else employee["last_name"]
    return " ".join(x for x in (first or "", last or "") if x).strip() or "Сотрудник"


def main_keyboard(employee: dict) -> InlineKeyboardMarkup:
    active = get_active_shift(int(employee["telegram_id"])) if employee.get("telegram_id") else None
    rows = [
        [InlineKeyboardButton("🔎 Найти блюдо или напиток", callback_data="menu:search"), InlineKeyboardButton("🚫 Стоп-лист", callback_data="menu:stops")],
        [InlineKeyboardButton("🔴 Закрыть смену" if active else "🟢 Начать смену", callback_data="shift:finish" if active else "shift:start")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="menu:profile"), InlineKeyboardButton("📱 Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_URL))] if WEBAPP_URL else [InlineKeyboardButton("👤 Мой профиль", callback_data="menu:profile")],
    ]
    if employee.get("role") in ("admin", "manager"):
        rows.append([InlineKeyboardButton("👥 Сотрудники", callback_data="admin:employees"), InlineKeyboardButton("🕘 История смен", callback_data="admin:shifts")])
    return InlineKeyboardMarkup(rows)



async def edit_or_send(message, text: str, **kwargs):
    """Keep one interactive Telegram message whenever Telegram allows editing it."""
    try:
        await message.edit_text(text, **kwargs)
    except Exception:
        try: await message.reply_text(text, **kwargs)
        except Exception: log.exception("Could not render Telegram screen")

async def notify_approved_staff(text: str, exclude: set[int] | None = None) -> None:
    if not bot_app: return
    exclude = exclude or set()
    with db_connect() as con:
        ids = [int(r[0]) for r in con.execute("SELECT telegram_id FROM employees WHERE status='approved'").fetchall()]
    for tid in ids:
        if tid in exclude: continue
        try: await bot_app.bot.send_message(tid, text, disable_web_page_preview=True)
        except Exception as exc: log.info("Could not notify employee %s: %s", tid, exc)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message: return
    u = update.effective_user
    previous = get_employee(u.id)
    was_rejected = bool(previous and previous.get("status") == "rejected")
    employee = add_or_update_employee({"id":u.id,"username":u.username,"first_name":u.first_name,"last_name":u.last_name})
    if not str(employee.get("display_name") or "").strip():
        context.user_data["awaiting_display_name"] = True
        await update.effective_message.reply_text("Привет! Для регистрации напиши своё ФИО или имя, по которому тебя знают в ресторане. Например: Анна Иванова или просто Анна.")
        return
    if employee.get("status") != "approved":
        if was_rejected:
            for admin_id in get_admin_ids():
                try:
                    await context.bot.send_message(admin_id, f"🔁 Повторная заявка на доступ\nСотрудник: {employee_label(employee)}\nTelegram ID: {u.id}\nUsername: @{employee.get('username') or 'нет'}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Официант", callback_data=f"staff:approve:{u.id}:waiter"), InlineKeyboardButton("🧑‍🍳 Стажёр", callback_data=f"staff:approve:{u.id}:trainee")],[InlineKeyboardButton("🧑‍💼 Управляющий", callback_data=f"staff:approve:{u.id}:manager"), InlineKeyboardButton("🛡 Администратор", callback_data=f"staff:approve:{u.id}:admin")],[InlineKeyboardButton("❌ Отклонить", callback_data=f"staff:reject:{u.id}")]]))
                except Exception: log.exception("Could not notify admin about reapplication")
        await update.effective_message.reply_text(f"Заявка на доступ {'отправлена повторно' if was_rejected else 'уже ожидает подтверждения'}.\nИмя: {employee_label(employee)}\nTelegram ID: {u.id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Проверить статус", callback_data="registration:status")]]))
        return
    await update.effective_message.reply_text(f"🍽 <b>Помощник команды ресторана</b>\nПривет, {html_escape(employee_label(employee))}!\n\nВыбери действие:", parse_mode="HTML", reply_markup=main_keyboard(employee))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message: return
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if not text or text.startswith("/"): return
    if context.user_data.get("awaiting_display_name"):
        if len(text) < 2 or len(text) > 80:
            await update.effective_message.reply_text("Напиши ФИО или имя длиной от 2 до 80 символов."); return
        with db_connect() as con:
            con.execute("UPDATE employees SET display_name=? WHERE telegram_id=?", (text, uid)); con.commit()
        context.user_data.pop("awaiting_display_name", None)
        employee = get_employee(uid) or {}
        if employee.get("status") != "approved":
            for admin_id in get_admin_ids():
                try:
                    await context.bot.send_message(admin_id, f"🆕 Заявка на доступ\nСотрудник: {text}\nTelegram ID: {uid}\nUsername: @{employee.get('username') or 'нет'}", reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Официант", callback_data=f"staff:approve:{uid}:waiter"), InlineKeyboardButton("🧑‍🍳 Стажёр", callback_data=f"staff:approve:{uid}:trainee")],
                        [InlineKeyboardButton("🧑‍💼 Управляющий", callback_data=f"staff:approve:{uid}:manager"), InlineKeyboardButton("🛡 Администратор", callback_data=f"staff:approve:{uid}:admin")],
                        [InlineKeyboardButton("❌ Отклонить", callback_data=f"staff:reject:{uid}")]
                    ]))
                except Exception: log.exception("Could not notify manager about application")
            await update.effective_message.reply_text(f"Спасибо, {text}! Заявка отправлена управляющему/администратору. Доступ появится после одобрения.")
        else:
            await update.effective_message.reply_text(f"Профиль сохранён: {text}.", reply_markup=main_keyboard(employee))
        return
    employee = get_employee(uid)
    if not employee or employee.get("status") != "approved":
        await update.effective_message.reply_text("Сначала отправь /start и дождись подтверждения доступа."); return
    if context.user_data.pop("awaiting_menu_search", False):
        prompt_id = context.user_data.pop("search_prompt_id", None)
        try: await update.effective_message.delete()
        except Exception: pass
        if prompt_id:
            try:
                with db_connect() as con: rows = con.execute("SELECT id,name,category,section,stock_status,price FROM menu_items WHERE is_active=1 AND (name LIKE ? OR category LIKE ? OR composition LIKE ? OR description LIKE ? OR extra_info LIKE ?) ORDER BY CASE WHEN lower(name)=lower(?) THEN 0 WHEN lower(name) LIKE lower(?) THEN 1 ELSE 2 END, name LIMIT 40", (f"%{text}%",)*5 + (text, f"{text}%")).fetchall()
                if not rows:
                    await context.bot.edit_message_text(chat_id=uid, message_id=prompt_id, text=f"По запросу «{text}» ничего не найдено.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Новый поиск", callback_data="menu:search")],[InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")]])); return
                kb=[[InlineKeyboardButton(("🔴 " if r["stock_status"]=="stop" else "🟢 ")+r["name"][:48]+(f" · {int(r['price'])} ₽" if r["price"] is not None else ""), callback_data=f"item:view:{r['id']}")] for r in rows]
                kb += [[InlineKeyboardButton("◀️ Предыдущая",callback_data="search:page:0"),InlineKeyboardButton("Следующая ▶️",callback_data="search:page:1")],[InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")]] if len(rows)>=40 else [[InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")]]
                await context.bot.edit_message_text(chat_id=uid, message_id=prompt_id, text=f"🔎 Результаты поиска «{text}» — выбери позицию:", reply_markup=InlineKeyboardMarkup(kb))
                context.user_data["last_search_query"] = text
                return
            except Exception: log.exception("Could not replace search prompt with results")
    await search_menu_in_chat(update, context, text)


async def search_menu_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    with db_connect() as con:
        rows = con.execute("SELECT id,name,category,section,stock_status,price FROM menu_items WHERE is_active=1 AND (name LIKE ? OR category LIKE ? OR composition LIKE ? OR description LIKE ? OR extra_info LIKE ?) ORDER BY CASE WHEN lower(name)=lower(?) THEN 0 WHEN lower(name) LIKE lower(?) THEN 1 ELSE 2 END, name LIMIT 40", (f"%{query}%",)*5 + (query, f"{query}%")).fetchall()
    if not rows:
        await update.effective_message.reply_text(f"По запросу «{query}» ничего не нашёл. Попробуй часть названия или ингредиент.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Новый поиск", callback_data="menu:search")],[InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")]])); return
    kb = [[InlineKeyboardButton(("🔴 " if r["stock_status"] == "stop" else "🟢 ") + r["name"][:55] + (f" · {int(r['price'])} ₽" if r["price"] is not None else ""), callback_data=f"item:view:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton("🚫 Открыть стоп-лист", callback_data="menu:stops")])
    kb.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")])
    await update.effective_message.reply_text(f"🔎 Нашёл по запросу «{query}» — выбери позицию:", reply_markup=InlineKeyboardMarkup(kb))


async def bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q: return
    await q.answer()
    uid = q.from_user.id
    employee = get_employee(uid)
    action = q.data or ""
    if action == "noop": return
    if action == "registration:status":
        if employee and employee.get("status") == "approved": await edit_or_send(q.message, "Доступ подтверждён. Нажми /start.", reply_markup=main_keyboard(employee))
        else: await edit_or_send(q.message, "Заявка пока ожидает подтверждения.")
        return
    if not employee or employee.get("status") != "approved":
        await edit_or_send(q.message, "Доступ ещё не одобрен. Нажми /start и отправь ФИО или имя."); return
    if action == "menu:search":
        context.user_data["awaiting_menu_search"] = True
        context.user_data["search_prompt_id"] = q.message.message_id
        await edit_or_send(q.message, "🔎 <b>Найти блюдо или напиток</b>\nНапиши название или часть названия следующим сообщением — я заменю этот экран результатами поиска.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="menu:home")]]))
    elif action.startswith("search:page:"):
        query=context.user_data.get("last_search_query", "мохито")
        page=int(action.rsplit(':',1)[1])
        with db_connect() as con:
            rows=con.execute("SELECT id,name,category,section,stock_status,price FROM menu_items WHERE is_active=1 AND (name LIKE ? OR category LIKE ? OR composition LIKE ? OR description LIKE ? OR extra_info LIKE ?) ORDER BY CASE WHEN lower(name)=lower(?) THEN 0 WHEN lower(name) LIKE lower(?) THEN 1 ELSE 2 END, name LIMIT 40",(f"%{query}%",)*5+(query,f"{query}%")).fetchall()
        size=12; page=max(0,min(page,max(0,(len(rows)-1)//size)))
        kb=[[InlineKeyboardButton(("🔴 " if r['stock_status']=='stop' else "🟢 ")+r['name'][:48]+(f" · {int(r['price'])} ₽" if r['price'] is not None else ''),callback_data=f"item:view:{r['id']}")] for r in rows[page*size:(page+1)*size]]
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("◀️ Назад",callback_data=f"search:page:{page-1}"))
        if (page+1)*size<len(rows): nav.append(InlineKeyboardButton("Следующая ▶️",callback_data=f"search:page:{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")])
        await edit_or_send(q.message,f"🔎 Поиск «{html_escape(query)}» · страница {page+1}",reply_markup=InlineKeyboardMarkup(kb))
    elif action == "menu:stops" or action.startswith("stops:page:"):
        page=int(action.rsplit(':',1)[1]) if action.startswith('stops:page:') else 0
        with db_connect() as con: rows=con.execute("SELECT id,name,category,stop_reason,price FROM menu_items WHERE is_active=1 AND stock_status='stop' ORDER BY section,category,name").fetchall()
        if not rows: await edit_or_send(q.message, "✅ Стоп-лист пуст — все позиции доступны.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")]])); return
        size=12; page=max(0,min(page,(len(rows)-1)//size)); kb=[[InlineKeyboardButton("🔴 "+r['name'][:50],callback_data=f"item:view:{r['id']}")] for r in rows[page*size:(page+1)*size]]
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("◀️ Назад",callback_data=f"stops:page:{page-1}"))
        if (page+1)*size<len(rows): nav.append(InlineKeyboardButton("Далее ▶️",callback_data=f"stops:page:{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")])
        await edit_or_send(q.message, f"🚫 <b>Стоп-лист</b> · {len(rows)} позиций · страница {page+1}\nВыбери позицию:",reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML")
    elif action == "menu:profile":
        sh=get_active_shift(uid)
        await edit_or_send(q.message, f"👤 <b>{employee_label(employee)}</b>\nРоль: {role_label(employee['role'])}\nСтатус: {employee['status']}\nСмена: {'активна с '+sh['started_at'] if sh else 'не начата'}\nTelegram ID: {uid}",parse_mode="HTML",reply_markup=main_keyboard(employee))
    elif action in ("shift:start","shift:finish"):
        if action == "shift:start":
            if not get_active_shift(uid):
                with db_connect() as con: con.execute("INSERT INTO shifts(employee_id,started_at,status) VALUES (?,?, 'active')",(uid,now_iso())); con.commit()
                await notify_approved_staff(f"🟢 Смена началась\n{employee_label(employee)} вышел(ла) на смену.", exclude={uid})
            await edit_or_send(q.message, "Смена активна.",reply_markup=main_keyboard(employee))
        else:
            sh=get_active_shift(uid)
            if sh:
                with db_connect() as con: con.execute("UPDATE shifts SET status='closed',ended_at=? WHERE id=?",(now_iso(),sh['id'])); con.commit()
                await notify_approved_staff(f"🔴 Смена завершена\n{employee_label(employee)} завершил(а) смену.",exclude={uid})
            await edit_or_send(q.message, "Смена завершена.",reply_markup=main_keyboard(employee))
    elif action.startswith("item:view:"):
        item_id=action.split(":",2)[2]
        with db_connect() as con: item=con.execute("SELECT * FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
        if not item: await edit_or_send(q.message, "Позиция не найдена."); return
        text=f"<b>{html_escape(item['name'])}</b>\n{html_escape(item['section'])} · {html_escape(item['category'])}\nСтатус: {'🔴 В стоп-листе' if item['stock_status']=='stop' else '🟢 Доступно'}\nЦена: {str(int(item['price']))+' ₽' if item['price'] is not None else 'не указана'}"
        for title,col in [("Описание","description"),("Состав","composition"),("Аллергены / дополнительная информация","extra_info"),("Особенности подачи","wow"),("Как рассказать гостю","speech")]:
            if item[col]: text+=f"\n\n<b>{title}</b>\n{html_escape(item[col])}"
        if item['stock_status']=='stop': buttons=[[InlineKeyboardButton("✅ Вернуть в меню",callback_data=f"item:restore:{item_id}")]]
        else: buttons=[[InlineKeyboardButton("🚫 В стоп-лист",callback_data=f"item:stop:{item_id}")]]
        buttons.append([InlineKeyboardButton("↩️ Главное меню",callback_data="menu:home")])
        await edit_or_send(q.message, text[:4000],parse_mode="HTML",reply_markup=InlineKeyboardMarkup(buttons))
    elif action.startswith("item:stop:") or action.startswith("item:restore:"):
        if not get_active_shift(uid): await edit_or_send(q.message, "Сначала начни смену — только активный сотрудник может менять стоп-лист."); return
        verb,item_id=action.split(":",2)[1:]
        with db_connect() as con:
            item=con.execute("SELECT id,name,stock_status FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
            if not item: await edit_or_send(q.message, "Позиция не найдена."); return
            if verb=='stop' and item['stock_status']!='stop':
                con.execute("UPDATE menu_items SET stock_status='stop',stop_reason='Добавлено из чата бота',updated_at=?,updated_by=? WHERE id=?",(now_iso(),uid,item_id)); con.execute("INSERT INTO audit_log(item_id,item_name,action,reason,employee_id,created_at) VALUES (?,?,?,?,?,?)",(item_id,item['name'],'stop','Добавлено из чата бота',uid,now_iso())); changed=True
            elif verb=='restore' and item['stock_status']=='stop':
                con.execute("UPDATE menu_items SET stock_status='available',stop_reason='',updated_at=?,updated_by=? WHERE id=?",(now_iso(),uid,item_id)); con.execute("INSERT INTO audit_log(item_id,item_name,action,reason,employee_id,created_at) VALUES (?,?,?,?,?,?)",(item_id,item['name'],'restore','',uid,now_iso())); changed=True
            else: changed=False
            con.commit()
        if changed: await notify_approved_staff(("🚫 Добавлено в стоп-лист" if verb=='stop' else "✅ Возвращено в меню")+f"\nПозиция: {item['name']}\nСотрудник: {employee_label(employee)}",exclude={uid})
        await edit_or_send(q.message, ("Позиция добавлена в стоп-лист." if verb=='stop' else "Позиция возвращена в меню."),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть позицию",callback_data=f"item:view:{item_id}")],[InlineKeyboardButton("Главное меню",callback_data="menu:home")]]))
    elif action == "menu:home":
        try: await q.edit_message_text(f"🍽 <b>Помощник команды ресторана</b>\nПривет, {employee_label(employee)}!\nВыбери действие:",parse_mode="HTML",reply_markup=main_keyboard(employee))
        except Exception: await edit_or_send(q.message, f"🍽 Помощник команды ресторана\nПривет, {employee_label(employee)}!",reply_markup=main_keyboard(employee))
    elif action == "admin:home":
        if employee['role'] not in ('admin','manager'): await edit_or_send(q.message, "Недостаточно прав."); return
        await edit_or_send(q.message, "🛠 <b>Управление сотрудниками</b>",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Заявки",callback_data="admin:pending"),InlineKeyboardButton("👥 Сотрудники",callback_data="admin:employees")],[InlineKeyboardButton("🕘 Все смены",callback_data="admin:shifts")],[InlineKeyboardButton("⬅️ Главное меню",callback_data="menu:home")]]))
    elif action == "admin:pending" or action == "admin:employees" or action.startswith("admin:employees:page:") or action.startswith("admin:pending:page:"):
        if employee['role'] not in ('admin','manager'): await edit_or_send(q.message, "Недостаточно прав."); return
        status='pending' if action.startswith('admin:pending') or action.startswith('admin:pending:page:') else ('pending' if action.startswith('admin:pending') else 'approved')
        if action.startswith('admin:approved:page:'): status='approved'
        page=int(action.rsplit(':',1)[1]) if ':page:' in action else 0
        with db_connect() as con: rows=con.execute("SELECT * FROM employees WHERE status=? ORDER BY display_name,first_name",(status,)).fetchall()
        page_size=12; page=max(0,min(page,(max(0,len(rows)-1)//page_size)))
        kb=[]
        for r in rows[page*page_size:(page+1)*page_size]:
            label=f"⏳ {employee_label(r)}" if status=='pending' else f"{employee_label(r)} · {role_label(r['role'])}"
            kb.append([InlineKeyboardButton(label[:60],callback_data=f"staff:open:{r['telegram_id']}:{status}")])
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("◀️ Назад",callback_data=f"admin:{status}:page:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{max(1,(len(rows)+page_size-1)//page_size)}",callback_data="noop"))
        if (page+1)*page_size<len(rows): nav.append(InlineKeyboardButton("Вперёд ▶️",callback_data=f"admin:{status}:page:{page+1}"))
        if nav: kb.append(nav)
        kb.extend([[InlineKeyboardButton("⏳ Заявки",callback_data="admin:pending"),InlineKeyboardButton("👥 Сотрудники",callback_data="admin:employees")],[InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")]])
        txt=("⏳ Заявки на доступ" if status=='pending' else "👥 Сотрудники")+f"\nВсего: {len(rows)} · страница {page+1}\nВыбери человека:"
        try: await q.edit_message_text(txt,reply_markup=InlineKeyboardMarkup(kb))
        except Exception: await edit_or_send(q.message, txt,reply_markup=InlineKeyboardMarkup(kb))
    elif action.startswith("staff:open:"):
        if employee['role'] not in ('admin','manager'): await edit_or_send(q.message, "Недостаточно прав."); return
        _,_,target,status=action.split(":"); target=int(target)
        with db_connect() as con: r=con.execute("SELECT * FROM employees WHERE telegram_id=?",(target,)).fetchone()
        if not r: await q.answer("Сотрудник не найден",show_alert=True); return
        if status=='pending':
            kb=[[InlineKeyboardButton("Официант",callback_data=f"staff:approve:{target}:waiter"),InlineKeyboardButton("Стажёр",callback_data=f"staff:approve:{target}:trainee")],[InlineKeyboardButton("Управляющий",callback_data=f"staff:approve:{target}:manager"),InlineKeyboardButton("Администратор",callback_data=f"staff:approve:{target}:admin")],[InlineKeyboardButton("❌ Отклонить",callback_data=f"staff:reject:{target}")]]
        else:
            kb=[[InlineKeyboardButton("Официант",callback_data=f"staff:role:{target}:waiter"),InlineKeyboardButton("Стажёр",callback_data=f"staff:role:{target}:trainee")],[InlineKeyboardButton("Управляющий",callback_data=f"staff:role:{target}:manager"),InlineKeyboardButton("Администратор",callback_data=f"staff:role:{target}:admin")],[InlineKeyboardButton("🚫 Отключить доступ",callback_data=f"staff:reject:{target}")]]
        kb.append([InlineKeyboardButton("⬅️ К списку",callback_data="admin:employees" if status=='approved' else "admin:pending"),InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")])
        try: await q.edit_message_text(f"👤 {employee_label(r)}\nРоль: {role_label(r['role'])}\nСтатус: {r['status']}\nID: {target}\n@{r['username'] or 'нет username'}",reply_markup=InlineKeyboardMarkup(kb))
        except Exception: await edit_or_send(q.message, f"👤 {employee_label(r)} · {role_label(r['role'])}",reply_markup=InlineKeyboardMarkup(kb))
    elif action == "admin:shifts" or action.startswith("admin:shifts:staff:") or action.startswith("admin:shifts:page:"):
        if employee['role'] not in ('admin','manager'): await edit_or_send(q.message, "Недостаточно прав."); return
        staff_filter=None; page=0
        if action.startswith("admin:shifts:staff:"):
            staff_filter=int(action.rsplit(':',1)[1])
        elif action.startswith("admin:shifts:page:"):
            bits=action.split(':'); page=int(bits[-1]); staff_filter=int(bits[-2]) if bits[-2].isdigit() and len(bits)>4 else None
        with db_connect() as con:
            staff=con.execute("SELECT telegram_id,display_name,first_name,last_name FROM employees WHERE status='approved' ORDER BY display_name,first_name").fetchall()
            if staff_filter is None: rows=con.execute("SELECT s.*,e.display_name,e.first_name,e.last_name FROM shifts s JOIN employees e ON e.telegram_id=s.employee_id ORDER BY s.id DESC").fetchall()
            else: rows=con.execute("SELECT s.*,e.display_name,e.first_name,e.last_name FROM shifts s JOIN employees e ON e.telegram_id=s.employee_id WHERE s.employee_id=? ORDER BY s.id DESC",(staff_filter,)).fetchall()
        page_size=8; total=len(rows); page=max(0,min(page,(max(0,total-1)//page_size)))
        lines=["🕘 <b>История смен</b>","Фильтр: "+(next((employee_label(x) for x in staff if int(x['telegram_id'])==staff_filter),"Сотрудник") if staff_filter else "Все сотрудники")]
        for r in rows[page*page_size:(page+1)*page_size]: lines.append(f"• {html_escape(employee_label(r))} — {'🟢 активна' if r['status']=='active' else 'закрыта'}\n  {html_escape(r['started_at'])}"+(f" — {html_escape(r['ended_at'])}" if r['ended_at'] else ''))
        kb=[[InlineKeyboardButton("Все сотрудники",callback_data="admin:shifts")]]
        kb += [[InlineKeyboardButton(employee_label(x)[:55],callback_data=f"admin:shifts:staff:{x['telegram_id']}")] for x in staff[:12]] if staff_filter is None else [[InlineKeyboardButton("👥 Все сотрудники",callback_data="admin:shifts")]]
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("◀️ Назад",callback_data=f"admin:shifts:page:{staff_filter or 0}:{page-1}"))
        if (page+1)*page_size<total: nav.append(InlineKeyboardButton("Далее ▶️",callback_data=f"admin:shifts:page:{staff_filter or 0}:{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")])
        await edit_or_send(q.message,"\n".join(lines)[:4000],parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))
    elif action.startswith("staff:approve:") or action.startswith("staff:role:"):
        if employee['role'] not in ('admin','manager'): await edit_or_send(q.message, "Недостаточно прав."); return
        _,_,target,role=action.split(":")
        if role not in ('admin','manager','waiter','trainee'): return
        with db_connect() as con:
            con.execute("UPDATE employees SET role=?,status='approved',approved_at=COALESCE(approved_at,?),approved_by=? WHERE telegram_id=?",(role,now_iso(),uid,int(target))); con.commit()
        await edit_or_send(q.message, f"Сотрудник {target}: {role_label(role)}.")
        try: await context.bot.send_message(int(target),f"✅ Доступ одобрен. Твоя роль: {role_label(role)}. Напиши /start.",reply_markup=main_keyboard(get_employee(int(target)) or {}))
        except Exception: pass
    elif action.startswith("staff:reject:"):
        if employee['role'] not in ('admin','manager'): await edit_or_send(q.message, "Недостаточно прав."); return
        target=int(action.split(":")[-1])
        with db_connect() as con: con.execute("UPDATE employees SET status='rejected' WHERE telegram_id=?",(target,)); con.commit()
        await q.edit_message_text(f"Доступ сотруднику {target} отключён.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 К сотрудникам",callback_data="admin:employees")],[InlineKeyboardButton("🏠 Главное меню",callback_data="menu:home")]]))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_message:
        await update.effective_message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Команды помощника:\n"
        "/start — открыть помощника или запросить доступ\n"
        "/id — показать свой Telegram ID\n"
        "/help — помощь\n\n"
        "Команды менеджера:\n"
        "/pending — заявки на доступ\n"
        "/approve ID waiter|manager|admin — одобрить сотрудника\n"
        "/employees — список сотрудников"
    )
    if update.effective_message:
        await update.effective_message.reply_text(text)

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_command_admin(uid):
        await update.effective_message.reply_text("Эта команда доступна только администратору."); return
    with db_connect() as con:
        rows = con.execute("SELECT telegram_id,first_name,last_name,username,created_at FROM employees WHERE status='pending' ORDER BY created_at").fetchall()
    if not rows:
        await update.effective_message.reply_text("Новых заявок нет."); return
    lines = ["Заявки на доступ:"]
    for r in rows:
        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or "Без имени"
        lines.append(f"• {name} — ID {r['telegram_id']} (@{r['username'] or 'нет username'})\n  /approve {r['telegram_id']} waiter")
    await update.effective_message.reply_text("\n".join(lines))

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_command_admin(uid):
        await update.effective_message.reply_text("Эта команда доступна только администратору."); return
    if len(context.args) < 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Формат: /approve TELEGRAM_ID waiter|trainee|manager|admin"); return
    target = int(context.args[0]); role = context.args[1].lower() if len(context.args) > 1 else "waiter"
    if role not in ("waiter", "trainee", "manager", "admin"):
        await update.effective_message.reply_text("Роль должна быть waiter, manager или admin."); return
    with db_connect() as con:
        cur = con.execute("UPDATE employees SET role=?,status='approved',approved_at=?,approved_by=? WHERE telegram_id=?", (role, now_iso(), uid, target))
        con.commit()
    if cur.rowcount == 0:
        await update.effective_message.reply_text("Сотрудник не найден. Пусть он сначала нажмёт /start у бота."); return
    await update.effective_message.reply_text(f"Доступ выдан: {target}, роль {role}.")
    try: await context.bot.send_message(target, "Доступ к помощнику одобрен. Нажми /start, чтобы открыть приложение.")
    except Exception: log.info("Approved user %s but could not DM them", target)

async def cmd_employees(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_command_admin(uid):
        await update.effective_message.reply_text("Эта команда доступна только администратору."); return
    with db_connect() as con:
        rows = con.execute("SELECT telegram_id,first_name,last_name,role,status,username FROM employees ORDER BY status,first_name").fetchall()
    if not rows:
        await update.effective_message.reply_text("Сотрудников пока нет."); return
    lines=["Сотрудники:"]
    for r in rows:
        name=" ".join(x for x in (r["first_name"],r["last_name"]) if x) or "Без имени"
        lines.append(f"• {name} — {r['role']}, {r['status']} — ID {r['telegram_id']}")
    await update.effective_message.reply_text("\n".join(lines))


def build_bot() -> Application | None:
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is not set; HTTP/Mini App runs, Telegram polling is disabled.")
        return None
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("approve", cmd_approve))
    application.add_handler(CommandHandler("employees", cmd_employees))
    application.add_handler(CallbackQueryHandler(bot_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def shift_scheduled_end(started_at: str) -> datetime:
    dt=datetime.fromisoformat(started_at)
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    local=dt.astimezone(RESTAURANT_TZ)
    # Friday/Saturday shifts end at midnight; Sunday-Thursday at 22:00.
    if local.weekday() in (4,5):
        target_date=local.date()+timedelta(days=1)
        return datetime.combine(target_date, dt_time(0,0), tzinfo=RESTAURANT_TZ)
    return datetime.combine(local.date(), dt_time(22,0), tzinfo=RESTAURANT_TZ)


async def maintenance_loop() -> None:
    while True:
        try:
            now_local=datetime.now(RESTAURANT_TZ)
            with db_connect() as con:
                active=con.execute("SELECT s.*,e.display_name,e.first_name,e.last_name,e.telegram_id FROM shifts s JOIN employees e ON e.telegram_id=s.employee_id WHERE s.status='active'").fetchall()
                to_close=[r for r in active if now_local >= shift_scheduled_end(r['started_at'])]
                for r in to_close:
                    con.execute("UPDATE shifts SET status='closed',ended_at=? WHERE id=? AND status='active'",(now_iso(),r['id']))
                con.commit()
            for r in to_close:
                await notify_approved_staff(f"⏰ Смена автоматически закрыта по расписанию\nСотрудник: {employee_label(r)}", exclude={int(r['telegram_id'])})
            # Send yesterday's end-of-day stop list once per local date, at/after 10:00.
            today=now_local.date().isoformat()
            if now_local.hour >= 10:
                with db_connect() as con:
                    sent=con.execute("SELECT value FROM app_settings WHERE key='last_daily_stop_report'").fetchone()
                if not sent or sent['value'] != today:
                    yesterday=(now_local.date()-timedelta(days=1))
                    cutoff=datetime.combine(yesterday+timedelta(days=1),dt_time(0,0),tzinfo=RESTAURANT_TZ).astimezone(timezone.utc).isoformat(timespec='seconds')
                    with db_connect() as con:
                        rows=con.execute("""SELECT m.name,m.category,a.reason,a.created_at,a.employee_id
                            FROM menu_items m JOIN audit_log a ON a.item_id=m.id
                            WHERE a.id=(SELECT a2.id FROM audit_log a2 WHERE a2.item_id=m.id AND a2.created_at < ? ORDER BY a2.id DESC LIMIT 1)
                            AND a.action='stop' ORDER BY m.section,m.category,m.name""",(cutoff,)).fetchall()
                        con.execute("INSERT INTO app_settings(key,value) VALUES('last_daily_stop_report',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(today,)); con.commit()
                    lines=[f"📋 <b>Стоп-лист на конец {yesterday.strftime('%d.%m.%Y')}</b>"]
                    if rows:
                        for r in rows: lines.append(f"• {html_escape(r['name'])}"+(f" — {html_escape(r['reason'])}" if r['reason'] else ""))
                    else: lines.append("На конец дня стоп-лист был пуст.")
                    text="\n".join(lines)
                    for tid in get_admin_ids():
                        try:
                            if bot_app: await bot_app.bot.send_message(tid,text[:4000],parse_mode='HTML')
                        except Exception: log.exception("Could not send daily stop report to %s",tid)
        except Exception:
            log.exception("Maintenance task failed")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    init_db()
    maintenance_task = asyncio.create_task(maintenance_loop())
    bot_app = build_bot()
    if bot_app:
        try:
            await bot_app.initialize()
            await bot_app.start()
            if bot_app.updater:
                await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
            try:
                await bot_app.bot.set_my_commands([
                    BotCommand("start", "Открыть помощника"), BotCommand("id", "Мой Telegram ID"),
                    BotCommand("help", "Помощь"), BotCommand("pending", "Заявки на доступ"),
                    BotCommand("approve", "Одобрить сотрудника"), BotCommand("employees", "Сотрудники")
                ])
            except Exception: log.exception("Could not set bot commands")
            log.info("Telegram bot polling started")
        except Exception:
            log.exception("Telegram bot failed to start; HTTP server will remain available")
            bot_app = None
    yield
    maintenance_task.cancel()
    try: await maintenance_task
    except asyncio.CancelledError: pass
    if bot_app:
        try:
            if bot_app.updater and bot_app.updater.running: await bot_app.updater.stop()
            if bot_app.running: await bot_app.stop()
            await bot_app.shutdown()
        except Exception: log.exception("Error while shutting down Telegram bot")

app = FastAPI(title="Помощник официанта", version="1.0.0", lifespan=lifespan)
app.mount("/images", StaticFiles(directory=str(PUBLIC_DIR / "images"), check_dir=False), name="images")
app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR), check_dir=False), name="static")

@app.get("/")
async def index():
    return FileResponse(PUBLIC_DIR / "index.html")

@app.get("/api/health")
async def health():
    return {"ok": True, "bot_configured": bool(BOT_TOKEN), "webapp_url_configured": bool(WEBAPP_URL)}

@app.get("/api/me")
async def me(employee: dict = Depends(current_employee)):
    shift = get_active_shift(employee["telegram_id"])
    result = {"telegram_id": employee["telegram_id"], "username": employee["username"],
              "first_name": employee["first_name"], "last_name": employee["last_name"], "display_name": employee.get("display_name", ""),
              "role": employee["role"], "active_shift": shift}
    if employee["role"] in ("manager", "admin"):
        with db_connect() as con:
            result["pending_count"] = con.execute("SELECT COUNT(*) FROM employees WHERE status='pending'").fetchone()[0]
            result["active_staff_count"] = con.execute("SELECT COUNT(DISTINCT employee_id) FROM shifts WHERE status='active'").fetchone()[0]
    return result

@app.get("/api/menu")
async def get_menu(q: str = "", section: str = "", category: str = "", stock: str = "all", employee: dict = Depends(current_employee)):
    sql = "SELECT * FROM menu_items WHERE is_active=1"; params: list = []
    if q.strip():
        like=f"%{q.strip()}%"
        sql += " AND (name LIKE ? OR composition LIKE ? OR description LIKE ? OR extra_info LIKE ? OR category LIKE ?)"
        params += [like,like,like,like,like]
    if section: sql += " AND section=?"; params.append(section)
    if category: sql += " AND category=?"; params.append(category)
    if stock == "available": sql += " AND stock_status='available'"
    if stock == "stop": sql += " AND stock_status='stop'"
    sql += " ORDER BY section,category,name COLLATE NOCASE"
    with db_connect() as con:
        rows = con.execute(sql, params).fetchall()
    return {"items":[dict(r) for r in rows], "sections": sorted({r["section"] for r in rows})}

@app.get("/api/categories")
async def get_categories(employee: dict = Depends(current_employee)):
    with db_connect() as con:
        rows = con.execute("SELECT DISTINCT section,category FROM menu_items WHERE is_active=1 ORDER BY section,category").fetchall()
    sections = {}
    for r in rows: sections.setdefault(r["section"], []).append(r["category"])
    return {"sections": sections}

@app.get("/api/availability")
async def get_availability(employee: dict = Depends(current_employee)):
    with db_connect() as con:
        rows = con.execute("SELECT id,stock_status,stop_reason,updated_at,updated_by FROM menu_items WHERE is_active=1").fetchall()
    return {"items":[dict(r) for r in rows]}

@app.get("/api/stops")
async def get_stops(employee: dict = Depends(current_employee)):
    with db_connect() as con:
        rows = con.execute("""SELECT m.*, COALESCE(NULLIF(e.display_name,''), TRIM(COALESCE(e.first_name,'') || ' ' || COALESCE(e.last_name,'')), '') AS updated_by_name
                                 FROM menu_items m LEFT JOIN employees e ON e.telegram_id=m.updated_by
                                 WHERE m.is_active=1 AND m.stock_status='stop' ORDER BY m.category,m.name COLLATE NOCASE""").fetchall()
    return {"items":[dict(r) for r in rows]}

@app.get("/api/audit")
async def get_audit(limit: int = 50, employee: dict = Depends(current_employee)):
    limit=max(1,min(limit,150))
    with db_connect() as con:
        rows=con.execute("SELECT a.*, e.first_name, e.last_name, e.display_name FROM audit_log a LEFT JOIN employees e ON e.telegram_id=a.employee_id ORDER BY a.id DESC LIMIT ?",(limit,)).fetchall()
    return {"events":[dict(r) for r in rows]}

class StopBody(BaseModel):
    reason: str = Field(default="", max_length=300)

@app.post("/api/items/{item_id}/stop")
async def stop_item(item_id: str, body: StopBody, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        item=con.execute("SELECT id,name,stock_status FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
        if not item: raise HTTPException(404,"item_not_found")
        if item['stock_status'] == 'stop':
            current=con.execute("SELECT * FROM menu_items WHERE id=?",(item_id,)).fetchone()
            return {"item":dict(current),"already_stopped":True}
        now=now_iso()
        con.execute("UPDATE menu_items SET stock_status='stop',stop_reason=?,updated_at=?,updated_by=? WHERE id=?",(body.reason.strip(),now,employee["telegram_id"],item_id))
        con.execute("INSERT INTO audit_log(item_id,item_name,action,reason,employee_id,created_at) VALUES (?,?,?,?,?,?)",(item_id,item["name"],"stop",body.reason.strip(),employee["telegram_id"],now))
        con.commit()
        current=con.execute("SELECT * FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
    await notify_approved_staff(f"🚫 Добавлено в стоп-лист\nПозиция: {item['name']}\nСотрудник: {employee_label(employee)}" + (f"\nПричина: {body.reason.strip()}" if body.reason.strip() else ""), exclude={employee["telegram_id"]})
    return {"item":dict(current)}

@app.post("/api/items/{item_id}/restore")
async def restore_item(item_id: str, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        item=con.execute("SELECT id,name,stock_status FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
        if not item: raise HTTPException(404,"item_not_found")
        if item['stock_status'] == 'available':
            current=con.execute("SELECT * FROM menu_items WHERE id=?",(item_id,)).fetchone()
            return {"item":dict(current),"already_available":True}
        now=now_iso()
        con.execute("UPDATE menu_items SET stock_status='available',stop_reason='',updated_at=?,updated_by=? WHERE id=?",(now,employee["telegram_id"],item_id))
        con.execute("INSERT INTO audit_log(item_id,item_name,action,reason,employee_id,created_at) VALUES (?,?,?,?,?,?)",(item_id,item["name"],"restore","",employee["telegram_id"],now))
        con.commit()
        current=con.execute("SELECT * FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
    await notify_approved_staff(f"✅ Возвращено в меню\nПозиция: {item['name']}\nСотрудник: {employee_label(employee)}", exclude={employee["telegram_id"]})
    return {"item":dict(current)}

@app.post("/api/shifts/start")
async def start_shift(employee: dict = Depends(current_employee)):
    tid=employee["telegram_id"]
    existing=get_active_shift(tid)
    if existing: return {"shift":existing,"already_active":True}
    with db_connect() as con:
        cur=con.execute("INSERT INTO shifts(employee_id,started_at,status) VALUES (?,?, 'active')",(tid,now_iso()))
        con.commit()
        shift=con.execute("SELECT * FROM shifts WHERE id=?",(cur.lastrowid,)).fetchone()
    await notify_approved_staff(f"🟢 Смена началась\n{employee_label(employee)} вышел(ла) на смену.", exclude={tid})
    return {"shift":dict(shift),"already_active":False}

@app.post("/api/shifts/finish")
async def finish_shift(employee: dict = Depends(current_employee)):
    tid=employee["telegram_id"]
    shift=get_active_shift(tid)
    if not shift: raise HTTPException(409,"no_active_shift")
    end=now_iso()
    with db_connect() as con:
        con.execute("UPDATE shifts SET status='closed',ended_at=? WHERE id=? AND employee_id=?",(end,shift["id"],tid))
        con.commit()
        row=con.execute("SELECT * FROM shifts WHERE id=?",(shift["id"],)).fetchone()
    await notify_approved_staff(f"🔴 Смена завершена\n{employee_label(employee)} завершил(а) смену.", exclude={tid})
    return {"shift":dict(row)}

@app.get("/api/shifts/history")
async def shifts_history(limit: int = 30, employee: dict = Depends(current_employee)):
    limit=max(1,min(limit,100))
    with db_connect() as con:
        if employee["role"] in ("manager","admin"):
            rows=con.execute("SELECT s.*,e.first_name,e.last_name,e.display_name FROM shifts s JOIN employees e ON e.telegram_id=s.employee_id ORDER BY s.id DESC LIMIT ?",(limit,)).fetchall()
        else:
            rows=con.execute("SELECT s.*,e.first_name,e.last_name,e.display_name FROM shifts s JOIN employees e ON e.telegram_id=s.employee_id WHERE s.employee_id=? ORDER BY s.id DESC LIMIT ?",(employee["telegram_id"],limit)).fetchall()
    return {"shifts":[dict(r) for r in rows]}

class StaffUpdateBody(BaseModel):
    role: str = "waiter"
    status: str = "approved"

@app.get("/api/staff")
async def list_staff(employee: dict = Depends(current_employee)):
    if employee["role"] not in ("admin", "manager"): raise HTTPException(403, "manager_required")
    with db_connect() as con:
        rows=con.execute("SELECT telegram_id,username,first_name,last_name,display_name,role,status,created_at,approved_at FROM employees ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, display_name, first_name").fetchall()
    return {"staff":[dict(r) for r in rows]}

@app.patch("/api/staff/{telegram_id}")
async def update_staff(telegram_id: int, body: StaffUpdateBody, employee: dict = Depends(current_employee)):
    if employee["role"] not in ("admin", "manager"): raise HTTPException(403, "manager_required")
    if body.role not in ("admin", "manager", "waiter", "trainee"): raise HTTPException(422, "invalid_role")
    if body.status not in ("approved", "rejected", "pending"): raise HTTPException(422, "invalid_status")
    if telegram_id == employee["telegram_id"] and body.status != "approved": raise HTTPException(400, "cannot_disable_self")
    with db_connect() as con:
        cur=con.execute("UPDATE employees SET role=?,status=?,approved_at=CASE WHEN ?='approved' THEN COALESCE(approved_at,?) ELSE approved_at END,approved_by=? WHERE telegram_id=?",(body.role,body.status,body.status,now_iso(),employee["telegram_id"],telegram_id)); con.commit()
    if not cur.rowcount: raise HTTPException(404,"staff_not_found")
    try:
        if bot_app and body.status=='approved': await bot_app.bot.send_message(telegram_id,f"Доступ подтверждён. Твоя роль: {role_label(body.role)}. Напиши /start.")
        elif bot_app and body.status=='rejected': await bot_app.bot.send_message(telegram_id,"Доступ к помощнику отключён. Если это ошибка, обратись к управляющему.")
    except Exception: pass
    return {"ok":True}

class CreateOrderBody(BaseModel):
    table_number: str = Field(min_length=1,max_length=30)

@app.get("/api/orders")
async def list_orders(employee: dict = Depends(current_employee)):
    with db_connect() as con:
        if employee["role"] in ("manager","admin"):
            rows=con.execute("SELECT id FROM orders ORDER BY updated_at DESC LIMIT 75").fetchall()
        else:
            rows=con.execute("SELECT id FROM orders WHERE created_by=? ORDER BY updated_at DESC LIMIT 75",(employee["telegram_id"],)).fetchall()
        orders=[order_payload(con,r["id"]) for r in rows]
    return {"orders":orders}

@app.post("/api/orders")
async def create_order(body: CreateOrderBody, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    table=body.table_number.strip()
    if not table: raise HTTPException(422,"table_required")
    with db_connect() as con:
        now=now_iso()
        cur=con.execute("INSERT INTO orders(table_number,created_by,created_at,updated_at,status) VALUES (?,?,?,?, 'draft')",(table,employee["telegram_id"],now,now))
        con.commit()
        result=order_payload(con,cur.lastrowid)
    return {"order":result}

@app.get("/api/orders/{order_id}")
async def get_order(order_id: int, employee: dict = Depends(current_employee)):
    with db_connect() as con:
        order=con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404,"order_not_found")
        if not can_access_order(employee,order): raise HTTPException(403,"order_forbidden")
        return {"order":order_payload(con,order_id)}

class AddLineBody(BaseModel):
    item_id: str
    quantity: int = Field(default=1,ge=1,le=50)
    note: str = Field(default="",max_length=300)

@app.post("/api/orders/{order_id}/lines")
async def add_order_line(order_id: int, body: AddLineBody, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        order=con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404,"order_not_found")
        if not can_access_order(employee,order): raise HTTPException(403,"order_forbidden")
        item=con.execute("SELECT * FROM menu_items WHERE id=? AND is_active=1",(body.item_id,)).fetchone()
        if not item: raise HTTPException(404,"item_not_found")
        if item["stock_status"]=='stop': raise HTTPException(409,"item_is_stopped")
        note=body.note.strip()
        existing=con.execute("SELECT id,quantity FROM order_lines WHERE order_id=? AND item_id=? AND note=?",(order_id,body.item_id,note)).fetchone()
        if existing:
            con.execute("UPDATE order_lines SET quantity=? WHERE id=?",(min(50,existing["quantity"]+body.quantity),existing["id"]))
        else:
            con.execute("INSERT INTO order_lines(order_id,item_id,item_name,quantity,note,created_at) VALUES (?,?,?,?,?,?)",(order_id,body.item_id,item["name"],body.quantity,note,now_iso()))
        con.execute("UPDATE orders SET updated_at=?,status='draft' WHERE id=?",(now_iso(),order_id))
        con.commit()
        result=order_payload(con,order_id)
    return {"order":result}

class UpdateLineBody(BaseModel):
    quantity: int = Field(ge=1,le=50)
    note: str = Field(default="",max_length=300)

@app.patch("/api/orders/{order_id}/lines/{line_id}")
async def update_order_line(order_id: int, line_id: int, body: UpdateLineBody, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        order=con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404,"order_not_found")
        if not can_access_order(employee,order): raise HTTPException(403,"order_forbidden")
        line=con.execute("SELECT * FROM order_lines WHERE id=? AND order_id=?",(line_id,order_id)).fetchone()
        if not line: raise HTTPException(404,"line_not_found")
        con.execute("UPDATE order_lines SET quantity=?,note=? WHERE id=?",(body.quantity,body.note.strip(),line_id))
        con.execute("UPDATE orders SET updated_at=?,status='draft' WHERE id=?",(now_iso(),order_id))
        con.commit()
        result=order_payload(con,order_id)
    return {"order":result}

@app.delete("/api/orders/{order_id}/lines/{line_id}")
async def delete_order_line(order_id: int, line_id: int, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        order=con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404,"order_not_found")
        if not can_access_order(employee,order): raise HTTPException(403,"order_forbidden")
        con.execute("DELETE FROM order_lines WHERE id=? AND order_id=?",(line_id,order_id))
        con.execute("UPDATE orders SET updated_at=?,status='draft' WHERE id=?",(now_iso(),order_id))
        con.commit()
        result=order_payload(con,order_id)
    return {"order":result}

class OrderCommentBody(BaseModel):
    comment: str = Field(default="",max_length=600)

@app.post("/api/orders/{order_id}/save")
async def save_order(order_id: int, body: OrderCommentBody, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        order=con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404,"order_not_found")
        if not can_access_order(employee,order): raise HTTPException(403,"order_forbidden")
        con.execute("UPDATE orders SET status='saved',comment=?,updated_at=? WHERE id=?",(body.comment.strip(),now_iso(),order_id))
        con.commit()
        result=order_payload(con,order_id)
    return {"order":result}

@app.post("/api/orders/{order_id}/delete")
async def delete_order(order_id: int, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        order=con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
        if not order: raise HTTPException(404,"order_not_found")
        if not can_access_order(employee,order): raise HTTPException(403,"order_forbidden")
        con.execute("DELETE FROM orders WHERE id=?",(order_id,))
        con.commit()
    return {"ok":True}

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail":exc.detail})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=os.getenv("LOG_LEVEL", "info").lower())
