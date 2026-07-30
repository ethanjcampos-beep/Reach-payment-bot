"""
Reach Models Telegram Bot
- Listens for payment messages in a Telegram group
- Uses Claude to parse them into structured data
- Stores entries in a local JSON file (payments.json)
- Posts a monthly report to the group in your standard format
- Posts scheduled expense reminders to the group

Setup instructions are in README.md
"""

import os
import json
import asyncio
from datetime import datetime
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---- CONFIG (set these as environment variables on your host) ----
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])  # the Telegram group's chat ID
COMMISSION_SPLIT = float(os.environ.get("COMMISSION_SPLIT", "0.20"))  # Ethan's 20%

# Topic (thread) IDs — this group uses Telegram forum topics.
# Payments are only parsed in PAYMENTS_THREAD_ID; reminders + monthly report
# are posted to their respective threads.
PAYMENTS_THREAD_ID = int(os.environ.get("PAYMENTS_THREAD_ID", "4"))  # "Models payments"
EXPENSES_THREAD_ID = int(os.environ.get("EXPENSES_THREAD_ID", "5"))  # "Expenses"

DATA_FILE = "payments.json"

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---- Storage helpers ----

def load_payments():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_payments(payments):
    with open(DATA_FILE, "w") as f:
        json.dump(payments, f, indent=2)

# ---- Claude parsing ----

PARSE_PROMPT = """You extract payment data from a message. The message will contain some
combination of: a model's name, a date, a "chatting cost" amount, and an amount the model
sent (e.g. "sent Jake"). Formats vary slightly.

Return ONLY a JSON object, no other text, no markdown fences, in this exact shape:
{{"model": "<name or null>", "date": "<date as written or null>", "chatting_cost": <number or null>, "amount_sent": <number or null>}}

If the message does not contain payment data at all, return {{"model": null, "date": null, "chatting_cost": null, "amount_sent": null}}

Message:
{message}
"""

def parse_payment_message(text: str):
    resp = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": PARSE_PROMPT.format(message=text)}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if data.get("chatting_cost") is None or data.get("amount_sent") is None:
        return None
    return data

# ---- Telegram handlers ----

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    if update.message.message_thread_id != PAYMENTS_THREAD_ID:
        return  # only parse payment messages posted in the "Models payments" topic
    text = update.message.text
    if not text:
        return

    parsed = parse_payment_message(text)
    if not parsed:
        return  # not a payment message, ignore silently

    chatting_cost = float(parsed["chatting_cost"])
    amount_sent = float(parsed["amount_sent"])
    total_after = round(amount_sent - chatting_cost, 2)
    ethan_share = round(total_after * COMMISSION_SPLIT, 2)

    entry = {
        "model": parsed.get("model"),
        "date": parsed.get("date") or datetime.now().strftime("%Y-%m-%d"),
        "chatting_cost": chatting_cost,
        "amount_sent": amount_sent,
        "total_after_chatting": total_after,
        "ethan_share": ethan_share,
        "logged_at": datetime.now().isoformat(),
        "month": datetime.now().strftime("%Y-%m"),
    }

    payments = load_payments()
    payments.append(entry)
    save_payments(payments)

    reply = (
        f"Logged ✅\n"
        f"Chatting cost: {chatting_cost:.2f}\n"
        f"{'Sent Jake' if not entry['model'] else entry['model'] + ' sent Jake'}: {amount_sent:.2f}\n"
        f"Total after chatting: {total_after:.2f}\n"
        f"Ethan share: {ethan_share:.2f}"
    )
    await update.message.reply_text(reply, message_thread_id=PAYMENTS_THREAD_ID)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a report: /report or /report 2026-07"""
    args = context.args
    month = args[0] if args else datetime.now().strftime("%Y-%m")
    text = build_monthly_report(month)
    await update.message.reply_text(text, message_thread_id=PAYMENTS_THREAD_ID)

def build_monthly_report(month: str) -> str:
    payments = [p for p in load_payments() if p["month"] == month]
    if not payments:
        return f"No payments logged for {month}."

    lines = []
    total_share = 0.0
    for p in payments:
        label = p["date"]
        lines.append(f"{label}")
        lines.append(f"Chatting cost: {p['chatting_cost']:.2f}")
        sender = f"{p['model']} sent Jake" if p.get("model") else "Sent Jake"
        lines.append(f"{sender}: {p['amount_sent']:.2f}")
        lines.append(f"Total after chatting: {p['total_after_chatting']:.2f}")
        lines.append(f"Ethan share: {p['ethan_share']:.2f}")
        lines.append("")
        total_share += p["ethan_share"]

    lines.append(f"Total Ethan share: {total_share:.2f}")
    return "\n".join(lines)

# ---- Scheduled jobs ----

async def send_monthly_report(app: Application):
    month = datetime.now().strftime("%Y-%m")
    text = build_monthly_report(month)
    await app.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=PAYMENTS_THREAD_ID,
        text=f"📊 Monthly Report — {month}\n\n{text}",
    )

async def send_expense_reminder(app: Application):
    await app.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=EXPENSES_THREAD_ID,
        text="💰 Reminder: log this week's expenses in this topic.",
    )

# ---- Main ----

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()
    # Monthly report on the 1st at 9am
    scheduler.add_job(lambda: asyncio.create_task(send_monthly_report(app)), "cron", day=1, hour=9)
    # Weekly expense reminder, e.g. every Monday at 9am — adjust as needed
    scheduler.add_job(lambda: asyncio.create_task(send_expense_reminder(app)), "cron", day_of_week="mon", hour=9)
    scheduler.start()

    app.run_polling()

if __name__ == "__main__":
    main()
