"""Klien Telegram Bot API pakai urllib saja (tanpa dependency luar)."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

_API = "https://api.telegram.org/bot"


class TelegramError(Exception):
    pass


def _call(method, payload=None, timeout=65):
    url = _API + config.BOT_TOKEN + "/" + method
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise TelegramError(method + " HTTP " + str(exc.code))
        # 429 = kena rate limit, hormati retry_after
        if exc.code == 429:
            wait = int(body.get("parameters", {}).get("retry_after", 3))
            time.sleep(wait + 1)
            return _call(method, payload, timeout)
        raise TelegramError(method + ": " + str(body.get("description")))
    except urllib.error.URLError as exc:
        raise TelegramError(method + " jaringan: " + str(exc.reason))

    if not body.get("ok"):
        raise TelegramError(method + ": " + str(body.get("description")))
    return body.get("result")


def get_updates(offset, timeout=50):
    return _call(
        "getUpdates",
        {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        },
        timeout=timeout + 15,
    )


def send(chat_id, text, keyboard=None, preview=False):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": not preview},
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return _call("sendMessage", payload)
    except TelegramError as exc:
        # pengguna blokir bot / chat dihapus: jangan bikin worker mati
        print("[tg] gagal kirim ke " + str(chat_id) + ": " + str(exc))
        return None


def edit(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return _call("editMessageText", payload)
    except TelegramError as exc:
        msg = str(exc)
        if "not modified" in msg:
            return None
        print("[tg] gagal edit: " + msg)
        return None


def answer_callback(callback_id, text=None, alert=False):
    payload = {"callback_query_id": callback_id, "show_alert": alert}
    if text:
        payload["text"] = text[:200]
    try:
        return _call("answerCallbackQuery", payload)
    except TelegramError:
        return None


def delete_webhook():
    """Wajib dipanggil sebelum long polling, kalau tidak getUpdates ditolak."""
    try:
        return _call("deleteWebhook", {"drop_pending_updates": False})
    except TelegramError as exc:
        print("[tg] deleteWebhook: " + str(exc))
        return None


def get_me():
    return _call("getMe", {})


def notify_admins(text):
    for admin_id in config.ADMIN_IDS:
        send(admin_id, text)


def escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
