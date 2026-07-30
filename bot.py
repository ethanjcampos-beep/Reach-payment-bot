"""
Reach Models Telegram Bot
- Logs any money-related message posted in the "Models payments" topic
- Auto-watches the "Expenses" topic (no @ needed) for purchases — logs them
  immediately even with no price or date; a split is only calculated when
  explicitly stated (e.g. specific quantities), never assumed
- /report shows payments + purchases for a month on demand
- Posts a purchases summary automatically on the last day of each month
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
import calendar
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
COMMISSION_SPLIT = float(os.environ.get("COMMISSION_SPLIT", "0.20"))  # Ethan's 20% of payments
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

PARSE_PAYMENT_PROMPT = """You extract money-related info from a message posted in a payments log chat.
Messages could be in many formats — structured ("Chatting cost: 200 / Maddie sent Jake: 1500"),
a single line ("Kai sent 3000"), a note ("Maddie owes 38292"), or "Ethan got 200$".

Return ONLY a JSON object, no other text, no markdown fences, in this exact shape:
{{"is_money_related": true/false, "model": "<name or null>", "date": "<date as written, or null if not mentioned>", "chatting_cost": <number or null>, "amount_sent": <number or null>, "note": "<short plain description of what happened, always fill this in if is_money_related is true>"}}

Rules:
- is_money_related is true for ANY message describing a payment, amount owed, amount received, or similar — even a single line with just a name and a number.
- chatting_cost and amount_sent are only for those SPECIFIC concepts. If the message doesn't mention one of them, leave it null — do not guess.
- note should always be filled in when is_money_related is true, in plain English, e.g. "Maddie owes 38292", "Kai sent 3000", "Ethan received 200".
- If the message is unrelated to money entirely (a greeting, a test, banter), return is_money_related: false and note: null.

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
    if not data.get("is_money_related"):
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

# ---- Claude parsing: Expenses topic auto-watch (no @ mention needed) ----

CLASSIFY_EXPENSE_PROMPT = """A message was posted in a shared business expenses chat between two partners,
Ethan and Jake. Determine if it's expense-related and extract details.

Two kinds of expense messages:
1. Intent/purchase announcement with no price yet, e.g. "Jake is buying 10 X accounts, I'm getting 5 of them"
   -> description summarizes it, amount is null.
2. A price being reported, possibly for something mentioned earlier, e.g. "X accounts purchased $350"
   -> description is the item, amount is the number.

Only extract a split (ethan_ratio) when the message EXPLICITLY states how it's divided between
the two of them — e.g. quantities ("I'm getting 5 of the 10", "I got 2, Jake got 8"), an explicit
fraction/percentage, or a directly stated dollar amount for one person. Do NOT assume or default to
any split (not even 50/50) when the message doesn't say how it's divided — leave ethan_ratio null
in that case, and it will just be logged as a plain expense with no split.

Return ONLY a JSON object, no other text, no markdown fences:
{{"is_expense_related": true/false, "description": "<short description or null>", "amount": <number or null>, "ethan_ratio": <fraction 0-1 or null>}}

Examples:
"Jake is buying 10 X accounts, I'm getting 5 of them" -> ethan_ratio: 0.5 (5 of 10 explicitly stated)
"We bought 10 X accounts, I only wanted 2 and Jake wanted 8" -> ethan_ratio: 0.2
"X accounts purchased $350" -> ethan_ratio: null (no division stated)
"Bought a new laptop for the office, $1200" -> ethan_ratio: null

If the message is clearly unrelated to a purchase/expense (casual chat, a test, banter), return is_expense_related: false.

Message:
{message}
"""

def classify_expense_message(text: str):
    try:
        resp = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": CLASSIFY_EXPENSE_PROMPT.format(message=text)}],
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception:
        logger.exception("Failed to classify expense message")
        return None

# ---- Telegram handlers: payment logging (Models payments topic) ----

async def handle_payment_message(update: Update, text: str):
    parsed = parse_payment_message(text)
    logger.info("Parsed payment result: %s", parsed)
    if not parsed:
        return  # not money-related, ignore silently

    chatting_cost = parsed.get("chatting_cost")
    amount_sent = parsed.get("amount_sent")
    chatting_cost = float(chatting_cost) if chatting_cost is not None else None
    amount_sent = float(amount_sent) if amount_sent is not None else None

    total_after = None
    ethan_share = None
    jake_share = None
    if chatting_cost is not None and amount_sent is not None:
        total_after = round(amount_sent - chatting_cost, 2)
        ethan_share = round(total_after * COMMISSION_SPLIT, 2)
        jake_share = round(total_after - ethan_share, 2)

    entry = {
        "model": parsed.get("model"),
        "date": parsed.get("date") or datetime.now().strftime("%Y-%m-%d"),
        "chatting_cost": chatting_cost,
        "amount_sent": amount_sent,
        "total_after_chatting": total_after,
        "ethan_share": ethan_share,
        "jake_share": jake_share,
        "note": parsed.get("note"),
        "raw_text": text,
        "logged_at": datetime.now().isoformat(),
        "month": datetime.now().strftime("%Y-%m"),
    }

    payments = load_payments()
    payments.append(entry)
    save_payments(payments)

    lines = ["Logged \u2705"]
    if parsed.get("note"):
        lines.append(parsed["note"])
    if chatting_cost is not None:
        lines.append(f"Chatting cost: {chatting_cost:.2f}")
    if amount_sent is not None:
        sender_label = f"{entry['model']} sent Jake" if entry.get("model") else "Sent Jake"
        lines.append(f"{sender_label}: {amount_sent:.2f}")
    if total_after is not None:
        lines.append(f"Total after chatting: {total_after:.2f}")
        lines.append(f"Ethan's share: {ethan_share:.2f}")
        lines.append(f"Jake's share: {jake_share:.2f}")
    else:
        lines.append("No other variables documented \u2014 nothing to calculate.")

    await update.message.reply_text("\n".join(lines), message_thread_id=PAYMENTS_THREAD_ID)

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
        amount = float(amount)
        entry = {
            "description": description,
            "amount": amount,
            "ethan_ratio": None,
            "ethan_share": None,
            "jake_share": None,
            "logged_at": datetime.now().isoformat(),
            "month": datetime.now().strftime("%Y-%m"),
        }
        expenses = load_expenses()
        expenses.append(entry)
        save_expenses(expenses)
        await update.message.reply_text(
            f"Expense logged \u2705\n{description}: {amount:.2f}",
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

async def handle_expense_thread_message(update: Update, text: str):
    """Auto-watches the Expenses topic — no @ mention needed. Handles two-step flow:
    an intent/purchase message with no price yet, followed later by a price message
    that fills in the most recent pending (price-less) entry. A split is only computed
    when the message explicitly states how it's divided — never assumed."""
    thread_id = update.message.message_thread_id
    classified = classify_expense_message(text)
    logger.info("Classified expense-thread message: %s", classified)
    if not classified or not classified.get("is_expense_related"):
        return  # not expense-related, ignore silently

    description = classified.get("description") or text
    amount = classified.get("amount")
    ethan_ratio = classified.get("ethan_ratio")
    expenses = load_expenses()

    if amount is not None:
        amount = float(amount)
        pending = [e for e in expenses if e.get("amount") is None]
        if pending:
            entry = pending[-1]
            entry["amount"] = amount
            # carry forward a ratio stated earlier if this message doesn't restate one
            ratio = ethan_ratio if ethan_ratio is not None else entry.get("ethan_ratio")
            entry["ethan_ratio"] = ratio
            if ratio is not None:
                entry["ethan_share"] = round(amount * ratio, 2)
                entry["jake_share"] = round(amount - entry["ethan_share"], 2)
            entry["month"] = datetime.now().strftime("%Y-%m")
        else:
            entry = {
                "description": description,
                "amount": amount,
                "ethan_ratio": ethan_ratio,
                "ethan_share": round(amount * ethan_ratio, 2) if ethan_ratio is not None else None,
                "jake_share": round(amount * (1 - ethan_ratio), 2) if ethan_ratio is not None else None,
                "logged_at": datetime.now().isoformat(),
                "month": datetime.now().strftime("%Y-%m"),
            }
            expenses.append(entry)
        save_expenses(expenses)

        lines = ["Expense logged \u2705", entry["description"], f"Amount: {entry['amount']:.2f}"]
        if entry.get("ethan_share") is not None:
            lines.append(f"Ethan's share: {entry['ethan_share']:.2f}")
            lines.append(f"Jake's share: {entry['jake_share']:.2f}")
        await update.message.reply_text("\n".join(lines), message_thread_id=thread_id)
    else:
        entry = {
            "description": description,
            "amount": None,
            "ethan_ratio": ethan_ratio,
            "ethan_share": None,
            "jake_share": None,
            "logged_at": datetime.now().isoformat(),
            "month": datetime.now().strftime("%Y-%m"),
        }
        expenses.append(entry)
        save_expenses(expenses)
        reply = ["Purchase logged \u2705", description]
        if ethan_ratio is not None:
            reply.append(f"Ethan: {ethan_ratio*100:.0f}% \u2014 Jake: {(1-ethan_ratio)*100:.0f}%")
        reply.append("No price yet \u2014 add one anytime and I'll calculate the split. Otherwise this'll show up in the monthly summary.")
        await update.message.reply_text("\n".join(reply), message_thread_id=thread_id)

def build_range_report(start_date: str, end_date: str) -> str:
    payments = load_payments()
    expenses = load_expenses()

    def in_range(iso_logged_at):
        d = iso_logged_at[:10]
        return start_date <= d <= end_date

    p_in_range = [p for p in payments if in_range(p["logged_at"])]
    e_in_range = [e for e in expenses if in_range(e["logged_at"])]

    total_chatting = sum(p["chatting_cost"] for p in p_in_range if p.get("chatting_cost") is not None)
    total_sent = sum(p["amount_sent"] for p in p_in_range if p.get("amount_sent") is not None)
    total_after = sum(p["total_after_chatting"] for p in p_in_range if p.get("total_after_chatting") is not None)
    total_ethan_share = sum(p["ethan_share"] for p in p_in_range if p.get("ethan_share") is not None)
    total_jake_share = sum(p["jake_share"] for p in p_in_range if p.get("jake_share") is not None)
    total_expenses = sum(e["amount"] for e in e_in_range if e.get("amount") is not None)

    lines = [f"Report: {start_date} to {end_date}", ""]
    lines.append(f"Payments logged: {len(p_in_range)}")
    lines.append(f"Total chatting cost: {total_chatting:.2f}")
    lines.append(f"Total sent: {total_sent:.2f}")
    lines.append(f"Total after chatting: {total_after:.2f}")
    lines.append(f"Ethan's share: {total_ethan_share:.2f}")
    lines.append(f"Jake's share: {total_jake_share:.2f}")
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

    if update.message.message_thread_id == PAYMENTS_THREAD_ID and not mentioned:
        await handle_payment_message(update, text)
        return

    if update.message.message_thread_id == EXPENSES_THREAD_ID and not mentioned:
        await handle_expense_thread_message(update, text)
        return

    if mentioned:
        await handle_mention(update, text)

# ---- Commands ----

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a report: /report or /report 2026-07"""
    args = context.args
    month = args[0] if args else datetime.now().strftime("%Y-%m")
    thread_id = update.message.message_thread_id
    payment_text = build_monthly_report(month)
    expense_text = build_expense_summary(month)
    full_text = f"{payment_text}\n\n\U0001F9FE Purchases \u2014 {month}\n\n{expense_text}"
    await update.message.reply_text(full_text, message_thread_id=thread_id)

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
        detail = entry.get("note") or entry.get("raw_text") or "entry"
        lines.append(f"- {entry['date']}: {detail}")
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
        amt = f"{entry['amount']:.2f}" if entry.get("amount") is not None else "no price logged yet"
        lines.append(f"- {entry['description']}: {amt}")
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
    total_ethan_share = 0.0
    total_jake_share = 0.0
    any_complete = False
    for p in payments:
        lines.append(f"{p['date']}")
        if p.get("note"):
            lines.append(p["note"])
        if p.get("chatting_cost") is not None:
            lines.append(f"Chatting cost: {p['chatting_cost']:.2f}")
        if p.get("amount_sent") is not None:
            sender = f"{p['model']} sent Jake" if p.get("model") else "Sent Jake"
            lines.append(f"{sender}: {p['amount_sent']:.2f}")
        if p.get("total_after_chatting") is not None:
            lines.append(f"Total after chatting: {p['total_after_chatting']:.2f}")
            lines.append(f"Ethan's share: {p['ethan_share']:.2f}")
            lines.append(f"Jake's share: {p['jake_share']:.2f}")
            total_ethan_share += p["ethan_share"]
            total_jake_share += p["jake_share"]
            any_complete = True
        else:
            lines.append("No other variables documented \u2014 nothing to calculate.")
        lines.append("")

    if any_complete:
        lines.append(f"Total Ethan's share: {total_ethan_share:.2f}")
        lines.append(f"Total Jake's share: {total_jake_share:.2f}")
    else:
        lines.append("Totals: N/A \u2014 no entries this period had both a chatting cost and an amount sent.")
    return "\n".join(lines)

def build_expense_summary(month: str) -> str:
    expenses = [e for e in load_expenses() if e["month"] == month]
    if not expenses:
        return f"No purchases logged for {month}."

    lines = []
    for e in expenses:
        lines.append(e["description"])
        if e.get("amount") is not None:
            lines.append(f"Amount: {e['amount']:.2f}")
            if e.get("ethan_share") is not None:
                lines.append(f"Ethan's share: {e['ethan_share']:.2f}")
                lines.append(f"Jake's share: {e['jake_share']:.2f}")
        else:
            lines.append("No price logged yet")
        lines.append("")
    return "\n".join(lines).strip()

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

async def check_month_end_expense_summary(app: Application):
    now = datetime.now()
    last_day_of_month = calendar.monthrange(now.year, now.month)[1]
    if now.day != last_day_of_month:
        return
    month = now.strftime("%Y-%m")
    text = build_expense_summary(month)
    await app.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=EXPENSES_THREAD_ID,
        text=f"\U0001F9FE Purchases this month \u2014 {month}\n\n{text}",
    )

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
    scheduler.add_job(lambda: asyncio.create_task(check_month_end_expense_summary(app)), "cron", hour=9, minute=30)
    scheduler.start()

    logger.info("Bot starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
