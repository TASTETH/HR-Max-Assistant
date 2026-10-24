import hashlib
import json

import httpx

from app import tunnel_watch, webhook_manager
from app.settings import Settings


async def test_rotating_domain_replaces_only_the_managed_subscription(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, max_bot_token="fake-token", max_webhook_secret="test-secret",
                        public_base_url="https://new.lhr.life")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "bot_fingerprint": hashlib.sha256(b"fake-token").hexdigest(),
        "url": "https://old.lhr.life/webhook/max", "owned_urls": ["https://old.lhr.life/webhook/max"]
    }), encoding="utf-8")
    (tmp_path / ".env").write_text("PUBLIC_BASE_URL=https://old.lhr.life\n", encoding="utf-8")
    calls = []
    class Client:
        def __init__(self, config):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def subscribe(self, config):
            calls.append(("subscribe", config.webhook_url))
            return {"success": True}
        async def request(self, method, path, *, params):
            calls.append((method, params["url"]))
            return {"success": True}
    monkeypatch.setattr(webhook_manager, "STATE", state_path)
    monkeypatch.setattr(webhook_manager, "ROOT", tmp_path)
    monkeypatch.setattr(webhook_manager, "MaxClient", Client)
    await webhook_manager.register(settings)
    assert calls == [("subscribe", "https://new.lhr.life/webhook/max"), ("DELETE", "https://old.lhr.life/webhook/max")]
    assert webhook_manager.read_state(settings)["owned_urls"] == ["https://new.lhr.life/webhook/max"]
    assert "https://new.lhr.life" in (tmp_path / ".env").read_text()


async def test_watcher_uses_last_announced_hostname(tmp_path, monkeypatch):
    log = tmp_path / "data/logs/ssh-tunnel.stdout.log"
    log.parent.mkdir(parents=True)
    log.write_text("https://old.lhr.life\nhttps://current.lhr.life\n", encoding="utf-8")
    monkeypatch.setattr(tunnel_watch, "ROOT", tmp_path)
    monkeypatch.setattr(tunnel_watch, "read_state", lambda settings: {"url": None, "owned_urls": []})
    addresses = []
    async def register(settings):
        addresses.append(settings.public_base_url)
    monkeypatch.setattr(tunnel_watch, "register", register)
    original = httpx.AsyncClient
    monkeypatch.setattr(tunnel_watch.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "ready"}))))
    assert await tunnel_watch.synchronize(Settings(_env_file=None))
    assert addresses == ["https://current.lhr.life"]
