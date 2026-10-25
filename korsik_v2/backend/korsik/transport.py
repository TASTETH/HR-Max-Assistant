"""MAX Bot API adapter (+ console transport for local checks; same business logic).

Delivery outcomes are explicit:
  * Sent(external_id)      — MAX returned the message id;
  * Rejected(retryable)    — MAX refused before acceptance (4xx/429/connect errors);
  * Uncertain              — timeout / 5xx after the request was sent: it MAY have been
                             delivered; never auto-resent (see outbox 'uncertain').
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings, get_settings, ROOT

log = logging.getLogger(__name__)


class Rejected(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class Uncertain(Exception):
    pass


@dataclass
class Sent:
    external_id: str


class _RateLimiter:
    def __init__(self, per_sec: float):
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.last + self.interval - now
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


def build_body(body: dict, attachment_tokens: list[dict]) -> dict:
    """Internal body {text, buttons:[[{text,payload}|{text,url}]]} -> MAX NewMessageBody."""
    out: dict = {"text": body.get("text", "")[:4000]}
    attachments = list(attachment_tokens)
    rows = body.get("buttons") or []
    if rows:
        kb = []
        for row in rows:
            kb_row = []
            for b in row:
                if b.get("url"):
                    kb_row.append({"type": "link", "text": b["text"][:64], "url": b["url"]})
                else:
                    kb_row.append({"type": "callback", "text": b["text"][:64], "payload": b["payload"]})
            kb.append(kb_row)
        attachments.append({"type": "inline_keyboard", "payload": {"buttons": kb}})
    if attachments:
        out["attachments"] = attachments
    return out


class MaxTransport:
    name = "max"

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.s = settings or get_settings()
        token = self.s.max_bot_token.get_secret_value()
        if not token:
            raise RuntimeError("MAX_BOT_TOKEN не задан")
        verify: ssl.SSLContext | bool = True
        if self.s.max_ca_bundle:
            ctx = ssl.create_default_context()
            p = Path(self.s.max_ca_bundle)
            ctx.load_verify_locations(cafile=str(p if p.is_absolute() else ROOT / p))
            verify = ctx
        self.http = client or httpx.Client(base_url=self.s.max_api_base_url, headers={"Authorization": token},
                                           timeout=self.s.max_timeout, verify=verify, follow_redirects=False)
        self.rl = _RateLimiter(self.s.max_rate_per_sec)

    def _req(self, method: str, path: str, *, params=None, json_body=None, files=None, url=None) -> dict:
        self.rl.wait()
        try:
            if url:
                r = httpx.post(url, files=files, timeout=120)  # upload URL is pre-signed: no bot token
            else:
                r = self.http.request(method, path, params=params, json=json_body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise Rejected(type(e).__name__, retryable=True) from None
        except httpx.HTTPError as e:
            raise Uncertain(type(e).__name__) from None
        if r.status_code == 429:
            raise Rejected("HTTP_429", retryable=True)
        if r.status_code >= 500:
            raise Uncertain(f"HTTP_{r.status_code}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            code = str(data.get("code") or f"HTTP_{r.status_code}")
            raise Rejected(code, retryable=code == "attachment.not.ready")
        return data if isinstance(data, dict) else {}

    # --- API
    def me(self) -> dict:
        return self._req("GET", "/me")

    def subscriptions(self) -> dict:
        return self._req("GET", "/subscriptions")

    def subscribe(self, url: str, secret: str) -> dict:
        return self._req("POST", "/subscriptions", json_body={
            "url": url, "secret": secret,
            "update_types": ["message_created", "message_callback", "bot_started"]})

    def unsubscribe(self, url: str) -> dict:
        return self._req("DELETE", "/subscriptions", params={"url": url})

    def upload(self, path: Path, kind: str) -> str:
        """kind: image | file. Returns attachment token."""
        info = self._req("POST", "/uploads", params={"type": kind})
        with open(path, "rb") as f:
            res = self._req("POST", "", url=info["url"], files={"data": (path.name, f)})
        token = info.get("token") or res.get("token")
        if not token and isinstance(res.get("photos"), dict):  # image upload response
            token = next(iter(res["photos"].values())).get("token")
        if not token:
            raise Rejected("UPLOAD_NO_TOKEN")
        return token

    def send(self, chat_id: str, body: dict) -> Sent:
        data = self._req("POST", "/messages", params={"chat_id": chat_id}, json_body=body)
        mid = ((data.get("message") or {}).get("body") or {}).get("mid")
        if not mid:
            raise Uncertain("NO_MESSAGE_ID")
        return Sent(str(mid))

    def edit(self, message_id: str, body: dict) -> None:
        self._req("PUT", "/messages", params={"message_id": message_id}, json_body=body)

    def answer_callback(self, callback_id: str, notification: str = "") -> None:
        self._req("POST", "/answers", params={"callback_id": callback_id},
                  json_body={"notification": notification or "✓"})


class ConsoleTransport:
    """Local verification transport: writes to data/console_outbox.jsonl.
    It is NOT a proof of delivery to MAX."""
    name = "console"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.file = self.s.data_dir / "console_outbox.jsonl"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.n = int(time.time() * 1000)
        self.lock = threading.Lock()

    def _write(self, rec: dict):
        with self.lock, open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def upload(self, path: Path, kind: str) -> str:
        return f"console-upload:{path.name}"

    def send(self, chat_id: str, body: dict) -> Sent:
        with self.lock:
            self.n += 1
            mid = f"console-{self.n}"
        self._write({"chat_id": chat_id, "mid": mid, "body": body})
        return Sent(mid)

    def edit(self, message_id: str, body: dict) -> None:
        self._write({"edit": message_id, "body": body})

    def answer_callback(self, callback_id: str, notification: str = "") -> None:
        pass

    def subscribe(self, url, secret):
        raise RuntimeError("console transport has no webhook")


_transport = None


def get_transport():
    global _transport
    if _transport is None:
        s = get_settings()
        _transport = ConsoleTransport(s) if s.transport == "console" else MaxTransport(s)
    return _transport


def set_transport(t) -> None:
    global _transport
    _transport = t
