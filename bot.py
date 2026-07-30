"""
Reach Models Telegram Bot
- Logs payment messages posted in the "Models payments" topic
- Lets you @ the bot anywhere to: log an expense, schedule an expense
  reminder, or ask for a spending report over any date range
- Posts a monthly payments report automatically on the 1st
- Posts due expense reminders automatically each morning

Setup instructions are in README.md
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reach-bot")

# ---- CONFIG (set these as environment variables on your host) ----
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])  # the Telegram group's chat ID
COMMISSION_SPLIT = float(os.environ.get("COMMISSION_SPLIT", "0.20"))  # Ethan's 20%
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Reachaccountantbot")  # without the @

# Topic (thread) IDs — this group uses Telegram forum topics.
PAYMENTS_THREAD_ID = int(os.environ.get("PAYMENTS_THREAD_ID", "4"))  # "Models payments"
EXPENSES_THREAD_ID = int(os.environ.get("EXPENSES_THREAD_ID", "5"))  # "Expenses"

PAYMENTS_FILE = "payments.json"
EXPENSES_FILE = "expenses.json"
REMINDERS_FILE = "reminders.json"

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---- Storage helpers ----

def load_json(path):
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_payments():
    return load_json(PAYMENTS_FILE)

def save_payments(data):
    save_json(PAYMENTS_FILE, data)

def load_expenses():
    return load_json(EXPENSES_FILE)

def save_expenses(data):
    save_json(EXPENSES_FILE, data)

def load_reminders():
    return load_json(REMINDERS_FILE)

def save_reminders(data):
    save_json(REMINDERS_FILE, data)

# ---- Claude parsing: payment messages (Models payments topic) ----

PARSE_PAYMENT_PROMPT = """You extract payment data from a message. The message will contain some
combination of: a model's name, a date, a "chatting cost" amount, and an amount the model
sent (e.g. "sent Jake"). Formats vary slightly.

Return ONLY a JSON object, no other text, no markdown fences, in this exact shape:
{{"model": "<name or null>", "date": "<date as written or null>", "chatting_cost": <number or null>, "amount_sent": <number or null>}}

If the message does not contain payment data at all, return {{"model": null, "date": null, "chatting_cost": null, "amount_sent": null}}

Message:
{message}
"""

def parse_payment_message(text: str):
    try:
        resp = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": PARSE_PAYMENT_PROMPT.format(message=text)}],
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
    except Exception:
        logger.exception("Failed to parse payment message")
        return None
    if data.get("chatting_cost") is None or data.get("amount_sent") is None:
        return None
    return data

# ---- Claude parsing: @ mention intent classification ----

CLASSIFY_PROMPT = """Today's date is {today}. A user @ mentioned a Telegram bot with the message below.
Classify the intent and extract fields. Return ONLY a JSON object, no other text, no markdown fences.

Possible intents:
- "expense_log": they're reporting a purchase/expense to log now. Fields: description (string), amount (number).
- "expense_reminder": they want to be reminded to pay something on a future date. Fields: description (string), due_date (YYYY-MM-DD, resolved relative to today's date).
- "report_query": they're asking for spending/earnings totals over a date range. Fields: start_date (YYYY-MM-DD), end_date (YYYY-MM-DD), resolved relative to today's date. If a year isn't specified, assume the current year.
- "unknown": doesn't clearly match any of the above.

Return shape:
{{"intent": "<intent>", "description": "<string or null>", "amount": <number or null>, "due_date": "<YYYY-MM-DD or null>", "start_date": "<YYYY-MM-DD or null>", "end_date": "<YYYY-MM-DD or null>"}}

Message:
{message}
"""

def classify_mention(text: str):
    try:
        resp = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(
                today=datetime.now().strftime("%Y-%m-%d"), message=text
            )}],
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception:
        logger.exception("Failed to classify mention")
        return None

# ---- Telegram handlers: payment logging (Models payments topic) ----

async def handle_payment_message(update: Update, text: str):
    parsed = parse_payment_message(text)
    logger.info("Parsed payment result: %s", parsed)
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
        f"Logged \u2705\n"
        f"Chatting cost: {chatting_cost:.2f}\n"
        f"{'Sent Jake' if not entry['model'] else entry['model'] + ' sent Jake'}: {amount_sent:.2f}\n"
        f"Total after chatting: {total_after:.2f}\n"
        f"Ethan share: {ethan_share:.2f}"
    )
    await update.message.reply_text(reply, message_thread_id=PAYMENTS_THREAD_ID)

# ---- Telegram handlers: @ mention (expense log / reminder / report query) ----

async def handle_mention(update: Update, text: str):
    thread_id = update.message.message_thread_id
    classified = classify_mention(text)
    logger.info("Classified mention: %s", classified)
    if not classified:
        await update.message.reply_text("Sorry, I couldn't understand that \u2014 try rephrasing.", message_thread_id=thread_id)
        return

    intent = classified.get("intent")

    if intent == "expense_log":
        description = classified.get("description") or "Expense"
        amount = classified.get("amount")
        if amount is None:
            await update.message.reply_text("I caught the expense but not the amount \u2014 try including a dollar figure.", message_thread_id=thread_id)
            return
        entry = {
            "description": description,
            "amount": float(amount),
            "logged_at": datetime.now().isoformat(),
            "month": datetime.now().strftime("%Y-%m"),
        }
        expenses = load_expenses()
        expenses.append(entry)
        save_expenses(expenses)
        await update.message.reply_text(
            f"Expense logged \u2705\n{description}: {float(amount):.2f}",
            message_thread_id=thread_id,
        )

    elif intent == "expense_reminder":
        description = classified.get("description") or "Expense"
        due_date = classified.get("due_date")
        if not due_date:
            await update.message.reply_text("I caught the expense but not a clear date \u2014 try including when it's due.", message_thread_id=thread_id)
            return
        reminder = {
            "description": description,
            "due_date": due_date,
            "created_at": datetime.now().isoformat(),
            "reminded": False,
        }
        reminders = load_reminders()
        reminders.append(reminder)
        save_reminders(reminders)
        await update.message.reply_text(
            f"Reminder set \u2705\nI'll remind you to pay \"{description}\" on {due_date}.",
            message_thread_id=thread_id,
        )

    elif intent == "report_query":
        start_date = classified.get("start_date")
        end_date = classified.get("end_date")
        if not start_date or not end_date:
            await update.message.reply_text("I couldn't work out the date range \u2014 try being more specific (e.g. \"May to June\").", message_thread_id=thread_id)
            return
        text_report = build_range_report(start_date, end_date)
        await update.message.reply_text(text_report, message_thread_id=thread_id)

    else:
        await update.message.reply_text(
            "I can log expenses, set expense reminders, or pull spending reports \u2014 try rephrasing what you need.",
            message_thread_id=thread_id,
        )

def build_range_report(start_date: str, end_date: str) -> str:
    payments = load_payments()
    expenses = load_expenses()

    def in_range(iso_logged_at):
        d = iso_logged_at[:10]
        return start_date <= d <= end_date

    p_in_range = [p for p in payments if in_range(p["logged_at"])]
    e_in_range = [e for e in expenses if in_range(e["logged_at"])]

    total_chatting = sum(p["chatting_cost"] for p in p_in_range)
    total_sent = sum(p["amount_sent"] for p in p_in_range)
    total_after = sum(p["total_after_chatting"] for p in p_in_range)
    total_share = sum(p["ethan_share"] for p in p_in_range)
    total_expenses = sum(e["amount"] for e in e_in_range)

    lines = [f"Report: {start_date} to {end_date}", ""]
    lines.append(f"Payments logged: {len(p_in_range)}")
    lines.append(f"Total chatting cost: {total_chatting:.2f}")
    lines.append(f"Total sent: {total_sent:.2f}")
    lines.append(f"Total after chatting: {total_after:.2f}")
    lines.append(f"Ethan share: {total_share:.2f}")
    lines.append("")
    lines.append(f"Expenses logged: {len(e_in_range)}")
    lines.append(f"Total expenses: {total_expenses:.2f}")
    lines.append("")
    lines.append(f"Net (after chatting, minus expenses): {total_after - total_expenses:.2f}")
    return "\n".join(lines)

# ---- Master message handler ----

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    logger.info(
        "Received message: chat_id=%s thread_id=%s text=%r",
        update.effective_chat.id if update.effective_chat else None,
        update.message.message_thread_id,
        text,
    )
    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    mentioned = f"@{BOT_USERNAME}".lower() in text.lower()

    if mentioned:
        await handle_mention(update, text)
        return

    if update.message.message_thread_id == PAYMENTS_THREAD_ID:
        await handle_payment_message(update, text)

# ---- Commands ----

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a report: /report or /report 2026-07"""
    args = context.args
    month = args[0] if args else datetime.now().strftime("%Y-%m")
    text = build_monthly_report(month)
    await update.message.reply_text(text, message_thread_id=PAYMENTS_THREAD_ID)

async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove the most recently logged payment entry: /undo, or /undo 3 to remove the last 3"""
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    args = context.args
    count = int(args[0]) if args and args[0].isdigit() else 1

    payments = load_payments()
    if not payments:
        await update.message.reply_text("Nothing to undo \u2014 no payments logged.", message_thread_id=PAYMENTS_THREAD_ID)
        return

    removed = payments[-count:]
    remaining = payments[:-count]
    save_payments(remaining)

    lines = [f"Removed {len(removed)} payment entry(ies):"]
    for entry in removed:
        lines.append(f"- {entry['date']}: chatting cost {entry['chatting_cost']:.2f}, sent {entry['amount_sent']:.2f}")
    await update.message.reply_text("\n".join(lines), message_thread_id=PAYMENTS_THREAD_ID)

async def undo_expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove the most recently logged expense: /undo_expense, or /undo_expense 2 for the last 2"""
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    thread_id = update.message.message_thread_id
    args = context.args
    count = int(args[0]) if args and args[0].isdigit() else 1

    expenses = load_expenses()
    if not expenses:
        await update.message.reply_text("Nothing to undo \u2014 no expenses logged.", message_thread_id=thread_id)
        return

    removed = expenses[-count:]
    remaining = expenses[:-count]
    save_expenses(remaining)

    lines = [f"Removed {len(removed)} expense(s):"]
    for entry in removed:
        lines.append(f"- {entry['description']}: {entry['amount']:.2f}")
    await update.message.reply_text("\n".join(lines), message_thread_id=thread_id)

async def undo_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove the most recently set reminder: /undo_reminder, or /undo_reminder 2 for the last 2"""
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    thread_id = update.message.message_thread_id
    args = context.args
    count = int(args[0]) if args and args[0].isdigit() else 1

    reminders = load_reminders()
    if not reminders:
        await update.message.reply_text("Nothing to undo \u2014 no reminders set.", message_thread_id=thread_id)
        return

    removed = reminders[-count:]
    remaining = reminders[:-count]
    save_reminders(remaining)

    lines = [f"Removed {len(removed)} reminder(s):"]
    for entry in removed:
        lines.append(f"- {entry['description']} (due {entry['due_date']})")
    await update.message.reply_text("\n".join(lines), message_thread_id=thread_id)

def build_monthly_report(month: str) -> str:
    payments = [p for p in load_payments() if p["month"] == month]
    if not payments:
        return f"No payments logged for {month}."

    lines = []
    total_share = 0.0
    for p in payments:
        lines.append(f"{p['date']}")
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
    now = datetime.now()
    prev_month_last_day = now.replace(day=1) - timedelta(days=1)
    month = prev_month_last_day.strftime("%Y-%m")
    text = build_monthly_report(month)
    await app.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=PAYMENTS_THREAD_ID,
        text=f"\U0001F4CA Monthly Report \u2014 {month}\n\n{text}",
    )

async def check_due_reminders(app: Application):
    today = datetime.now().strftime("%Y-%m-%d")
    reminders = load_reminders()
    changed = False
    for r in reminders:
        if not r["reminded"] and r["due_date"] <= today:
            await app.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                message_thread_id=EXPENSES_THREAD_ID,
                text=f"\U0001F4B0 Reminder: pay \"{r['description']}\" \u2014 due {r['due_date']}",
            )
            r["reminded"] = True
            changed = True
    if changed:
        save_reminders(reminders)

# ---- Main ----

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CommandHandler("undo_expense", undo_expense_command))
    app.add_handler(CommandHandler("undo_reminder", undo_reminder_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(send_monthly_report(app)), "cron", day=1, hour=9)
    scheduler.add_job(lambda: asyncio.create_task(check_due_reminders(app)), "cron", hour=9)
    scheduler.start()

    logger.info("Bot starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
