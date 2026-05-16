#!/usr/bin/env python3
"""
Polls Telegram for new messages and replies with a solar forecast.

Deduplication strategy: only process messages received in the last 7 minutes.
Since the cron runs every 5 minutes, each message is processed by at most one run.
No external state storage needed.

Supported commands (anywhere in the message):
  today / now        → today's forecast
  tomorrow           → tomorrow's forecast
  YYYY-MM-DD         → forecast for that specific date
  help               → usage instructions
"""

import os
import sys
import datetime
import urllib.request
import urllib.parse
import json
import re

from forecast import build_message, send_telegram

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Only process messages younger than this (seconds). Must be > cron interval (300s).
MAX_MESSAGE_AGE_SECS = 7 * 60

HELP_TEXT = (
    "📋 <b>Solar Forecast Bot — Commands</b>\n\n"
    "  <code>today</code>        → today's forecast\n"
    "  <code>tomorrow</code>     → tomorrow's forecast\n"
    "  <code>2026-05-20</code>   → forecast for a specific date\n\n"
    "The daily forecast is sent automatically every evening at 20:00 SAST."
)


def get_updates() -> list:
    params = {"timeout": 0, "limit": 20}
    url = "https://api.telegram.org/bot{}/getUpdates?{}".format(
        TELEGRAM_BOT_TOKEN, urllib.parse.urlencode(params)
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data["result"]


def parse_date(text: str) -> datetime.date | None:
    """Extract a target date from free-form text."""
    text = text.strip().lower()
    today = datetime.date.today()

    if re.search(r'\btoday\b|\bnow\b', text):
        return today
    if re.search(r'\btomorrow\b', text):
        return today + datetime.timedelta(days=1)

    # Look for YYYY-MM-DD anywhere in the text
    match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if match:
        try:
            return datetime.date.fromisoformat(match.group(1))
        except ValueError:
            pass

    return None


def reply(chat_id: str, text: str):
    send_telegram(TELEGRAM_BOT_TOKEN, chat_id, text)


def main():
    now_ts = datetime.datetime.utcnow().timestamp()
    updates = get_updates()

    if not updates:
        print("No messages.")
        return

    processed = 0
    for update in updates:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue

        # Skip messages older than MAX_MESSAGE_AGE_SECS
        age = now_ts - msg.get("date", 0)
        if age > MAX_MESSAGE_AGE_SECS:
            continue

        sender_chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        print(f"Message from {sender_chat_id} ({int(age)}s ago): {text!r}")

        # Only respond to the authorised chat
        if sender_chat_id != TELEGRAM_CHAT_ID:
            print(f"  → Ignored (unauthorised chat)")
            continue

        if not text:
            continue

        processed += 1
        lower = text.lower()

        if re.search(r'\bhelp\b|^/help$|^/start$', lower):
            reply(sender_chat_id, HELP_TEXT)
            continue

        date = parse_date(lower)
        if date is None:
            reply(sender_chat_id,
                  "❓ Didn't recognise that. Send <code>today</code>, "
                  "<code>tomorrow</code>, or a date like <code>2026-05-20</code>.\n"
                  "Send <code>help</code> for all commands.")
            continue

        if date < datetime.date.today() - datetime.timedelta(days=1):
            reply(sender_chat_id,
                  "⚠️ Open-Meteo only provides forecasts, not historical data. "
                  "Please pick today or a future date.")
            continue

        try:
            print(f"  → Fetching forecast for {date} …")
            reply(sender_chat_id, build_message(date))
            print("  → Sent.")
        except Exception as e:
            reply(sender_chat_id, f"⚠️ Error fetching forecast: {e}")

    if processed == 0:
        print("No recent messages to process.")


if __name__ == "__main__":
    main()
