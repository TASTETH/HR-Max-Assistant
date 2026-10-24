"""Official MAX HTTP contract; secrets and provider response bodies never enter logs."""
from __future__ import annotations

import ssl

import httpx

from app.router.schemas import OutgoingMessage, RejectedBeforeAcceptance
from app.settings import Settings


class MaxRejected(RejectedBeforeAcceptance):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.retryable = retryable


class MaxUncertain(RuntimeError):
    """The POST may have been accepted. Reconciliation is required before a resend."""


def message_bodies(message: OutgoingMessage) -> list[dict]:
    """4000-character provider limit. Each part is persisted separately before sending."""
    chunks = [message.text[i:i + 4000] for i in range(0, len(message.text), 4000)] or [""]
    bodies = [{"text": chunk} for chunk in chunks]
    attachments = []
    for item in message.attachments:
        kind = "image" if item.kind in {"image", "photo"} else "file"
        if item.reference.startswith("https://") and kind == "image":
            payload = {"url": item.reference}
        elif "://" in item.reference:
            raise MaxRejected("ATTACHMENT_REQUIRES_MAX_UPLOAD_TOKEN")
        else:
            payload = {"token": item.reference}
        attachments.append({"type": kind, "payload": payload})
    if message.buttons:
        buttons = []
        for button in message.buttons:
            if button.url:
                buttons.append({"type": "link", "text": button.label, "url": button.url})
            elif button.callback_data:
                buttons.append({"type": "callback", "text": button.label, "payload": button.callback_data})
        # One button per row fits long Russian labels and MAX's per-row limit.
        attachments.append({"type": "inline_keyboard", "payload": {"buttons": [[b] for b in buttons]}})
    if attachments:
        bodies[-1]["attachments"] = attachments
    return bodies


class MaxClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        token = settings.max_bot_token.get_secret_value()
        if not token:
            raise ValueError("Set MAX_BOT_TOKEN in .env")
        verify = ssl.create_default_context()
        if settings.max_ca_bundle:
            from pathlib import Path
            from app.settings import ROOT
            ca_path = Path(settings.max_ca_bundle)
            verify.load_verify_locations(cafile=str(ca_path if ca_path.is_absolute() else ROOT / ca_path))
        self.client = httpx.AsyncClient(base_url=settings.max_api_base_url,
            headers={"Authorization": token}, timeout=settings.max_request_timeout,
            verify=verify, transport=transport, follow_redirects=False, trust_env=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method: str, path: str, *, params=None, body=None) -> dict:
        try:
            response = await self.client.request(method, path, params=params, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise MaxRejected(type(exc).__name__, retryable=True) from None
        except httpx.HTTPError as exc:
            raise MaxUncertain(type(exc).__name__) from None
        if response.status_code == 429:
            raise MaxRejected("MAX_HTTP_429", retryable=True)
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise MaxUncertain(f"MAX_HTTP_{response.status_code}")
        if response.status_code >= 400:
            raise MaxRejected(f"MAX_HTTP_{response.status_code}")
        try:
            result = response.json()
        except ValueError:
            raise MaxUncertain("MAX_INVALID_JSON") from None
        if not isinstance(result, dict):
            raise MaxUncertain("MAX_INVALID_RESPONSE")
        if result.get("success") is False or result.get("error") or result.get("code"):
            raise MaxRejected("MAX_REJECTED")
        return result

    async def me(self) -> dict:
        return await self.request("GET", "/me")

    async def subscriptions(self) -> dict:
        return await self.request("GET", "/subscriptions")

    async def subscribe(self, settings: Settings) -> dict:
        from .events import SUPPORTED_UPDATES
        return await self.request("POST", "/subscriptions", body={"url": settings.webhook_url,
            "secret": settings.require_webhook_secret(), "update_types": SUPPORTED_UPDATES})

    async def send_body(self, chat_id: str, body: dict) -> str:
        result = await self.request("POST", "/messages", params={"chat_id": chat_id}, body=body)
        mid = ((result.get("message") or {}).get("body") or {}).get("mid")
        if not mid:
            raise MaxUncertain("MAX_MISSING_MESSAGE_ID")
        return str(mid)

    async def answer_callback(self, callback_id: str) -> None:
        await self.request("POST", "/answers", params={"callback_id": callback_id}, body={})
