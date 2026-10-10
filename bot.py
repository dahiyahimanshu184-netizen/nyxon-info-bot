from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import asyncpg
import httpx
from utils import phone_normalize
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, PreCheckoutQueryHandler, filters,
)

# ---------------------------------------------------------------------------
# Configuration: set these in Railway Variables. Never commit real secrets.
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(v.strip()) for v in os.getenv("ADMIN_IDS", "").split(",") if v.strip().isdigit()}
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CUSTOM_API_URL = os.getenv("CUSTOM_API_URL", "").strip()
CUSTOM_API_KEY = os.getenv("CUSTOM_API_KEY", "").strip()
CUSTOM_API_KEY_HEADER = os.getenv("CUSTOM_API_KEY_HEADER", "Authorization").strip()
CUSTOM_API_KEY_PREFIX = os.getenv("CUSTOM_API_KEY_PREFIX", "Bearer ")
CUSTOM_PHONE_PARAM = os.getenv("CUSTOM_PHONE_PARAM", "phone").strip() or "phone"
CUSTOM_API_METHOD = os.getenv("CUSTOM_API_METHOD", "GET").strip().upper()
CUSTOM_API_TIMEOUT = float(os.getenv("CUSTOM_API_TIMEOUT", "15"))
NUMVERIFY_KEY = os.getenv("NUMVERIFY_KEY", "").strip()
NUMVERIFY_URL = os.getenv("NUMVERIFY_URL", "https://apilayer.net/api/validate").strip()
ABSTRACT_KEY = os.getenv("ABSTRACT_KEY", "").strip()
ABSTRACT_URL = os.getenv("ABSTRACT_URL", "https://phonevalidation.abstractapi.com/v1/").strip()
FREE_LOOKUPS = 5
REFERRAL_BONUS = 2
LOOKUP_COOLDOWN_SECONDS = 2.0

# Telegram Stars are the in-Telegram automatic payment rail for digital services.
DEFAULT_SUBSCRIPTION_STARS = 150
DEFAULT_CREDITS_PRICE_STARS = 50
DEFAULT_CREDITS_PACK_SIZE = 50
SUBSCRIPTION_DAYS = 30

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("nyxon")

pool: asyncpg.Pool | None = None
last_lookup_at: dict[int, float] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  first_name TEXT NOT NULL DEFAULT '',
  credits INTEGER NOT NULL DEFAULT 5 CHECK (credits >= 0),
  paid_until TIMESTAMPTZ,
  blocked BOOLEAN NOT NULL DEFAULT FALSE,
  referred_by BIGINT,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS force_channels (
  channel_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  join_url TEXT NOT NULL,
  added_by BIGINT NOT NULL,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS referrals (
  referred_user BIGINT PRIMARY KEY,
  referrer_user BIGINT NOT NULL,
  bonus_credited BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS payments (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  charge_id TEXT UNIQUE NOT NULL,
  invoice_payload TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount INTEGER NOT NULL,
  product TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS lookups (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  provider TEXT NOT NULL,
  outcome TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS audit (
  id BIGSERIAL PRIMARY KEY,
  actor_id BIGINT,
  action TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_paid_until ON users(paid_until);
CREATE INDEX IF NOT EXISTS idx_lookups_user_created ON lookups(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_id, created_at DESC);
"""

DEFAULT_SETTINGS = {
    "subscription_stars": str(DEFAULT_SUBSCRIPTION_STARS),
    "credits_price_stars": str(DEFAULT_CREDITS_PRICE_STARS),
    "credits_pack_size": str(DEFAULT_CREDITS_PACK_SIZE),
    "subscription_days": str(SUBSCRIPTION_DAYS),
    "welcome_text": "Welcome to NYXON INFO — fast, privacy-respecting phone validation.",
}


def dbpool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool is not initialized")
    return pool


async def init_db(app: Application) -> None:
    global pool
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is required; set your numeric Telegram user ID")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required. Add a Railway PostgreSQL service and reference its DATABASE_URL.")
    dsn = DATABASE_URL
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5, command_timeout=20)
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            await con.execute("INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING", key, value)
    log.info("PostgreSQL schema ready")


async def close_db(app: Application) -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


async def setting(key: str, default: str = "") -> str:
    row = await dbpool().fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await dbpool().execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
        key, str(value),
    )


async def audit(actor: int | None, action: str, details: dict[str, Any] | None = None) -> None:
    await dbpool().execute("INSERT INTO audit(actor_id,action,details) VALUES($1,$2,$3::jsonb)", actor, action, json.dumps(details or {}))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_paid(user_id: int) -> bool:
    row = await dbpool().fetchrow("SELECT paid_until FROM users WHERE user_id=$1", user_id)
    return bool(row and row["paid_until"] and row["paid_until"] > datetime.now(timezone.utc))


async def ensure_user(user) -> bool:
    """Create/update a user and return True only on first insert."""
    row = await dbpool().fetchrow(
        "INSERT INTO users(user_id,username,first_name,credits) VALUES($1,$2,$3,$4) "
        "ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen=NOW() "
        "RETURNING (xmax = 0) AS is_new",
        user.id, user.username or "", user.first_name or "", FREE_LOOKUPS,
    )
    return bool(row["is_new"])


async def get_user(user_id: int):
    return await dbpool().fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def all_channels():
    return await dbpool().fetch("SELECT channel_id,title,join_url FROM force_channels ORDER BY added_at ASC")


async def force_join_ok(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    channels = await all_channels()
    for row in channels:
        try:
            chat_ref: str | int = int(row["channel_id"]) if row["channel_id"].lstrip("-").isdigit() else row["channel_id"]
            member = await context.bot.get_chat_member(chat_ref, user_id)
            if member.status in ("creator", ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
                continue
            if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
                continue
            return False
        except TelegramError:
            log.warning("Membership check failed for configured channel %s", row["channel_id"])
            return False
    return True


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    channels = await all_channels()
    if not channels or not update.effective_user:
        return False
    if await force_join_ok(context, update.effective_user.id):
        return False
    buttons = [[InlineKeyboardButton(f"📢 Join {row['title'] or row['channel_id']}", url=row["join_url"])] for row in channels]
    buttons.append([InlineKeyboardButton("✅ I Joined — Check Again", callback_data="join:check")])
    text = "🔒 **CHANNEL ACCESS REQUIRED**\n\nJoin every channel below, then tap *Check Again* to unlock NYXON INFO."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return True


def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔎 Phone Validation", callback_data="lookup:help"), InlineKeyboardButton("🎁 Referrals", callback_data="referrals")],
        [InlineKeyboardButton("👤 My Account", callback_data="account"), InlineKeyboardButton("💎 Premium", callback_data="premium")],
        [InlineKeyboardButton("🧾 Buy Credits", callback_data="buy:credits"), InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("⚙️ ADMIN CONTROL CENTER", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Force-Join Channels", callback_data="admin:channels"), InlineKeyboardButton("👥 Users & Stats", callback_data="admin:stats")],
        [InlineKeyboardButton("💳 Payment Pricing", callback_data="admin:pricing"), InlineKeyboardButton("📡 API Config", callback_data="admin:api")],
        [InlineKeyboardButton("🎟 Manage User", callback_data="admin:user"), InlineKeyboardButton("📣 Broadcast", callback_data="admin:broadcast")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_message:
        return
    is_new = await ensure_user(user)
    if is_new and context.args:
        m = re.fullmatch(r"ref_(\d+)", context.args[0])
        if m:
            referrer = int(m.group(1))
            if referrer != user.id:
                async with dbpool().acquire() as con:
                    async with con.transaction():
                        ref_exists = await con.fetchrow("SELECT user_id FROM users WHERE user_id=$1", referrer)
                        if ref_exists:
                            inserted = await con.fetchrow(
                                "INSERT INTO referrals(referred_user,referrer_user,bonus_credited) VALUES($1,$2,FALSE) ON CONFLICT(referred_user) DO NOTHING RETURNING referred_user",
                                user.id, referrer,
                            )
                            if inserted:
                                await con.execute("UPDATE users SET referred_by=$1 WHERE user_id=$2", referrer, user.id)
    # Credit a valid referral exactly once after the referred user opens the bot.
    async with dbpool().acquire() as con:
        async with con.transaction():
            referral = await con.fetchrow("SELECT referrer_user FROM referrals WHERE referred_user=$1 AND bonus_credited=FALSE", user.id)
            if referral:
                await con.execute("UPDATE users SET credits=credits+$1 WHERE user_id=$2", REFERRAL_BONUS, referral["referrer_user"])
                await con.execute("UPDATE referrals SET bonus_credited=TRUE WHERE referred_user=$1", user.id)
                await con.execute("INSERT INTO audit(actor_id,action,details) VALUES($1,$2,$3::jsonb)", referral["referrer_user"], "referral_bonus", json.dumps({"referred_user": user.id, "bonus": REFERRAL_BONUS}))
    if await gate(update, context):
        return
    welcome = await setting("welcome_text", DEFAULT_SETTINGS["welcome_text"])
    text = f"<b>ＮＹＸＯＮ  ＩＮＦＯ</b>\n━━━━━━━━━━━━━━━━━━\n{welcome}\n\n<i>Validation metadata only • Respect privacy</i>\n\nChoose an option below."
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard(user.id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and await gate(update, context):
        return
    await update.effective_message.reply_text(
        "<b>NYXON INFO — Quick Guide</b>\n\n• Tap <b>Phone Validation</b> and send a phone number with country code.\n• Free accounts start with 5 credits.\n• Invite a new user with your referral link to earn 2 credits after their first start.\n• Premium users can access the additional configured validation providers.\n• This bot only displays validation and non-sensitive metadata returned by authorized APIs.",
        parse_mode="HTML", reply_markup=main_keyboard(update.effective_user.id),
    )


async def cb_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if await gate(update, context):
        return
    await q.answer()
    await q.edit_message_text("<b>ＮＹＸＯＮ  ＩＮＦＯ</b>\nSelect a service.", parse_mode="HTML", reply_markup=main_keyboard(q.from_user.id))


async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    user_id = q.from_user.id
    if data == "join:check":
        await q.answer()
        if await force_join_ok(context, user_id):
            await q.message.reply_text("✅ Membership verified. Send /start to open the menu.")
        else:
            await q.message.reply_text("⚠️ You still need to join every required channel.")
        return
    if data == "home":
        await cb_home(update, context)
        return
    if not is_admin(user_id) and await gate(update, context):
        return
    if data == "lookup:help":
        await q.answer()
        context.user_data["awaiting_lookup"] = True
        await q.message.reply_text("📲 Send the phone number including country code (example format: +14155552671). Use only numbers you are authorized to validate.")
    elif data == "account":
        await q.answer()
        row = await get_user(user_id)
        paid_until = row["paid_until"].strftime("%Y-%m-%d %H:%M UTC") if row and row["paid_until"] and row["paid_until"] > datetime.now(timezone.utc) else "Not active"
        total = await dbpool().fetchval("SELECT COUNT(*) FROM lookups WHERE user_id=$1", user_id)
        await q.edit_message_text(f"<b>👤 YOUR ACCOUNT</b>\n━━━━━━━━━━━━━━━━\n🆔 <code>{user_id}</code>\n🎟 Credits: <b>{row['credits'] if row else 0}</b>\n💎 Premium: <b>{paid_until}</b>\n🔎 Lookups used: <b>{total}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="home")]]))
    elif data == "referrals":
        await q.answer()
        me = await context.bot.get_me()
        count = await dbpool().fetchval("SELECT COUNT(*) FROM referrals WHERE referrer_user=$1 AND bonus_credited=TRUE", user_id)
        row = await get_user(user_id)
        link = f"https://t.me/{me.username}?start=ref_{user_id}"
        await q.edit_message_text(f"<b>🎁 REFERRAL CENTER</b>\n\nSuccessful referrals: <b>{count}</b>\nBonus: <b>+{REFERRAL_BONUS} credits</b> each\nCurrent credits: <b>{row['credits'] if row else 0}</b>\n\nYour invite link:\n<code>{link}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="home")]]))
    elif data == "help":
        await q.answer()
        await q.edit_message_text("<b>Help & Privacy</b>\n\nThis service performs phone validation and returns limited, non-sensitive metadata from your configured APIs. It does not provide private subscriber identity, leaked records, addresses, or account access.\n\nSend /start to return to the menu.", parse_mode="HTML")
    elif data == "premium":
        await q.answer()
        sub_price = await setting("subscription_stars", str(DEFAULT_SUBSCRIPTION_STARS))
        days = await setting("subscription_days", str(SUBSCRIPTION_DAYS))
        await q.edit_message_text(f"<b>💎 NYXON PREMIUM</b>\n\n• Access additional validation providers\n• Premium account for {days} days\n• Faster queue when available\n\nPrice: <b>{sub_price} Telegram Stars</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Subscribe with Stars", callback_data="buy:subscription")], [InlineKeyboardButton("⬅️ Menu", callback_data="home")]]))
    elif data == "buy:credits":
        await q.answer()
        price = await setting("credits_price_stars", str(DEFAULT_CREDITS_PRICE_STARS))
        size = await setting("credits_pack_size", str(DEFAULT_CREDITS_PACK_SIZE))
        await q.edit_message_text(f"<b>🎟 CREDIT PACK</b>\n\nGet {size} lookup credits for <b>{price} Telegram Stars</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Buy credits", callback_data="buy:credits_confirm")], [InlineKeyboardButton("⬅️ Menu", callback_data="home")]]))
    elif data == "buy:subscription":
        await q.answer()
        price = int(await setting("subscription_stars", str(DEFAULT_SUBSCRIPTION_STARS)))
        days = int(await setting("subscription_days", str(SUBSCRIPTION_DAYS)))
        await send_invoice(context, q.message.chat_id, user_id, "subscription", price, f"NYXON Premium — {days} days", f"Premium access for {days} days")
    elif data == "buy:credits_confirm":
        await q.answer()
        price = int(await setting("credits_price_stars", str(DEFAULT_CREDITS_PRICE_STARS)))
        size = int(await setting("credits_pack_size", str(DEFAULT_CREDITS_PACK_SIZE)))
        await send_invoice(context, q.message.chat_id, user_id, "credits", price, f"NYXON Credit Pack — {size} credits", f"Adds {size} lookup credits")
    elif data == "admin":
        await q.answer()
        if not is_admin(user_id):
            return
        await q.edit_message_text("<b>⚙️ NYXON CONTROL CENTER</b>\nSelect an admin module.", parse_mode="HTML", reply_markup=admin_keyboard())
    elif data == "admin:channels":
        await q.answer()
        if not is_admin(user_id): return
        channels = await all_channels()
        lines = [f"• {r['title'] or r['channel_id']} (<code>{r['channel_id']}</code>)" for r in channels]
        text = "<b>📢 FORCE-JOIN CHANNELS</b>\n\n" + ("\n".join(lines) if lines else "No channels configured.")
        buttons = [[InlineKeyboardButton("➕ Add channel", callback_data="admin:channel_add")], [InlineKeyboardButton("➖ Remove channel", callback_data="admin:channel_remove")], [InlineKeyboardButton("⬅️ Admin menu", callback_data="admin")]]
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    elif data == "admin:channel_add":
        await q.answer()
        if not is_admin(user_id): return
        context.user_data["admin_action"] = "add_channel"
        await q.message.reply_text("Send a public channel as <code>@channelname</code>, or a private channel as <code>-1001234567890 | https://t.me/+invite</code>. Add the bot as an admin first.", parse_mode="HTML")
    elif data == "admin:channel_remove":
        await q.answer()
        if not is_admin(user_id): return
        channels = await all_channels()
        if not channels:
            await q.message.reply_text("No channels to remove.")
            return
        buttons = [[InlineKeyboardButton(f"🗑 {r['title'] or r['channel_id']}", callback_data=f"admin:channel_del:{r['channel_id']}")] for r in channels]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:channels")])
        await q.edit_message_text("Choose a channel to remove:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("admin:channel_del:"):
        await q.answer()
        if not is_admin(user_id): return
        channel_id = data.split(":", 2)[2]
        await dbpool().execute("DELETE FROM force_channels WHERE channel_id=$1", channel_id)
        await audit(user_id, "force_channel_removed", {"channel_id": channel_id})
        await q.edit_message_text(f"✅ Removed <code>{channel_id}</code> from force-join.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Channels", callback_data="admin:channels")]]))
    elif data == "admin:stats":
        await q.answer()
        if not is_admin(user_id): return
        users = await dbpool().fetchval("SELECT COUNT(*) FROM users")
        paid = await dbpool().fetchval("SELECT COUNT(*) FROM users WHERE paid_until>NOW()")
        lookups = await dbpool().fetchval("SELECT COUNT(*) FROM lookups")
        revenue = await dbpool().fetchval("SELECT COALESCE(SUM(amount),0) FROM payments WHERE currency='XTR'")
        await q.edit_message_text(f"<b>📊 BOT ANALYTICS</b>\n\n👥 Users: <b>{users}</b>\n💎 Active premium: <b>{paid}</b>\n🔎 Lookups: <b>{lookups}</b>\n⭐ Gross Stars recorded: <b>{revenue}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin menu", callback_data="admin")]]))
    elif data == "admin:api":
        await q.answer()
        if not is_admin(user_id): return
        state = lambda value: "✅ configured" if value else "⚠️ not configured"
        await q.edit_message_text(f"<b>📡 API CONFIGURATION</b>\n\nCustom API: {state(CUSTOM_API_URL)}\nNumverify: {state(NUMVERIFY_KEY)}\nAbstract: {state(ABSTRACT_KEY)}\nDatabase: PostgreSQL\nPayments: Telegram Stars", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin menu", callback_data="admin")]]))
    elif data == "admin:pricing":
        await q.answer()
        if not is_admin(user_id): return
        sub = await setting("subscription_stars", str(DEFAULT_SUBSCRIPTION_STARS)); cp = await setting("credits_price_stars", str(DEFAULT_CREDITS_PRICE_STARS)); size = await setting("credits_pack_size", str(DEFAULT_CREDITS_PACK_SIZE)); days = await setting("subscription_days", str(SUBSCRIPTION_DAYS))
        await q.edit_message_text(f"<b>💳 PAYMENT PRICING</b>\n\nSubscription: {sub} Stars / {days} days\nCredit pack: {cp} Stars / {size} credits\n\nChoose a setting to change:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Subscription price", callback_data="admin:set:subscription_stars"), InlineKeyboardButton("Subscription days", callback_data="admin:set:subscription_days")], [InlineKeyboardButton("Credits price", callback_data="admin:set:credits_price_stars"), InlineKeyboardButton("Pack size", callback_data="admin:set:credits_pack_size")], [InlineKeyboardButton("⬅️ Admin menu", callback_data="admin")]]))
    elif data.startswith("admin:set:"):
        await q.answer()
        if not is_admin(user_id): return
        key = data.split(":", 2)[2]
        if key not in {"subscription_stars", "subscription_days", "credits_price_stars", "credits_pack_size"}: return
        context.user_data["admin_action"] = "set:" + key
        await q.message.reply_text(f"Send the new positive integer value for <code>{key}</code>.", parse_mode="HTML")
    elif data == "admin:user":
        await q.answer()
        if not is_admin(user_id): return
        context.user_data["admin_action"] = "manage_user"
        await q.message.reply_text("Send user ID to manage, e.g. <code>123456789</code>.", parse_mode="HTML")
    elif data == "admin:broadcast":
        await q.answer()
        if not is_admin(user_id): return
        context.user_data["admin_action"] = "broadcast"
        await q.message.reply_text("Send the broadcast text. It will be delivered to bot users; keep it relevant and compliant.")


async def send_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, product: str, amount: int, title: str, description: str) -> None:
    if amount < 1:
        await context.bot.send_message(chat_id, "Payment amount is not configured correctly. Contact the admin.")
        return
    payload = f"nyxon:{product}:{user_id}:{int(time.time())}"
    await context.bot.send_invoice(
        chat_id=chat_id, title=title[:32], description=description[:255], payload=payload,
        provider_token="", currency="XTR", prices=[LabeledPrice(title[:32], amount)],
    )


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    try:
        parts = q.invoice_payload.split(":")
        if len(parts) != 4 or parts[0] != "nyxon" or int(parts[2]) != q.from_user.id or q.currency != "XTR":
            await q.answer(ok=False, error_message="This invoice is invalid. Please start a fresh purchase.")
            return
        product = parts[1]
        expected = int(await setting("subscription_stars", str(DEFAULT_SUBSCRIPTION_STARS))) if product == "subscription" else int(await setting("credits_price_stars", str(DEFAULT_CREDITS_PRICE_STARS))) if product == "credits" else -1
        if expected != q.total_amount:
            await q.answer(ok=False, error_message="Price changed. Please create a new invoice.")
            return
        await q.answer(ok=True)
    except Exception:
        log.exception("Pre-checkout validation failed")
        await q.answer(ok=False, error_message="Could not validate payment. Please try again.")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    sp = msg.successful_payment if msg else None
    if not user or not sp:
        return
    payload = sp.invoice_payload.split(":")
    if len(payload) != 4 or payload[0] != "nyxon" or payload[2] != str(user.id) or sp.currency != "XTR":
        log.error("Rejected unexpected successful payment payload for user %s", user.id)
        return
    product = payload[1]
    async with dbpool().acquire() as con:
        async with con.transaction():
            inserted = await con.fetchrow(
                "INSERT INTO payments(user_id,charge_id,invoice_payload,currency,amount,product) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(charge_id) DO NOTHING RETURNING id",
                user.id, sp.telegram_payment_charge_id, sp.invoice_payload, sp.currency, sp.total_amount, product,
            )
            if not inserted:
                await msg.reply_text("This payment was already processed. Your account is safe from duplicate crediting.")
                return
            if product == "credits":
                size = int(await con.fetchval("SELECT value FROM settings WHERE key='credits_pack_size'"))
                await con.execute("UPDATE users SET credits=credits+$1 WHERE user_id=$2", size, user.id)
            elif product == "subscription":
                days = int(await con.fetchval("SELECT value FROM settings WHERE key='subscription_days'"))
                await con.execute("UPDATE users SET paid_until=GREATEST(COALESCE(paid_until,NOW()),NOW()) + ($1 * INTERVAL '1 day') WHERE user_id=$2", days, user.id)
            else:
                log.error("Unknown product after successful payment: %s", product)
                return
            await con.execute("INSERT INTO audit(actor_id,action,details) VALUES($1,'payment_success',$2::jsonb)", user.id, json.dumps({"product": product, "amount": sp.total_amount}))
    await msg.reply_text("✅ <b>Payment successful!</b> Your purchase has been applied to your account.", parse_mode="HTML", reply_markup=main_keyboard(user.id))


async def call_custom(phone: str) -> dict[str, Any]:
    if not CUSTOM_API_URL:
        return {"provider": "Custom API", "status": "not_configured"}
    parsed = urlparse(CUSTOM_API_URL)
    if parsed.scheme != "https" or not parsed.netloc:
        return {"provider": "Custom API", "status": "configuration_error", "message": "Custom API endpoint must use HTTPS."}
    if CUSTOM_API_METHOD not in {"GET", "POST"}:
        return {"provider": "Custom API", "status": "configuration_error", "message": "CUSTOM_API_METHOD must be GET or POST."}
    headers = {"Accept": "application/json"}
    if CUSTOM_API_KEY:
        headers[CUSTOM_API_KEY_HEADER] = f"{CUSTOM_API_KEY_PREFIX}{CUSTOM_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=CUSTOM_API_TIMEOUT, follow_redirects=False) as client:
            if CUSTOM_API_METHOD == "GET":
                response = await client.get(CUSTOM_API_URL, params={CUSTOM_PHONE_PARAM: phone}, headers=headers)
            else:
                response = await client.post(CUSTOM_API_URL, json={CUSTOM_PHONE_PARAM: phone}, headers=headers)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            return {"provider": "Custom API", "status": "invalid_response"}
        # Explicit allowlist: never echo arbitrary API response fields to users.
        allowed = ("valid", "country", "country_name", "country_code", "calling_code", "carrier", "line_type", "type", "national_format", "international_format", "message")
        safe = {k: data[k] for k in allowed if k in data and isinstance(data[k], (str, int, float, bool, type(None)))}
        return {"provider": "Custom API", "status": "ok", "data": safe}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Custom API lookup failed: %s", type(exc).__name__)
        return {"provider": "Custom API", "status": "error", "message": "The configured provider is temporarily unavailable."}


async def call_numverify(phone: str) -> dict[str, Any]:
    if not NUMVERIFY_KEY:
        return {"provider": "Numverify", "status": "not_configured"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(NUMVERIFY_URL, params={"access_key": NUMVERIFY_KEY, "number": phone.lstrip("+")})
            response.raise_for_status(); data = response.json()
        if not isinstance(data, dict): return {"provider": "Numverify", "status": "invalid_response"}
        allowed = ("valid", "number", "local_format", "international_format", "country_prefix", "country_code", "country_name", "location", "carrier", "line_type")
        return {"provider": "Numverify", "status": "ok", "data": {k: data[k] for k in allowed if k in data and isinstance(data[k], (str, int, float, bool, type(None)))}}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Numverify lookup failed: %s", type(exc).__name__)
        return {"provider": "Numverify", "status": "error", "message": "Provider unavailable."}


async def call_abstract(phone: str) -> dict[str, Any]:
    if not ABSTRACT_KEY:
        return {"provider": "Abstract API", "status": "not_configured"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(ABSTRACT_URL, params={"api_key": ABSTRACT_KEY, "phone": phone})
            response.raise_for_status(); data = response.json()
        if not isinstance(data, dict): return {"provider": "Abstract API", "status": "invalid_response"}
        allowed = ("valid", "phone", "phone_country_code", "phone_carrier", "phone_line_type", "country", "location")
        return {"provider": "Abstract API", "status": "ok", "data": {k: data[k] for k in allowed if k in data and isinstance(data[k], (str, int, float, bool, type(None)))}}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Abstract API lookup failed: %s", type(exc).__name__)
        return {"provider": "Abstract API", "status": "error", "message": "Provider unavailable."}


def render_result(result: dict[str, Any]) -> str:
    provider = result.get("provider", "Provider")
    status = result.get("status", "unknown")
    if status == "not_configured": return f"<b>{provider}</b>\nNot configured."
    if status != "ok": return f"<b>{provider}</b>\n⚠️ {result.get('message', 'Lookup failed.') }"
    data = result.get("data", {})
    lines = [f"<b>{provider}</b>"]
    for key, value in data.items():
        if value is not None and value != "":
            label = key.replace("_", " ").title()
            safe_value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"• {label}: <code>{safe_value[:200]}</code>")
    return "\n".join(lines) if len(lines) > 1 else f"<b>{provider}</b>\nNo metadata returned."


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not msg.text:
        return
    if not await get_user(user.id):
        await ensure_user(user)
    action = context.user_data.get("admin_action")
    if action and is_admin(user.id):
        context.user_data.pop("admin_action", None)
        if action == "add_channel":
            raw = msg.text.strip()
            parts = [x.strip() for x in raw.split("|", 1)]
            channel_ref = parts[0]
            try:
                chat_ref = int(channel_ref) if channel_ref.lstrip("-").isdigit() else channel_ref
                chat = await context.bot.get_chat(chat_ref)
                me = await context.bot.get_me()
                bot_member = await context.bot.get_chat_member(chat.id, me.id)
                if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, "creator"):
                    await msg.reply_text("⚠️ I need to be an administrator in that channel to check membership. No channel was added.")
                    return
                join_url = parts[1] if len(parts) > 1 else (f"https://t.me/{chat.username}" if chat.username else "")
                if not join_url.startswith("https://t.me/"):
                    await msg.reply_text("Private channels need an invite URL. Send: <code>-1001234567890 | https://t.me/+invite</code>", parse_mode="HTML")
                    return
                channel_id = str(chat.id)
                await dbpool().execute("INSERT INTO force_channels(channel_id,title,join_url,added_by) VALUES($1,$2,$3,$4) ON CONFLICT(channel_id) DO UPDATE SET title=EXCLUDED.title,join_url=EXCLUDED.join_url,added_by=EXCLUDED.added_by", channel_id, chat.title or channel_ref, join_url, user.id)
                await audit(user.id, "force_channel_added", {"channel_id": channel_id, "title": chat.title})
                await msg.reply_text(f"✅ Added <b>{chat.title or channel_ref}</b> to force-join.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Manage channels", callback_data="admin:channels")]]))
            except TelegramError:
                await msg.reply_text("❌ Could not access that channel. Check the channel ID/username and ensure the bot is an admin.")
            return
        if action.startswith("set:"):
            key = action.split(":", 1)[1]
            if not msg.text.strip().isdigit() or int(msg.text.strip()) < 1 or int(msg.text.strip()) > 1000000:
                await msg.reply_text("Send a positive integer up to 1,000,000.")
                return
            await set_setting(key, msg.text.strip())
            await audit(user.id, "setting_updated", {"key": key, "value": msg.text.strip()})
            await msg.reply_text(f"✅ Updated <code>{key}</code> to <b>{msg.text.strip()}</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Payment pricing", callback_data="admin:pricing")]]))
            return
        if action == "manage_user":
            if not msg.text.strip().isdigit():
                await msg.reply_text("Send a numeric Telegram user ID."); return
            target = int(msg.text.strip()); row = await get_user(target)
            if not row:
                await msg.reply_text("User not found. They must open the bot first."); return
            buttons = [[InlineKeyboardButton("+10 credits", callback_data=f"admin:credit:{target}:10"), InlineKeyboardButton("+50 credits", callback_data=f"admin:credit:{target}:50")], [InlineKeyboardButton("Grant 30d Premium", callback_data=f"admin:grant:{target}"), InlineKeyboardButton("Revoke Premium", callback_data=f"admin:revoke:{target}")], [InlineKeyboardButton("Block user", callback_data=f"admin:block:{target}"), InlineKeyboardButton("Unblock", callback_data=f"admin:unblock:{target}")]]
            await msg.reply_text(f"User <code>{target}</code>\nCredits: {row['credits']}\nBlocked: {row['blocked']}\nPaid until: {row['paid_until']}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)); return
        if action == "broadcast":
            users = await dbpool().fetch("SELECT user_id FROM users WHERE blocked=FALSE")
            sent = failed = 0
            for row in users:
                try:
                    await context.bot.send_message(row["user_id"], msg.text[:4000]); sent += 1
                except TelegramError:
                    failed += 1
                await asyncio.sleep(0.04)
            await audit(user.id, "broadcast", {"sent": sent, "failed": failed})
            await msg.reply_text(f"📣 Broadcast finished. Sent: {sent}; failed: {failed}."); return
    # Ignore messages from users who have not requested a lookup.
    if not context.user_data.pop("awaiting_lookup", False):
        return
    if await gate(update, context):
        return
    row = await get_user(user.id)
    if row and row["blocked"]:
        await msg.reply_text("Your account is currently blocked. Contact the bot admin."); return
    premium_access = is_admin(user.id) or await is_paid(user.id)
    raw = phone_normalize(msg.text)
    if not raw:
        context.user_data["awaiting_lookup"] = True
        await msg.reply_text("❌ Invalid format. Send a phone number with 7–15 digits, optionally beginning with +.")
        return
    now = time.monotonic(); prev = last_lookup_at.get(user.id, 0)
    if now - prev < LOOKUP_COOLDOWN_SECONDS:
        context.user_data["awaiting_lookup"] = True
        await msg.reply_text("Please wait a moment before the next lookup."); return
    row = await get_user(user.id)
    if not premium_access and (not row or row["credits"] < 1):
        await msg.reply_text("🎟 You are out of credits. Buy a credit pack or invite a friend.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Buy credits", callback_data="buy:credits")], [InlineKeyboardButton("Referrals", callback_data="referrals")]])); return
    last_lookup_at[user.id] = now
    status = await msg.reply_text("🔎 <b>Validating number…</b>", parse_mode="HTML")
    results = [await call_custom(raw)]
    if is_admin(user.id) or await is_paid(user.id):
        results.extend(await asyncio.gather(call_numverify(raw), call_abstract(raw)))
    # Charge credits only if at least one configured provider completed a request.
    success = any(r.get("status") == "ok" for r in results)
    if success and not premium_access:
        changed = await dbpool().execute("UPDATE users SET credits=credits-1 WHERE user_id=$1 AND credits>0", user.id)
        if changed.endswith(" 0"):
            await status.edit_text("You ran out of credits. Please buy a pack."); return
    for r in results:
        await dbpool().execute("INSERT INTO lookups(user_id,provider,outcome) VALUES($1,$2,$3)", user.id, r.get("provider", "unknown"), r.get("status", "unknown"))
    row = await get_user(user.id)
    body = "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(render_result(r) for r in results)
    body += f"\n\n🎟 Credits remaining: <b>{row['credits'] if row else 0}</b>"
    body += "\n\n<i>Only authorized API metadata is shown. No private subscriber records.</i>"
    await status.edit_text(body[:3900], parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 New lookup", callback_data="lookup:help"), InlineKeyboardButton("💎 Premium", callback_data="premium")], [InlineKeyboardButton("⬅️ Menu", callback_data="home")]]))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not is_admin(q.from_user.id):
        if q: await q.answer("Admin only", show_alert=True)
        return
    data = q.data or ""
    await q.answer()
    m = re.fullmatch(r"admin:(credit|grant|revoke|block|unblock):(\d+)(?::(\d+))?", data)
    if not m: return
    action, target_s, amount_s = m.groups(); target = int(target_s)
    if action == "credit":
        amount = int(amount_s or 0); await dbpool().execute("UPDATE users SET credits=credits+$1 WHERE user_id=$2", amount, target)
        await audit(q.from_user.id, "credits_granted", {"target": target, "amount": amount})
        await q.edit_message_text(f"✅ Added {amount} credits to <code>{target}</code>.", parse_mode="HTML")
    elif action == "grant":
        await dbpool().execute("UPDATE users SET paid_until=GREATEST(COALESCE(paid_until,NOW()),NOW()) + INTERVAL '30 days' WHERE user_id=$1", target)
        await audit(q.from_user.id, "premium_granted", {"target": target})
        await q.edit_message_text(f"✅ Granted 30-day premium to <code>{target}</code>.", parse_mode="HTML")
    elif action == "revoke":
        await dbpool().execute("UPDATE users SET paid_until=NULL WHERE user_id=$1", target)
        await audit(q.from_user.id, "premium_revoked", {"target": target})
        await q.edit_message_text(f"✅ Premium revoked for <code>{target}</code>.", parse_mode="HTML")
    elif action == "block":
        await dbpool().execute("UPDATE users SET blocked=TRUE WHERE user_id=$1", target)
        await audit(q.from_user.id, "user_blocked", {"target": target})
        await q.edit_message_text(f"🚫 Blocked <code>{target}</code>.", parse_mode="HTML")
    elif action == "unblock":
        await dbpool().execute("UPDATE users SET blocked=FALSE WHERE user_id=$1", target)
        await audit(q.from_user.id, "user_unblocked", {"target": target})
        await q.edit_message_text(f"✅ Unblocked <code>{target}</code>.", parse_mode="HTML")


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Payment support: if your Telegram Stars purchase was charged but not applied, send the bot admin your Telegram user ID, approximate payment time, and Telegram Stars receipt details. Never share passwords or API keys. Refunds are reviewed according to the bot's published terms and Telegram requirements."
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id): return
    users = await dbpool().fetchval("SELECT COUNT(*) FROM users")
    paid = await dbpool().fetchval("SELECT COUNT(*) FROM users WHERE paid_until>NOW()")
    lookups = await dbpool().fetchval("SELECT COUNT(*) FROM lookups")
    await update.effective_message.reply_text(f"Users: {users}\nActive premium: {paid}\nLookups: {lookups}")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in Railway Variables")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(init_db).post_shutdown(close_db).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:(credit|grant|revoke|block|unblock):"))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Starting NYXON INFO bot polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
