#!/usr/bin/env python3
"""
Polls Telegram for new messages and replies with a solar forecast.

Supported commands:
  today              → today's forecast
  tomorrow           → tomorrow's forecast
  YYYY-MM-DD         → forecast for that specific date
  help               → usage instructions

The last-seen update_id is stored as a GitHub Actions variable (LAST_UPDATE_ID)
so messages are never processed twice across runs.
"""

import os
import sys
import datetime
import urllib.request
import urllib.parse
import json

from forecast import build_message, send_telegram

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GH_TOKEN           = os.environ.get("GH_TOKEN", "")
GH_REPO            = os.environ.get("GH_REPO", "")
LAST_UPDATE_ID     = int(os.environ.get("LAST_UPDATE_ID", "0") or "0")

HELP_TEXT = (
    "📋 <b>Solar Forecast Bot — Commands</b>\n\n"
    "  <code>today</code>        → today's forecast\n"
    "  <code>tomorrow</code>     → tomorrow's forecast\n"
    "  <code>2026-05-20</code>   → forecast for a specific date\n\n"
    "The daily forecast is sent automatically every evening at 20:00 SAST."
)


def get_updates(offset: int) -> list:
    params = {"offset": offset + 1, "timeout": 0, "limit": 20}
    url = "https://api.telegram.org/bot{}/getUpdates?{}".format(
        TELEGRAM_BOT_TOKEN, urllib.parse.urlencode(params)
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data["result"]


def save_last_update_id(update_id: int):
    if not GH_TOKEN or not GH_REPO:
        print(f"[skip] GH_TOKEN/GH_REPO not set — not saving update_id {update_id}")
        return
    url = f"https://api.github.com/repos/{GH_REPO}/actions/variables/LAST_UPDATE_ID"
    payload = json.dumps({"name": "LAST_UPDATE_ID", "value": str(update_id)}).encode()
    req = urllib.request.Request(url, data=payload, method="PATCH",
                                 headers={
                                     "Authorization": f"Bearer {GH_TOKEN}",
                                     "Accept": "application/vnd.github+json",
                                     "Content-Type": "application/json",
                                     "X-GitHub-Api-Version": "2022-11-28",
                                 })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                print(f"[warn] Unexpected status saving update_id: {resp.status}")
    except Exception as e:
        # Variable may not exist yet on first run — try POST instead
        url2 = f"https://api.github.com/repos/{GH_REPO}/actions/variables"
        payload2 = json.dumps({"name": "LAST_UPDATE_ID", "value": str(update_id)}).encode()
        req2 = urllib.request.Request(url2, data=payload2, method="POST",
                                      headers={
                                          "Authorization": f"Bearer {GH_TOKEN}",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                      })
        with urllib.request.urlopen(req2, timeout=10):
            pass


def parse_date(text: str) -> datetime.date | None:
    text = text.strip().lower()
    today = datetime.date.today()
    if text in ("today", "now"):
        return today
    if text in ("tomorrow",):
        return today + datetime.timedelta(days=1)
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def reply(chat_id: str, text: str):
    send_telegram(TELEGRAM_BOT_TOKEN, chat_id, text)


def main():
    updates = get_updates(LAST_UPDATE_ID)
    if not updates:
        print("No new messages.")
        return

    last_id = LAST_UPDATE_ID
    for update in updates:
        update_id = update["update_id"]
        last_id = max(last_id, update_id)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue

        sender_chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        print(f"Message from {sender_chat_id}: {text!r}")

        # Only respond to the authorised chat
        if sender_chat_id != TELEGRAM_CHAT_ID:
            print(f"  → Ignored (unauthorised chat {sender_chat_id})")
            continue

        if not text:
            continue

        lower = text.lower()
        if lower in ("help", "/help", "/start"):
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
            reply(sender_chat_id, "⚠️ Open-Meteo only provides forecasts, not historical data. "
                                  "Please pick today or a future date.")
            continue

        try:
            print(f"  → Fetching forecast for {date} …")
            msg_out = build_message(date)
            reply(sender_chat_id, msg_out)
            print("  → Sent.")
        except Exception as e:
            reply(sender_chat_id, f"⚠️ Error fetching forecast: {e}")

    if last_id > LAST_UPDATE_ID:
        save_last_update_id(last_id)
        print(f"Saved LAST_UPDATE_ID={last_id}")


if __name__ == "__main__":
    main()
