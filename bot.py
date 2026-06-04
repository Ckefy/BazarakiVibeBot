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
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from scraper import fetch_listings, normalize_search_url
from storage import load_state, save_state

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = "https://api.telegram.org/bot{}".format(TOKEN)

SUB_DAYS = 30           # subscription length / renew length
MAX_SEEN = 600          # cap stored ids per subscription
MAX_SEND_PER_RUN = 15   # avoid flooding on a single run

# Telegram updates are handled on every run (cheap, keeps the bot responsive),
# but the Bazaraki search runs at most once per this interval (saves credits).
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES") or "30")

# If > 0, keep long-polling Telegram for this many seconds before exiting (near
# real-time replies). The cron restarts the job to give continuous coverage.
# 0 = single-shot (process pending updates once and exit).
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS") or "0")
LONG_POLL_TIMEOUT = 25  # seconds Telegram holds the getUpdates connection
# Inside a long loop, commit state.json to git this often so a killed job keeps
# its progress (only active when GIT_PERSIST=1, i.e. on GitHub Actions).
GIT_PERSIST_SECONDS = int(os.environ.get("GIT_PERSIST_SECONDS") or "180")

# Access control: the owner approves who may use the bot. If OWNER_CHAT_ID is set
# it pins the owner; otherwise the first person who ever messages the bot becomes
# the owner automatically (it's their bot).
OWNER_CHAT_ID = (os.environ.get("OWNER_CHAT_ID") or "").strip()

# Quiet hours (Cyprus local time): no Bazaraki/scraping-API requests in this window.
QUIET_START = int(os.environ.get("QUIET_START_HOUR") or "1")   # 01:00
QUIET_END = int(os.environ.get("QUIET_END_HOUR") or "6")       # 06:00

try:
    from zoneinfo import ZoneInfo
    _CYPRUS_TZ = ZoneInfo("Asia/Nicosia")
except Exception:  # noqa: BLE001 - fall back to EEST if tz data is unavailable
    _CYPRUS_TZ = timezone(timedelta(hours=3))

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
    _migrate_users(state)

    if LOOP_SECONDS <= 0:
        process_updates(state)
        poll_subscriptions(state)
        save_state(state)
        return

    # Long-poll loop: replies are near-instant; the search stays rate-limited.
    # A long window means a queued (pending) run is ready to take over the instant
    # this one ends, so coverage survives GitHub's unreliable cron scheduling.
    deadline = time.monotonic() + LOOP_SECONDS
    last_persist = time.monotonic()
    while time.monotonic() < deadline:
        process_updates(state, long_poll=LONG_POLL_TIMEOUT)
        poll_subscriptions(state)
        save_state(state)
        if time.monotonic() - last_persist >= GIT_PERSIST_SECONDS:
            _git_persist()
            last_persist = time.monotonic()
    _git_persist()  # final flush before this run hands off to the next


# --- Telegram API ------------------------------------------------------------

def tg(method, _http_timeout=40, **params):
    resp = requests.post("{}/{}".format(API, method), json=params, timeout=_http_timeout)
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

def process_updates(state, long_poll=0):
    offset = state.get("update_offset", 0)
    result = tg("getUpdates", offset=offset + 1, timeout=long_poll,
                allowed_updates=["message", "callback_query"],
                _http_timeout=long_poll + 15)
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

    _register_user(state, message)
    status = _access_status(state, chat_id)

    if status == "new":
        _set_user_status(state, chat_id, "pending")
        _notify_owner_request(state, chat_id)
        send(chat_id, "👋 Привет! Доступ к этому боту по одобрению. "
                      "Запрос отправлен владельцу — подожди, пожалуйста ⏳")
        return
    if status == "pending":
        send(chat_id, "Доступ ещё не одобрен. Подожди одобрения владельца ⏳")
        return
    if status == "denied":
        send(chat_id, "Доступ к боту закрыт.")
        return

    # Owner-only command to review pending requests.
    if status == "owner" and text.startswith("/pending"):
        send_pending_list(state, chat_id)
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

    action, _, arg = data.partition(":")

    # Owner approving/denying an access request.
    if action in ("approve", "deny"):
        _handle_access_decision(state, chat_id, cb_id, action, arg)
        return

    # Everything else requires an approved user.
    if _access_status(state, chat_id) not in ("owner", "allowed"):
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Нет доступа")
        return

    sub_id = arg
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
    url = normalize_search_url(url)
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
    # During quiet hours we don't touch the scraping API — seeding is deferred.
    if _is_quiet_hours():
        seeded_note = ("Ночью (с {:02d}:00 до {:02d}:00 по Кипру) выдачу не читаю — "
                       "начну отслеживать после {:02d}:00.").format(QUIET_START, QUIET_END, QUIET_END)
    else:
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


# --- access control ----------------------------------------------------------

def _migrate_users(state):
    """Grandfather existing subscribers as allowed (first one as owner) so the
    approval feature doesn't lock out people who subscribed before it existed."""
    if state.get("users"):
        return
    users = {}
    owner_set = False
    for sub in state.get("subscriptions", []):
        cid = str(sub["chat_id"])
        if cid in users:
            continue
        users[cid] = {"status": "allowed"}
        if not owner_set and not OWNER_CHAT_ID:
            users[cid]["is_owner"] = True
            owner_set = True
    if users:
        state["users"] = users


def _register_user(state, message):
    users = state.setdefault("users", {})
    rec = users.setdefault(str(message["chat"]["id"]), {"status": "new"})
    frm = message.get("from", {})
    name = " ".join(x for x in [frm.get("first_name"), frm.get("last_name")] if x)
    if name:
        rec["name"] = name
    if frm.get("username"):
        rec["username"] = frm["username"]


def _get_owner_id(state):
    if OWNER_CHAT_ID:
        return int(OWNER_CHAT_ID)
    for cid, rec in state.get("users", {}).items():
        if rec.get("is_owner"):
            return int(cid)
    return None


def _make_owner(state, chat_id):
    rec = state.setdefault("users", {}).setdefault(str(chat_id), {})
    rec["is_owner"] = True
    rec["status"] = "allowed"


def _access_status(state, chat_id):
    """Returns one of: owner, allowed, pending, denied, new. May bootstrap owner."""
    owner = _get_owner_id(state)
    if owner is None or chat_id == owner:
        _make_owner(state, chat_id)
        return "owner"
    rec = state.get("users", {}).get(str(chat_id))
    if not rec or rec.get("status") in (None, "new"):
        return "new"
    return rec["status"]


def _is_allowed(state, chat_id):
    """Read-only allow check (no side effects) — used while polling."""
    owner = _get_owner_id(state)
    if owner is not None and chat_id == owner:
        return True
    rec = state.get("users", {}).get(str(chat_id))
    return bool(rec and rec.get("status") == "allowed")


def _set_user_status(state, chat_id, status):
    rec = state.setdefault("users", {}).setdefault(str(chat_id), {})
    rec["status"] = status
    if status == "pending" and "requested_at" not in rec:
        rec["requested_at"] = _now().isoformat()


def _user_label(rec, chat_id):
    parts = []
    if rec.get("name"):
        parts.append(rec["name"])
    if rec.get("username"):
        parts.append("@" + rec["username"])
    parts.append("(id {})".format(chat_id))
    return " ".join(parts)


def _notify_owner_request(state, requester_chat_id):
    owner = _get_owner_id(state)
    if owner is None or owner == requester_chat_id:
        return
    rec = state.get("users", {}).get(str(requester_chat_id), {})
    keyboard = [[
        {"text": "✅ Разрешить", "callback_data": "approve:{}".format(requester_chat_id)},
        {"text": "⛔ Отклонить", "callback_data": "deny:{}".format(requester_chat_id)},
    ]]
    send(owner, "🔐 Запрос доступа от {}.\nРазрешить пользоваться ботом?".format(
        _user_label(rec, requester_chat_id)), keyboard=keyboard)


def _handle_access_decision(state, presser_chat_id, cb_id, action, arg):
    if presser_chat_id != _get_owner_id(state):
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Только владелец")
        return
    try:
        target = int(arg)
    except ValueError:
        tg("answerCallbackQuery", callback_query_id=cb_id)
        return

    rec = state.get("users", {}).get(str(target), {})
    if action == "approve":
        _set_user_status(state, target, "allowed")
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Доступ открыт")
        send(presser_chat_id, "✅ Доступ открыт: {}".format(_user_label(rec, target)))
        send(target, "✅ Тебе открыли доступ! Пришли ссылку с bazaraki.com, чтобы подписаться. /help")
    else:
        _set_user_status(state, target, "denied")
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Отклонено")
        send(presser_chat_id, "⛔ Отклонён: {}".format(_user_label(rec, target)))
        send(target, "К сожалению, доступ к боту не предоставлен.")


def send_pending_list(state, owner_chat_id):
    pending = [(cid, rec) for cid, rec in state.get("users", {}).items()
               if rec.get("status") == "pending"]
    if not pending:
        send(owner_chat_id, "Нет ожидающих запросов.")
        return
    for cid, rec in pending:
        keyboard = [[
            {"text": "✅ Разрешить", "callback_data": "approve:" + cid},
            {"text": "⛔ Отклонить", "callback_data": "deny:" + cid},
        ]]
        send(owner_chat_id, "🔐 Ожидает: {}".format(_user_label(rec, int(cid))), keyboard=keyboard)


# --- polling subscriptions ---------------------------------------------------

def poll_subscriptions(state):
    if _is_quiet_hours():
        print("Quiet hours in Cyprus ({:02d}:00-{:02d}:00) — skipping scraping.".format(
            QUIET_START, QUIET_END))
        return

    now = _now()
    last = state.get("last_poll_at")
    if last and (now - _parse(last)) < timedelta(minutes=POLL_INTERVAL_MINUTES):
        print("Last search was < {} min ago — skipping (Telegram still handled).".format(
            POLL_INTERVAL_MINUTES))
        return
    # Mark the poll time up front so frequent runs don't all fetch on transient errors.
    state["last_poll_at"] = now.isoformat()

    for sub in state["subscriptions"]:
        if not sub.get("active"):
            continue
        if not _is_allowed(state, sub["chat_id"]):
            continue  # user revoked/denied — stop sending

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

def _is_quiet_hours():
    hour = datetime.now(_CYPRUS_TZ).hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= hour < QUIET_END
    # window wraps past midnight (e.g. 23:00-06:00)
    return hour >= QUIET_START or hour < QUIET_END


def _find_sub(state, sub_id):
    for sub in state["subscriptions"]:
        if sub["id"] == sub_id:
            return sub
    return None


def _git_persist():
    """Commit & push state.json mid-loop so a killed job keeps its progress.
    Best-effort and a no-op unless GIT_PERSIST=1 (set by the workflow)."""
    if not os.environ.get("GIT_PERSIST"):
        return
    ident = ["-c", "user.name=bazaraki-bot", "-c", "user.email=bot@users.noreply.github.com"]
    try:
        if subprocess.run(["git", "diff", "--quiet", "--", "state.json"]).returncode == 0:
            return  # nothing changed since last persist
        subprocess.run(["git"] + ident + ["add", "state.json"], check=False, capture_output=True)
        subprocess.run(["git"] + ident + ["commit", "-m", "Update state [skip ci]"],
                       check=False, capture_output=True)
        subprocess.run(["git", "push"], check=False, capture_output=True)
    except Exception as exc:  # noqa: BLE001 - persistence must never crash the bot
        print("git persist failed: {}".format(exc), file=sys.stderr)


def _now():
    return datetime.now(timezone.utc)


def _parse(iso):
    return datetime.fromisoformat(iso)


def _fmt(iso):
    return _parse(iso).strftime("%d.%m.%Y")


if __name__ == "__main__":
    main()
