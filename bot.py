import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
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
CRYPTO_PAY_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "")
BINANCE_PAY_ID = os.environ.get("BINANCE_PAY_ID", "Not set yet")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # public https url after deploy
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("DB_PATH", "store.db")
CRYPTO_API_BASE = "https://pay.crypt.bot/api"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("store_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


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
            active INTEGER DEFAULT 1
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
    """Grabs the first available stock unit (without delivering it yet) and returns the row"""
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


async def deliver_order(order_id):
    """Sends the content (account/code) to the customer and closes the order"""
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
# CryptoBot API
# ------------------------------------------------------------------
async def cryptobot_create_invoice(amount: float, description: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{CRYPTO_API_BASE}/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
            json={
                "amount": str(amount),
                "currency_type": "fiat",
                "fiat": "USD",
                "accepted_assets": "USDT,TON,BTC",
                "description": description[:1024],
            },
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"CryptoBot error: {data}")
            return data["result"]


def verify_cryptobot_signature(body_bytes: bytes, signature: str) -> bool:
    secret = hashlib.sha256(CRYPTO_PAY_TOKEN.encode()).digest()
    computed = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature or "")


# ------------------------------------------------------------------
# Common keyboards
# ------------------------------------------------------------------
def main_menu_kb():
    kb = [[InlineKeyboardButton(text="🛍️ Open Store", web_app=WebAppInfo(url=WEBAPP_URL))]] if WEBAPP_URL else []
    kb.append([InlineKeyboardButton(text="📋 Show products here", callback_data="list_products")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def payment_kb(product_id):
    kb = [
        [InlineKeyboardButton(text="💎 Pay with CryptoBot", callback_data=f"pay_crypto:{product_id}")],
        [InlineKeyboardButton(text="🟡 Pay with Binance Pay", callback_data=f"pay_binance:{product_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ------------------------------------------------------------------
# Regular user commands
# ------------------------------------------------------------------
@router.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "👋 Welcome to the store!\n\nBrowse products and buy accounts & software codes, paying via:\n"
        "💎 CryptoBot (instant & automatic)\n🟡 Binance Pay\n\nTap the button below:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "list_products")
async def list_products_cb(callback: CallbackQuery):
    products = list_products()
    if not products:
        await callback.message.answer("No products available right now.")
        await callback.answer()
        return
    for p in products:
        text = f"<b>{p['name']}</b>\n{p['description'] or ''}\n\n💵 Price: {p['price']}$\n📦 In stock: {p['available']}"
        kb = payment_kb(p["id"]) if p["available"] > 0 else None
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("pay_crypto:"))
async def pay_crypto_cb(callback: CallbackQuery):
    if not CRYPTO_PAY_TOKEN:
        await callback.answer("CryptoBot is not enabled yet by the admin.", show_alert=True)
        return
    product_id = int(callback.data.split(":")[1])
    product = get_product(product_id)
    if not product:
        await callback.answer("Product not found", show_alert=True)
        return
    try:
        invoice = await cryptobot_create_invoice(product["price"], product["name"])
    except Exception:
        log.exception("cryptobot error")
        await callback.answer("Error creating the payment invoice, try again later.", show_alert=True)
        return
    order_id = create_order(
        callback.from_user.id, product_id, None, product["price"], "cryptobot", invoice_id=str(invoice["invoice_id"])
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💳 Pay now", url=invoice["bot_invoice_url"])]]
    )
    await callback.message.answer(
        f"🧾 Payment invoice created for order #{order_id}\nTap the button below to complete payment — the product will be delivered automatically once paid.",
        reply_markup=kb,
    )
    await callback.answer()


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
        f"🔔 New Binance Pay order #{order_id}\nProduct: {product['name']}\nAmount: {order['amount']}$\n"
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


class AddStock(StatesGroup):
    product_id = State()
    content = State()


def admin_only(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@router.message(Command("admin"))
async def admin_menu(message: Message):
    if not admin_only(message):
        return
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
    data = await state.update_data(category=message.text)
    conn = db()
    conn.execute(
        "INSERT INTO products (name, description, price, category) VALUES (?,?,?,?)",
        (data["name"], data["description"], data["price"], data["category"]),
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
# Web server (Mini App API + CryptoBot webhook receiver)
# ------------------------------------------------------------------
async def handle_index(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "webapp", "index.html"))


async def handle_api_products(request):
    return web.json_response(list_products())


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

    if method == "cryptobot":
        if not CRYPTO_PAY_TOKEN:
            return web.json_response({"ok": False, "error": "CryptoBot is not enabled"}, status=400)
        try:
            invoice = await cryptobot_create_invoice(product["price"], product["name"])
        except Exception:
            return web.json_response({"ok": False, "error": "Could not create the payment invoice"}, status=500)
        order_id = create_order(user["id"], product_id, None, product["price"], "cryptobot", invoice_id=str(invoice["invoice_id"]))
        return web.json_response({"ok": True, "order_id": order_id, "pay_url": invoice["bot_invoice_url"]})

    elif method == "binance":
        order_id = create_order(user["id"], product_id, None, product["price"], "binance", status="awaiting_payment")
        return web.json_response({"ok": True, "order_id": order_id, "binance_id": BINANCE_PAY_ID})

    return web.json_response({"ok": False, "error": "Unknown payment method"}, status=400)


async def handle_api_binance_confirm(request):
    """User taps 'I have paid' from the Mini App"""
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
        f"🔔 New Binance Pay order #{order_id}\nProduct: {product['name']}\nAmount: {order['amount']}$\n"
        f"Customer: <a href='tg://user?id={order['user_id']}'>{order['user_id']}</a>",
        reply_markup=kb,
    )
    return web.json_response({"ok": True})


async def handle_cryptobot_webhook(request):
    raw = await request.read()
    signature = request.headers.get("crypto-pay-api-signature", "")
    if not verify_cryptobot_signature(raw, signature):
        return web.json_response({"ok": False}, status=403)
    data = json.loads(raw)
    if data.get("update_type") == "invoice_paid":
        invoice = data["payload"]
        order = get_order(invoice_id=str(invoice["invoice_id"]))
        if order and order["status"] != "delivered":
            await deliver_order(order["id"])
    return web.json_response({"ok": True})


async def on_startup(app):
    init_db()
    asyncio.create_task(dp.start_polling(bot))
    log.info("Bot polling started")


def create_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/products", handle_api_products)
    app.router.add_post("/api/order", handle_api_order)
    app.router.add_post("/api/binance-confirm", handle_api_binance_confirm)
    app.router.add_post("/webhook/cryptobot", handle_cryptobot_webhook)
    app.router.add_static("/static/", os.path.join(os.path.dirname(__file__), "webapp"))
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
