import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from number_lookup import lookup_number
import config

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("nyxon-info-bot")

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🖤 NYXON INFO BOT\n\n"
        "Send a phone number with country code to check available phone-number metadata.\n"
        "Example: +14155552671\n\n"
        "This bot only returns basic validation/line/carrier/country data provided by configured APIs."
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage: send a phone number with country code, e.g. +14155552671.\n"
        "Use /status to check configuration. Admins can use /id to see their Telegram numeric ID."
    )

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for the bot admin.")
        return
    ready = [
        f"Numverify: {'configured' if config.NUMVERIFY_KEY else 'not configured'}",
        f"Abstract: {'configured' if config.ABSTRACT_KEY else 'not configured'}",
        f"Custom API module: {'enabled' if config.CUSTOM_API_ENABLED else 'disabled'}",
    ]
    await update.message.reply_text("Configuration status:\n" + "\n".join(ready))

async def number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    # Avoid accidentally treating commands or long pasted text as phone numbers.
    compact = raw.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not compact.startswith("+") or not compact[1:].isdigit() or not (7 <= len(compact[1:]) <= 15):
        await update.message.reply_text(
            "Please send a phone number with country code.\nExample: +14155552671"
        )
        return
    await update.message.reply_text("🔎 Checking available number metadata…")
    try:
        results = await lookup_number(compact)
        lines = [f"📱 Number: {compact}"]
        any_result = False
        for provider, data in results.items():
            if data:
                any_result = True
                lines.append(f"\n🔹 {provider}")
                for key, value in data.items():
                    if value not in (None, "", "N/A", "Unknown"):
                        label = key.replace("_", " ").title()
                        lines.append(f"• {label}: {value}")
        if not any_result:
            lines.append("\nNo metadata returned. Check API keys/provider quota.")
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception:
        log.exception("Lookup failed")
        await update.message.reply_text("Lookup failed. Check Railway deployment logs and API settings.")

def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "PASTE_BOTFATHER_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN in config.py before starting.")
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, number_message))
    log.info("NYXON INFO BOT starting")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
