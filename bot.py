import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

# ------------------------------------------------------------------
# Settings (read from environment variables - never hardcode secrets here)
# ------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
BINANCE_PAY_ID = os.environ.get("BINANCE_PAY_ID", "Not set yet")
USDT_TRC20 = os.environ.get("USDT_TRC20", "")
USDT_BEP20 = os.environ.get("USDT_BEP20", "")
USDT_ERC20 = os.environ.get("USDT_ERC20", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "No111x")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # public https url after deploy
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("DB_PATH", "store.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("store_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


def usdt_text():
    lines = []
    if USDT_TRC20:
        lines.append(f"🔸 TRC20 (Tron): <code>{USDT_TRC20}</code>")
    if USDT_BEP20:
        lines.append(f"🔸 BEP20 (BNB Smart Chain): <code>{USDT_BEP20}</code>")
    if USDT_ERC20:
        lines.append(f"🔸 ERC20 (Ethereum): <code>{USDT_ERC20}</code>")
    if not lines:
        lines.append("⚠️ No USDT address configured yet.")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Database (SQLite - single file, simple)
# ------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            category TEXT DEFAULT 'General',
            active INTEGER DEFAULT 1,
            image_file_id TEXT
        );
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            sold INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            stock_id INTEGER,
            amount REAL,
            method TEXT,
            status TEXT DEFAULT 'pending',
            invoice_id TEXT,
            created_at TEXT
        );
        """
    )
    # migration for databases created before the image feature existed
    try:
        conn.execute("ALTER TABLE products ADD COLUMN image_file_id TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------------
def list_products():
    conn = db()
    rows = conn.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id = p.id AND s.sold = 0) AS available
           FROM products p WHERE p.active = 1 ORDER BY p.id DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product(pid):
    conn = db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def reserve_stock(product_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM stock WHERE product_id=? AND sold=0 LIMIT 1", (product_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_stock_sold(stock_id):
    conn = db()
    conn.execute("UPDATE stock SET sold=1 WHERE id=?", (stock_id,))
    conn.commit()
    conn.close()


def create_order(user_id, product_id, stock_id, amount, method, invoice_id=None, status="pending"):
    conn = db()
    cur = conn.execute(
        "INSERT INTO orders (user_id, product_id, stock_id, amount, method, status, invoice_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, product_id, stock_id, amount, method, status, invoice_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    oid = cur.lastrowid
    conn.close()
    return oid


def get_order(order_id=None, invoice_id=None):
    conn = db()
    if order_id:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM orders WHERE invoice_id=?", (invoice_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_order_status(order_id, status):
    conn = db()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()


# ---- admin dashboard helpers ----
def admin_stats():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = db()
    orders_today = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at LIKE ?", (f"{today}%",)
    ).fetchone()[0]
    revenue_today = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='delivered' AND created_at LIKE ?", (f"{today}%",)
    ).fetchone()[0]
    conn.close()
    return {"orders_today": orders_today, "revenue_today": round(revenue_today, 2)}


def list_all_products_admin():
    conn = db()
    rows = conn.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM stock s WHERE s.product_id = p.id AND s.sold = 0) AS available,
                  (SELECT COUNT(*) FROM stock s WHERE s.product_id = p.id) AS total_stock
           FROM products p ORDER BY p.id DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_product(name, description, price, category):
    conn = db()
    conn.execute(
        "INSERT INTO products (name, description, price, category) VALUES (?,?,?,?)",
        (name, description, price, category),
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return pid


def update_product_fields(pid, name, description, price, category, active):
    conn = db()
    conn.execute(
        "UPDATE products SET name=?, description=?, price=?, category=?, active=? WHERE id=?",
        (name, description, price, category, active, pid),
    )
    conn.commit()
    conn.close()


def list_stock_admin(pid):
    conn = db()
    rows = conn.execute("SELECT * FROM stock WHERE product_id=? ORDER BY sold ASC, id DESC", (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_stock_lines(pid, lines):
    conn = db()
    for line in lines:
        conn.execute("INSERT INTO stock (product_id, content) VALUES (?,?)", (pid, line))
    conn.commit()
    conn.close()


def delete_stock_item(stock_id):
    conn = db()
    conn.execute("DELETE FROM stock WHERE id=? AND sold=0", (stock_id,))
    conn.commit()
    conn.close()


def list_orders_admin(limit=100):
    conn = db()
    rows = conn.execute(
        """SELECT o.*, p.name AS product_name FROM orders o
           LEFT JOIN products p ON p.id = o.product_id
           ORDER BY o.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def deliver_order(order_id):
    order = get_order(order_id=order_id)
    if not order or order["status"] == "delivered":
        return
    product = get_product(order["product_id"])
    stock_id = order["stock_id"]
    if stock_id is None:
        s = reserve_stock(order["product_id"])
        if not s:
            await bot.send_message(order["user_id"], "⚠️ Sorry, this product is out of stock. Please contact support for a refund.")
            await bot.send_message(ADMIN_ID, f"⚠️ Product #{order['product_id']} is out of stock but order #{order_id} was paid")
            return
        stock_id = s["id"]
    conn = db()
    stock_row = conn.execute("SELECT * FROM stock WHERE id=?", (stock_id,)).fetchone()
    conn.close()
    mark_stock_sold(stock_id)
    set_order_status(order_id, "delivered")
    await bot.send_message(
        order["user_id"],
        f"✅ Payment successful!\n\n<b>{product['name']}</b>\n\n📦 Your order content:\n<code>{stock_row['content']}</code>\n\nThank you for shopping with us 🌟",
    )
    await bot.send_message(ADMIN_ID, f"💰 New order completed #{order_id} — {product['name']} — {order['amount']}$")


# ------------------------------------------------------------------
# Validate Telegram Mini App initData — protects against tampering
# ------------------------------------------------------------------
def validate_init_data(init_data: str):
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed_hash != received_hash:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user
    except Exception as e:
        log.warning("initData invalid: %s", e)
        return None


# ------------------------------------------------------------------
# Common keyboards
# ------------------------------------------------------------------
def main_menu_kb():
    if WEBAPP_URL:
        shop_button = InlineKeyboardButton(text="🛍️ Shop", web_app=WebAppInfo(url=WEBAPP_URL))
    else:
        shop_button = InlineKeyboardButton(text="🛍️ Shop", callback_data="list_products")
    kb = [
        [shop_button],
        [InlineKeyboardButton(text="🆘 Support", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def payment_kb(product_id):
    kb = [
        [InlineKeyboardButton(text="💵 Pay with USDT", callback_data=f"pay_usdt:{product_id}")],
        [InlineKeyboardButton(text="🟡 Pay with Binance Pay", callback_data=f"pay_binance:{product_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ------------------------------------------------------------------
# Regular user commands
# ------------------------------------------------------------------
@router.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "👋 Welcome to our store!\n\nBrowse products and buy accounts & software codes, paying via:\n"
        "💵 USDT\n🟡 Binance Pay",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("support"))
async def support_cmd(message: Message):
    await message.answer(f"🆘 Need help? Contact support: @{SUPPORT_USERNAME}")


@router.callback_query(F.data == "list_products")
async def list_products_cb(callback: CallbackQuery):
    products = list_products()
    if not products:
        await callback.message.answer("No products available right now.")
        await callback.answer()
        return
    kb = [
        [InlineKeyboardButton(
            text=("🔵 " if p["available"] > 0 else "🔴 ")
            + f"{p['name']} — {p['price']}$"
            + ("" if p["available"] > 0 else " (out of stock)"),
            callback_data=f"view_product:{p['id']}",
        )]
        for p in products
    ]
    await callback.message.answer("🛍️ Available products:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()


@router.callback_query(F.data.startswith("view_product:"))
async def view_product_cb(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    p = get_product(product_id)
    if not p:
        await callback.answer("Product not found", show_alert=True)
        return
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0", (product_id,)).fetchone()[0]
    conn.close()
    text = f"<b>{p['name']}</b>\n{p['description'] or ''}\n\n💵 Price: {p['price']}$\n📦 In stock: {count}"
    kb_rows = list(payment_kb(p["id"]).inline_keyboard) if count > 0 else []
    kb_rows = kb_rows + [[InlineKeyboardButton(text="⬅️ Back to list", callback_data="list_products")]]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if p.get("image_file_id"):
        await callback.message.answer_photo(photo=p["image_file_id"], caption=text, reply_markup=kb)
    else:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("pay_usdt:"))
async def pay_usdt_cb(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    product = get_product(product_id)
    if not product:
        await callback.answer("Product not found", show_alert=True)
        return
    order_id = create_order(callback.from_user.id, product_id, None, product["price"], "usdt", status="awaiting_payment")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ I have paid", callback_data=f"usdt_paid:{order_id}")]]
    )
    await callback.message.answer(
        f"💵 Pay with USDT\n\nSend <b>{product['price']} USDT</b> to one of the addresses below "
        f"(make sure to use the matching network):\n\n{usdt_text()}\n\n"
        f"After sending, tap (I have paid) below. Order #{order_id} will be reviewed and delivered within minutes.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("usdt_paid:"))
async def usdt_paid_cb(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    await notify_admin_review(order_id, callback)


@router.callback_query(F.data.startswith("pay_binance:"))
async def pay_binance_cb(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    product = get_product(product_id)
    if not product:
        await callback.answer("Product not found", show_alert=True)
        return
    order_id = create_order(callback.from_user.id, product_id, None, product["price"], "binance", status="awaiting_payment")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ I have paid", callback_data=f"binance_paid:{order_id}")]]
    )
    await callback.message.answer(
        f"🟡 Pay with Binance Pay\n\nTransfer <b>{product['price']}$</b> to:\n<code>{BINANCE_PAY_ID}</code>\n\n"
        f"After transferring, tap the (I have paid) button below. Order #{order_id} will be reviewed and delivered within minutes.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("binance_paid:"))
async def binance_paid_cb(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    await notify_admin_review(order_id, callback)


async def notify_admin_review(order_id, callback: CallbackQuery):
    order = get_order(order_id=order_id)
    if not order:
        await callback.answer("Order not found", show_alert=True)
        return
    set_order_status(order_id, "pending_review")
    product = get_product(order["product_id"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm payment & deliver", callback_data=f"admin_confirm:{order_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject:{order_id}"),
            ]
        ]
    )
    await bot.send_message(
        ADMIN_ID,
        f"🔔 New {order['method'].upper()} order #{order_id}\nProduct: {product['name']}\nAmount: {order['amount']}$\n"
        f"Customer: <a href='tg://user?id={order['user_id']}'>{order['user_id']}</a>",
        reply_markup=kb,
    )
    await callback.message.answer("⏳ Your order was sent for review, it will be delivered once confirmed.")
    await callback.answer()


# ------------------------------------------------------------------
# Admin-only commands
# ------------------------------------------------------------------
@router.callback_query(F.data.startswith("admin_confirm:"))
async def admin_confirm_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not authorized", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    await deliver_order(order_id)
    await callback.message.edit_text(callback.message.text + "\n\n✅ Confirmed and delivered.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_reject:"))
async def admin_reject_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not authorized", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    order = get_order(order_id=order_id)
    set_order_status(order_id, "rejected")
    await bot.send_message(order["user_id"], "❌ Your payment could not be confirmed. Contact support if you're sure you sent it.")
    await callback.message.edit_text(callback.message.text + "\n\n❌ Rejected.")
    await callback.answer()


class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()
    category = State()
    image = State()


class AddStock(StatesGroup):
    product_id = State()
    content = State()


def admin_only(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@router.message(Command("admin"))
async def admin_menu(message: Message):
    if not admin_only(message):
        return
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🛠 Open Admin Panel", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))]]
        )
        await message.answer("🛠️ Tap below to open your admin dashboard:", reply_markup=kb)
    else:
        await message.answer(
            "🛠️ Admin panel:\n"
            "/addproduct — Add a new product\n"
            "/addstock — Add stock (accounts/codes) to a product\n"
            "/products — Show all products and their IDs\n"
            "/orders — Show recent orders"
        )


@router.message(Command("products"))
async def products_cmd(message: Message):
    if not admin_only(message):
        return
    products = list_products()
    if not products:
        await message.answer("No products yet.")
        return
    text = "\n".join(f"#{p['id']} - {p['name']} - {p['price']}$ - in stock: {p['available']}" for p in products)
    await message.answer(text)


@router.message(Command("orders"))
async def orders_cmd(message: Message):
    if not admin_only(message):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    if not rows:
        await message.answer("No orders yet.")
        return
    text = "\n".join(f"#{r['id']} - product {r['product_id']} - {r['status']} - {r['method']}" for r in rows)
    await message.answer(text)


@router.message(Command("addproduct"))
async def addproduct_start(message: Message, state: FSMContext):
    if not admin_only(message):
        return
    await state.set_state(AddProduct.name)
    await message.answer("📝 Enter the product name:")


@router.message(AddProduct.name)
async def addproduct_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AddProduct.description)
    await message.answer("📝 Enter a short description for the product:")


@router.message(AddProduct.description)
async def addproduct_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(AddProduct.price)
    await message.answer("💵 Enter the price in USD (example: 5.5):")


@router.message(AddProduct.price)
async def addproduct_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Enter a valid number for the price, example: 5.5")
        return
    await state.update_data(price=price)
    await state.set_state(AddProduct.category)
    await message.answer("🏷️ Enter the category (example: Accounts / Software codes):")


@router.message(AddProduct.category)
async def addproduct_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text)
    await state.set_state(AddProduct.image)
    await message.answer("🖼️ Send a logo/photo for this product now, or type 'skip' to continue without one:")


@router.message(AddProduct.image, F.photo)
async def addproduct_image_photo(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await save_new_product(message, state, image_file_id=file_id)


@router.message(AddProduct.image, F.text)
async def addproduct_image_text(message: Message, state: FSMContext):
    if message.text.strip().lower() not in ("skip", "لا", "تخطي"):
        await message.answer("Send a photo, or type 'skip' to continue without one.")
        return
    await save_new_product(message, state, image_file_id=None)


async def save_new_product(message: Message, state: FSMContext, image_file_id):
    data = await state.get_data()
    conn = db()
    conn.execute(
        "INSERT INTO products (name, description, price, category, image_file_id) VALUES (?,?,?,?,?)",
        (data["name"], data["description"], data["price"], data["category"], image_file_id),
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    await state.clear()
    await message.answer(f"✅ Product #{pid} added\nNow add stock for it with /addstock")


@router.message(Command("addstock"))
async def addstock_start(message: Message, state: FSMContext):
    if not admin_only(message):
        return
    products = list_products()
    if not products:
        await message.answer("Add a product first with /addproduct")
        return
    text = "Enter the ID of the product you want to add stock to:\n" + "\n".join(f"#{p['id']} - {p['name']}" for p in products)
    await state.set_state(AddStock.product_id)
    await message.answer(text)


@router.message(AddStock.product_id)
async def addstock_pid(message: Message, state: FSMContext):
    try:
        pid = int(message.text.strip().lstrip("#"))
    except ValueError:
        await message.answer("⚠️ Enter the product ID number only, example: 1")
        return
    if not get_product(pid):
        await message.answer("⚠️ That product ID does not exist.")
        return
    await state.update_data(product_id=pid)
    await state.set_state(AddStock.content)
    await message.answer(
        "📦 Now send the stock content. Each line = one unit delivered to one customer.\n\n"
        "Example:\nuser1:pass1\nuser2:pass2\n\nOr if it's a single account, send one line only."
    )


@router.message(AddStock.content)
async def addstock_content(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["product_id"]
    lines = [l.strip() for l in message.text.splitlines() if l.strip()]
    conn = db()
    for line in lines:
        conn.execute("INSERT INTO stock (product_id, content) VALUES (?,?)", (pid, line))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Added {len(lines)} stock unit(s) to product #{pid}")


# ------------------------------------------------------------------
# Web server (Mini App API + product images)
# ------------------------------------------------------------------
async def handle_index(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "webapp", "index.html"))


async def handle_api_products(request):
    products = list_products()
    for p in products:
        p["has_image"] = bool(p.get("image_file_id"))
        p.pop("image_file_id", None)
    return web.json_response(products)


async def handle_image(request):
    pid = int(request.match_info["product_id"])
    product = get_product(pid)
    if not product or not product.get("image_file_id"):
        return web.Response(status=404)
    file = await bot.get_file(product["image_file_id"])
    buf = await bot.download_file(file.file_path)
    return web.Response(body=buf.read(), content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


async def handle_api_order(request):
    body = await request.json()
    init_data = body.get("initData", "")
    user = validate_init_data(init_data)
    if not user:
        return web.json_response({"ok": False, "error": "Invalid Telegram data"}, status=403)
    product_id = int(body.get("product_id"))
    method = body.get("method")
    product = get_product(product_id)
    if not product:
        return web.json_response({"ok": False, "error": "Product not found"}, status=404)

    if method == "usdt":
        order_id = create_order(user["id"], product_id, None, product["price"], "usdt", status="awaiting_payment")
        return web.json_response({"ok": True, "order_id": order_id, "usdt_addresses": {
            "trc20": USDT_TRC20, "bep20": USDT_BEP20, "erc20": USDT_ERC20
        }})

    elif method == "binance":
        order_id = create_order(user["id"], product_id, None, product["price"], "binance", status="awaiting_payment")
        return web.json_response({"ok": True, "order_id": order_id, "binance_id": BINANCE_PAY_ID})

    return web.json_response({"ok": False, "error": "Unknown payment method"}, status=400)


async def handle_api_confirm(request):
    """User taps 'I have paid' from the Mini App (works for both usdt and binance)"""
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    if not user:
        return web.json_response({"ok": False}, status=403)
    order_id = int(body.get("order_id"))
    order = get_order(order_id=order_id)
    if not order or order["user_id"] != user["id"]:
        return web.json_response({"ok": False}, status=404)
    set_order_status(order_id, "pending_review")
    product = get_product(order["product_id"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm payment & deliver", callback_data=f"admin_confirm:{order_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject:{order_id}"),
            ]
        ]
    )
    await bot.send_message(
        ADMIN_ID,
        f"🔔 New {order['method'].upper()} order #{order_id}\nProduct: {product['name']}\nAmount: {order['amount']}$\n"
        f"Customer: <a href='tg://user?id={order['user_id']}'>{order['user_id']}</a>",
        reply_markup=kb,
    )
    return web.json_response({"ok": True})


async def on_startup(app):
    init_db()
    asyncio.create_task(dp.start_polling(bot))
    log.info("Bot polling started")


def require_admin(body):
    user = validate_init_data(body.get("initData", ""))
    return bool(user and user.get("id") == ADMIN_ID)


async def handle_admin_index(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "webapp", "admin.html"))


async def handle_admin_stats(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    return web.json_response({"ok": True, **admin_stats()})


async def handle_admin_products_list(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    products = list_all_products_admin()
    for p in products:
        p["has_image"] = bool(p.get("image_file_id"))
        p.pop("image_file_id", None)
    return web.json_response({"ok": True, "products": products})


async def handle_admin_products_create(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    try:
        price = float(body.get("price", 0))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid price"}, status=400)
    pid = create_product(body.get("name", "").strip(), body.get("description", "").strip(), price, body.get("category", "General").strip() or "General")
    return web.json_response({"ok": True, "id": pid})


async def handle_admin_products_update(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    try:
        price = float(body.get("price", 0))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid price"}, status=400)
    update_product_fields(
        int(body["id"]), body.get("name", "").strip(), body.get("description", "").strip(),
        price, body.get("category", "General").strip() or "General", 1 if body.get("active", True) else 0,
    )
    return web.json_response({"ok": True})


async def handle_admin_products_delete(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    p = get_product(int(body["id"]))
    if p:
        update_product_fields(p["id"], p["name"], p["description"], p["price"], p["category"], 0)
    return web.json_response({"ok": True})


async def handle_admin_stock_list(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    items = list_stock_admin(int(body["product_id"]))
    return web.json_response({"ok": True, "items": items})


async def handle_admin_stock_add(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    lines = [l.strip() for l in body.get("lines", "").splitlines() if l.strip()]
    add_stock_lines(int(body["product_id"]), lines)
    return web.json_response({"ok": True, "added": len(lines)})


async def handle_admin_stock_delete(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    delete_stock_item(int(body["stock_id"]))
    return web.json_response({"ok": True})


async def handle_admin_orders_list(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    return web.json_response({"ok": True, "orders": list_orders_admin()})


async def handle_admin_orders_confirm(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    await deliver_order(int(body["order_id"]))
    return web.json_response({"ok": True})


async def handle_admin_orders_reject(request):
    body = await request.json()
    if not require_admin(body):
        return web.json_response({"ok": False}, status=403)
    order_id = int(body["order_id"])
    order = get_order(order_id=order_id)
    if order:
        set_order_status(order_id, "rejected")
        await bot.send_message(order["user_id"], "❌ Your payment could not be confirmed. Contact support if you're sure you sent it.")
    return web.json_response({"ok": True})


def create_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/admin", handle_admin_index)
    app.router.add_get("/api/products", handle_api_products)
    app.router.add_get("/image/{product_id}", handle_image)
    app.router.add_post("/api/order", handle_api_order)
    app.router.add_post("/api/confirm-payment", handle_api_confirm)
    app.router.add_post("/api/admin/stats", handle_admin_stats)
    app.router.add_post("/api/admin/products/list", handle_admin_products_list)
    app.router.add_post("/api/admin/products/create", handle_admin_products_create)
    app.router.add_post("/api/admin/products/update", handle_admin_products_update)
    app.router.add_post("/api/admin/products/delete", handle_admin_products_delete)
    app.router.add_post("/api/admin/stock/list", handle_admin_stock_list)
    app.router.add_post("/api/admin/stock/add", handle_admin_stock_add)
    app.router.add_post("/api/admin/stock/delete", handle_admin_stock_delete)
    app.router.add_post("/api/admin/orders/list", handle_admin_orders_list)
    app.router.add_post("/api/admin/orders/confirm", handle_admin_orders_confirm)
    app.router.add_post("/api/admin/orders/reject", handle_admin_orders_reject)
    app.router.add_static("/static/", os.path.join(os.path.dirname(__file__), "webapp"))
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
