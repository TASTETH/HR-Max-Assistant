"""Track only this installation's subscriptions, including rotating development URLs."""
from __future__ import annotations

import hashlib
import json
import logging

from dotenv import set_key

from app.max.client import MaxClient
from app.settings import ROOT, Settings

STATE = ROOT / "data/webhook-state.json"
log = logging.getLogger(__name__)


def read_state(settings: Settings):
    fingerprint = hashlib.sha256(settings.max_bot_token.get_secret_value().encode()).hexdigest()
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if state.get("bot_fingerprint") == fingerprint:
            return state
    except (OSError, ValueError):
        pass
    return {"bot_fingerprint": fingerprint, "url": None, "owned_urls": []}


async def register(settings: Settings):
    state = read_state(settings)
    url = settings.webhook_url
    previous = set(state.get("owned_urls", []))
    async with MaxClient(settings) as client:
        result = await client.subscribe(settings)
        if result.get("success") is not True:
            raise ValueError("MAX did not confirm subscription")
        pending = []
        for old in sorted(previous - {url}):
            try:
                await client.request("DELETE", "/subscriptions", params={"url": old})
            except Exception:
                pending.append(old)
                log.warning("Old managed webhook cleanup deferred")
    state.update(url=url, owned_urls=[url, *pending])
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(STATE)
    env_path = ROOT / ".env"
    if env_path.exists():
        set_key(str(env_path), "PUBLIC_BASE_URL", settings.public_base_url, quote_mode="never")
    return {"success": True, "webhook_url": url}
