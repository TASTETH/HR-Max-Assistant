"""Normalize MAX updates; do not assume that MAX supplies a global update ID."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pydantic import SecretStr

from app.router.schemas import Attachment, IncomingEvent

SUPPORTED_UPDATES = ["message_created", "message_callback", "bot_started"]


def _id(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError("Missing MAX identifier")
    return str(value)


def normalize_update(update: dict) -> IncomingEvent | None:
    kind = update.get("update_type")
    if kind not in SUPPORTED_UPDATES:
        return None
    timestamp = update.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("MAX timestamp is required, in milliseconds")
    occurred = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    common = {"channel": "max", "occurred_at": occurred}
    if kind == "bot_started":
        user = update.get("user") or {}
        if user.get("is_bot"):
            return None
        account, chat = _id(user.get("user_id")), _id(update.get("chat_id"))
        # payload can contain an invitation secret: persist its hash only.
        payload = update.get("payload") or ""
        if not isinstance(payload, str) or len(payload) > 512:
            raise ValueError("Invalid start payload")
        stable = json.dumps([kind, account, chat, timestamp, payload], separators=(",", ":"))
        common.update(provider_event_id="start:" + hashlib.sha256(stable.encode()).hexdigest(),
                      account_id=account, chat_id=chat)
        return IncomingEvent(**common, kind="invitation", invitation_token=SecretStr(payload)) if payload else IncomingEvent(**common, text="/start")

    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    # This HR bot handles personal dialogs only. Group posts must not expose HR context.
    if recipient.get("chat_type") != "dialog":
        return None
    chat = _id(recipient.get("chat_id"))
    if kind == "message_callback":
        callback = update.get("callback") or {}
        user = callback.get("user") or {}
        if user.get("is_bot"):
            return None
        callback_id = _id(callback.get("callback_id"))
        return IncomingEvent(**common, kind="callback", provider_event_id="callback:" + callback_id,
                             callback_id=callback_id, account_id=_id(user.get("user_id")),
                             chat_id=chat, callback_data=callback.get("payload"))

    sender = message.get("sender") or {}
    if sender.get("is_bot"):
        return None
    body = message.get("body") or {}
    text = body.get("text") or ""
    if not isinstance(text, str):
        raise ValueError("Invalid message text")
    attachments = []
    for item in body.get("attachments") or []:
        if item.get("type") not in {"image", "file"}:
            continue
        payload = item.get("payload") or {}
        reference = payload.get("token") or payload.get("url")
        if reference:
            attachments.append(Attachment(kind="image" if item["type"] == "image" else "document",
                                          reference=reference, name=item.get("filename", "")))
    link = message.get("link") or {}
    reply = (link.get("message") or {}).get("mid") if link.get("type") == "reply" else None
    # Unsupported attachments still receive a deterministic, useful response.
    if not text.strip() and not attachments:
        text = "[Неподдерживаемое вложение. Отправьте, пожалуйста, текст, изображение или файл.]"
    return IncomingEvent(**common, provider_event_id="message:" + _id(body.get("mid")),
                         account_id=_id(sender.get("user_id")), chat_id=chat,
                         kind="text" if text.strip() else "attachment", text=text,
                         attachments=attachments, reply_to_message_id=reply)
