"""Tiny JSON-file state store.

State shape:
{
  "update_offset": 0,                 # last processed Telegram update_id
  "subscriptions": [
    {
      "id": "ab12cd34",               # short unique id
      "chat_id": 123456789,
      "url": "https://www.bazaraki.com/...",
      "created_at": "2026-06-04T10:00:00+00:00",
      "expires_at": "2026-07-04T10:00:00+00:00",
      "active": true,
      "seeded": true,                 # initial listings recorded without notifying
      "expiry_notified": false,
      "seen_ids": [6426567, 6363855]
    }
  ]
}
"""
import json
import os

STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"update_offset": 0, "subscriptions": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("update_offset", 0)
    state.setdefault("subscriptions", [])
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)
