# Reach Models Payment Bot — Setup Guide

## What it does
- Watches your Telegram group for payment messages (chatting cost + amount sent)
- Parses them automatically, even with slightly different wording
- Replies in-chat confirming what it logged
- Posts a full monthly report on the 1st of each month, in your format
- Posts a weekly reminder to log expenses
- `/report` command to pull a report on demand (e.g. `/report 2026-07`)

## Step 1: Create the Telegram bot
1. Open Telegram, search for **@BotFather**
2. Send `/newbot`, follow the prompts (choose a name + username)
3. BotFather gives you a **token** — save it, you'll need it as `TELEGRAM_BOT_TOKEN`
4. Add your new bot to your group chat
5. In group settings, make sure the bot has permission to read messages
   (Telegram bots need "Group Privacy" turned OFF to read all messages —
   message @BotFather with `/setprivacy`, pick your bot, choose **Disable**)

## Step 2: Get your group's Chat ID
1. Add the bot **@userinfobot** to your group temporarily, or
2. Send any message in the group, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   in a browser — look for `"chat":{"id": -100xxxxxxxxxx, ...}`
3. Save that number as `GROUP_CHAT_ID` (it'll be negative for groups)

Your group uses Telegram **topics** ("Models payments" and "Expenses"), so the
bot is scoped per-topic:
- Payments are only parsed when posted in the "Models payments" topic — set
  its thread ID as `PAYMENTS_THREAD_ID`
- Expense reminders post into the "Expenses" topic — set its thread ID as
  `EXPENSES_THREAD_ID`

You can find these thread IDs in the same `getUpdates` JSON, under
`"message_thread_id"` for a message sent in each topic.

## Step 3: Get an Anthropic API key
1. Go to console.anthropic.com → API Keys → Create Key
2. Save it as `ANTHROPIC_API_KEY`

## Step 4: Deploy (Railway — simplest option)
1. Create a free account at railway.app
2. New Project → Deploy from GitHub repo (push this folder to a new GitHub repo first),
   or use "Empty Project" and upload these files via Railway's CLI
3. In Railway's project settings → Variables, add:
   - `TELEGRAM_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `GROUP_CHAT_ID`
   - `PAYMENTS_THREAD_ID` (defaults to 4 — "Models payments")
   - `EXPENSES_THREAD_ID` (defaults to 5 — "Expenses")
   - `COMMISSION_SPLIT` (optional, defaults to 0.20)
4. Railway auto-detects Python and installs `requirements.txt`
5. Set the start command to: `python bot.py`
6. Deploy — the bot should come online within a minute or two

## Step 5: Test it
In the group, send a message like:
```
July 20th
Chatting cost: 200
Maddie sent Jake: 1500
```
The bot should reply with the parsed breakdown within a few seconds.

## Notes
- Data is stored in `payments.json` on the server — Railway's filesystem is
  ephemeral on redeploys, so ask me to switch this to a proper database
  (e.g. free Postgres on Railway) once you're relying on this for real numbers.
- You can message costs in any reasonably similar format — Claude parsing means
  it doesn't have to match exactly.
- To change the reminder schedule (currently Monday 9am) or report day
  (currently the 1st), edit the `scheduler.add_job(...)` lines near the bottom
  of `bot.py`.
