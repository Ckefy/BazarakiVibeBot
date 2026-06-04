#!/usr/bin/env python3
"""Bazaraki subscription Telegram bot.

One-shot run, designed for cron / GitHub Actions. On each run it:
  1. pulls new Telegram updates (links the user sent, button presses);
  2. polls every active subscription and sends links to NEW listings;
  3. handles 30-day expiry (notifies, offers renew);
  4. persists everything to state.json.

Requires the TELEGRAM_BOT_TOKEN environment variable.
"""
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import requests

from scraper import fetch_listings
from storage import load_state, save_state

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = "https://api.telegram.org/bot{}".format(TOKEN)

SUB_DAYS = 30           # subscription length / renew length
MAX_SEEN = 600          # cap stored ids per subscription
MAX_SEND_PER_RUN = 15   # avoid flooding on a single run

URL_RE = re.compile(r"https?://\S+")

WELCOME = (
    "👋 Привет! Я слежу за новыми объявлениями на Bazaraki.\n\n"
    "Пришли мне <b>ссылку на поиск</b> с bazaraki.com (с нужными фильтрами — "
    "район, цена, число спален и т.д.), и я подпишу тебя на 30 дней. "
    "Как только появится новое объявление по этому поиску — пришлю ссылку.\n\n"
    "Команды:\n"
    "• /list — мои подписки\n"
    "• /help — помощь\n\n"
    "Подписку можно продлить или отменить кнопками под подтверждением."
)


def main():
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    state = load_state()
    process_updates(state)
    poll_subscriptions(state)
    save_state(state)


# --- Telegram API ------------------------------------------------------------

def tg(method, **params):
    resp = requests.post("{}/{}".format(API, method), json=params, timeout=40)
    data = resp.json()
    if not data.get("ok"):
        print("Telegram API error on {}: {}".format(method, data), file=sys.stderr)
    return data


def send(chat_id, text, keyboard=None):
    params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=False)
    if keyboard is not None:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", **params)


def sub_keyboard(sub_id):
    return [[
        {"text": "🔄 Продлить +30 дней", "callback_data": "renew:" + sub_id},
        {"text": "❌ Отменить", "callback_data": "cancel:" + sub_id},
    ]]


# --- handling incoming updates ----------------------------------------------

def process_updates(state):
    offset = state.get("update_offset", 0)
    result = tg("getUpdates", offset=offset + 1, timeout=0, allowed_updates=["message", "callback_query"])
    updates = result.get("result", []) if result.get("ok") else []

    for upd in updates:
        state["update_offset"] = max(state["update_offset"], upd["update_id"])
        if "message" in upd:
            handle_message(state, upd["message"])
        elif "callback_query" in upd:
            handle_callback(state, upd["callback_query"])


def handle_message(state, message):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        send(chat_id, WELCOME)
        return
    if text.startswith("/list"):
        send_subscription_list(state, chat_id)
        return

    match = URL_RE.search(text)
    if match and "bazaraki.com" in match.group(0):
        create_subscription(state, chat_id, match.group(0).rstrip(".,);"))
        return

    send(chat_id, "Пришли ссылку на поиск с bazaraki.com или используй /help.")


def handle_callback(state, callback):
    data = callback.get("data", "")
    chat_id = callback["message"]["chat"]["id"]
    cb_id = callback["id"]

    action, _, sub_id = data.partition(":")
    sub = _find_sub(state, sub_id)

    if sub is None or sub["chat_id"] != chat_id:
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Подписка не найдена")
        return

    if action == "cancel":
        sub["active"] = False
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Подписка отменена")
        send(chat_id, "❌ Подписка отменена:\n{}".format(sub["url"]))
    elif action == "renew":
        sub["active"] = True
        sub["expiry_notified"] = False
        sub["expires_at"] = (_now() + timedelta(days=SUB_DAYS)).isoformat()
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Продлено на 30 дней")
        send(chat_id, "🔄 Подписка продлена до {}:\n{}".format(
            _fmt(sub["expires_at"]), sub["url"]), keyboard=sub_keyboard(sub["id"]))
    else:
        tg("answerCallbackQuery", callback_query_id=cb_id)


def create_subscription(state, chat_id, url):
    for sub in state["subscriptions"]:
        if sub["chat_id"] == chat_id and sub["url"] == url and sub["active"]:
            send(chat_id, "Ты уже подписан на этот поиск 🙂", keyboard=sub_keyboard(sub["id"]))
            return

    now = _now()
    sub = {
        "id": uuid.uuid4().hex[:8],
        "chat_id": chat_id,
        "url": url,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=SUB_DAYS)).isoformat(),
        "active": True,
        "seeded": False,
        "expiry_notified": False,
        "seen_ids": [],
    }

    # Seed current listings so we don't spam the whole page on the first poll.
    try:
        listings = fetch_listings(url)
        sub["seen_ids"] = [lid for lid, _ in listings][:MAX_SEEN]
        sub["seeded"] = True
        seeded_note = "Сейчас в выдаче {} объявлений — пришлю только то, что появится позже.".format(len(listings))
    except Exception as exc:  # noqa: BLE001 - report any fetch failure to the user
        seeded_note = "(не удалось сразу прочитать выдачу, попробую при следующей проверке)"
        print("Seed failed for {}: {}".format(url, exc), file=sys.stderr)

    state["subscriptions"].append(sub)
    send(chat_id,
         "✅ Подписка создана на 30 дней (до {}).\n{}\n\n{}".format(
             _fmt(sub["expires_at"]), url, seeded_note),
         keyboard=sub_keyboard(sub["id"]))


def send_subscription_list(state, chat_id):
    subs = [s for s in state["subscriptions"] if s["chat_id"] == chat_id and s["active"]]
    if not subs:
        send(chat_id, "У тебя нет активных подписок. Пришли ссылку с bazaraki.com, чтобы создать.")
        return
    for sub in subs:
        send(chat_id,
             "🔎 Активна до {}:\n{}".format(_fmt(sub["expires_at"]), sub["url"]),
             keyboard=sub_keyboard(sub["id"]))


# --- polling subscriptions ---------------------------------------------------

def poll_subscriptions(state):
    now = _now()
    for sub in state["subscriptions"]:
        if not sub.get("active"):
            continue

        if _parse(sub["expires_at"]) <= now:
            _handle_expired(sub)
            continue

        try:
            listings = fetch_listings(sub["url"])
        except Exception as exc:  # noqa: BLE001
            print("Fetch failed for sub {}: {}".format(sub["id"], exc), file=sys.stderr)
            continue

        seen = set(sub.get("seen_ids", []))

        if not sub.get("seeded"):
            sub["seen_ids"] = [lid for lid, _ in listings][:MAX_SEEN]
            sub["seeded"] = True
            continue

        # Listings are newest-first; send oldest of the new ones first.
        new = [(lid, link) for lid, link in listings if lid not in seen]
        for lid, link in reversed(new[:MAX_SEND_PER_RUN]):
            send(sub["chat_id"], "🆕 Новое объявление:\n{}".format(link))

        if new:
            fresh_ids = [lid for lid, _ in listings]
            sub["seen_ids"] = (fresh_ids + sub.get("seen_ids", []))[:MAX_SEEN]


def _handle_expired(sub):
    if sub.get("expiry_notified"):
        sub["active"] = False
        return
    sub["expiry_notified"] = True
    sub["active"] = False
    send(sub["chat_id"],
         "⏰ Подписка истекла:\n{}\n\nНажми «Продлить», чтобы возобновить на 30 дней.".format(sub["url"]),
         keyboard=sub_keyboard(sub["id"]))


# --- helpers -----------------------------------------------------------------

def _find_sub(state, sub_id):
    for sub in state["subscriptions"]:
        if sub["id"] == sub_id:
            return sub
    return None


def _now():
    return datetime.now(timezone.utc)


def _parse(iso):
    return datetime.fromisoformat(iso)


def _fmt(iso):
    return _parse(iso).strftime("%d.%m.%Y")


if __name__ == "__main__":
    main()
