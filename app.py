from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
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
                      image=excluded.image, price=COALESCE(menu_items.price, excluded.price), is_active=1
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
    return bool(employee and employee.get('status') == 'approved' and employee.get('role') == 'admin')


def get_admin_ids() -> set[int]:
    ids = set(ADMIN_IDS)
    with db_connect() as con:
        rows = con.execute("SELECT telegram_id FROM employees WHERE status='approved' AND role='admin'").fetchall()
    ids.update(int(r[0]) for r in rows)
    return ids


def add_or_update_employee(tg_user: dict) -> dict:
    tid = int(tg_user["id"])
    is_admin = is_admin_id(tid)
    with db_connect() as con:
        existing = con.execute("SELECT * FROM employees WHERE telegram_id=?", (tid,)).fetchone()
        if existing is None:
            con.execute("""INSERT INTO employees (telegram_id,username,first_name,last_name,role,status,created_at,approved_at)
                         VALUES (?,?,?,?,?,?,?,?)""",
                        (tid, tg_user.get("username", ""), tg_user.get("first_name", ""), tg_user.get("last_name", ""),
                         "admin" if is_admin else "waiter", "approved" if is_admin else "pending", now_iso(), now_iso() if is_admin else None))
        else:
            con.execute("UPDATE employees SET username=?, first_name=?, last_name=? WHERE telegram_id=?",
                        (tg_user.get("username", ""), tg_user.get("first_name", ""), tg_user.get("last_name", ""), tid))
            if is_admin:
                con.execute("UPDATE employees SET role='admin',status='approved',approved_at=COALESCE(approved_at,?) WHERE telegram_id=?", (now_iso(), tid))
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
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message: return
    u = update.effective_user
    employee = add_or_update_employee({"id": u.id, "username": u.username, "first_name": u.first_name, "last_name": u.last_name})
    name = u.first_name or "сотрудник"
    if employee.get("status") != "approved":
        await update.effective_message.reply_text(
            f"Привет, {name}! Твоя заявка на доступ записана. Попроси менеджера одобрить её.\n\nТвой Telegram ID: {u.id}"
        )
        for admin_id in get_admin_ids():
            try:
                await context.bot.send_message(admin_id, f"Новая заявка на доступ: {name} (ID {u.id}, @{u.username or 'без username'}).\nОдобрить: /approve {u.id} waiter")
            except Exception:
                log.exception("Could not notify admin %s", admin_id)
        return
    if WEBAPP_URL:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть помощника", web_app=WebAppInfo(url=WEBAPP_URL))]])
        await update.effective_message.reply_text(
            f"Привет, {name}!\n\nПомощник официанта готов. Начни смену в приложении, чтобы менять стоп-лист и вести черновики заказов.",
            reply_markup=keyboard
        )
    else:
        await update.effective_message.reply_text(
            f"Привет, {name}! Доступ подтверждён. Для открытия Mini App администратору нужно задать WEBAPP_URL в настройках хостинга.\nТвой Telegram ID: {u.id}"
        )

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
        await update.effective_message.reply_text("Формат: /approve TELEGRAM_ID waiter|manager|admin"); return
    target = int(context.args[0]); role = context.args[1].lower() if len(context.args) > 1 else "waiter"
    if role not in ("waiter", "manager", "admin"):
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
    return application


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    init_db()
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
              "first_name": employee["first_name"], "last_name": employee["last_name"],
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
        rows = con.execute("""SELECT m.*, COALESCE(TRIM(COALESCE(e.first_name,'') || ' ' || COALESCE(e.last_name,'')), '') AS updated_by_name
                                 FROM menu_items m LEFT JOIN employees e ON e.telegram_id=m.updated_by
                                 WHERE m.is_active=1 AND m.stock_status='stop' ORDER BY m.category,m.name COLLATE NOCASE""").fetchall()
    return {"items":[dict(r) for r in rows]}

@app.get("/api/audit")
async def get_audit(limit: int = 50, employee: dict = Depends(current_employee)):
    limit=max(1,min(limit,150))
    with db_connect() as con:
        rows=con.execute("SELECT a.*, e.first_name, e.last_name FROM audit_log a LEFT JOIN employees e ON e.telegram_id=a.employee_id ORDER BY a.id DESC LIMIT ?",(limit,)).fetchall()
    return {"events":[dict(r) for r in rows]}

class StopBody(BaseModel):
    reason: str = Field(default="", max_length=300)

@app.post("/api/items/{item_id}/stop")
async def stop_item(item_id: str, body: StopBody, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        item=con.execute("SELECT id,name,stock_status FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
        if not item: raise HTTPException(404,"item_not_found")
        now=now_iso()
        con.execute("UPDATE menu_items SET stock_status='stop',stop_reason=?,updated_at=?,updated_by=? WHERE id=?",(body.reason.strip(),now,employee["telegram_id"],item_id))
        con.execute("INSERT INTO audit_log(item_id,item_name,action,reason,employee_id,created_at) VALUES (?,?,?,?,?,?)",(item_id,item["name"],"stop",body.reason.strip(),employee["telegram_id"],now))
        con.commit()
        current=con.execute("SELECT * FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
    return {"item":dict(current)}

@app.post("/api/items/{item_id}/restore")
async def restore_item(item_id: str, employee: dict = Depends(current_employee)):
    require_active_shift(employee["telegram_id"])
    with db_connect() as con:
        item=con.execute("SELECT id,name,stock_status FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
        if not item: raise HTTPException(404,"item_not_found")
        now=now_iso()
        con.execute("UPDATE menu_items SET stock_status='available',stop_reason='',updated_at=?,updated_by=? WHERE id=?",(now,employee["telegram_id"],item_id))
        con.execute("INSERT INTO audit_log(item_id,item_name,action,reason,employee_id,created_at) VALUES (?,?,?,?,?,?)",(item_id,item["name"],"restore","",employee["telegram_id"],now))
        con.commit()
        current=con.execute("SELECT * FROM menu_items WHERE id=? AND is_active=1",(item_id,)).fetchone()
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
    return {"shift":dict(row)}

@app.get("/api/shifts/history")
async def shifts_history(limit: int = 30, employee: dict = Depends(current_employee)):
    limit=max(1,min(limit,100))
    with db_connect() as con:
        if employee["role"] in ("manager","admin"):
            rows=con.execute("SELECT s.*,e.first_name,e.last_name FROM shifts s JOIN employees e ON e.telegram_id=s.employee_id ORDER BY s.id DESC LIMIT ?",(limit,)).fetchall()
        else:
            rows=con.execute("SELECT s.*,e.first_name,e.last_name FROM shifts s JOIN employees e ON e.telegram_id=s.employee_id WHERE s.employee_id=? ORDER BY s.id DESC LIMIT ?",(employee["telegram_id"],limit)).fetchall()
    return {"shifts":[dict(r) for r in rows]}

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
