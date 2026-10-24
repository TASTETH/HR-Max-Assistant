import json
from unittest.mock import Mock

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.max.client import MaxClient, MaxRejected, MaxUncertain, message_bodies
from app.max.events import normalize_update
from app.router.schemas import Button, OutgoingMessage, RouterResult
from app.settings import Settings


@pytest.fixture
def settings():
    return Settings(_env_file=None, max_bot_token="test-token", max_webhook_secret="test-secret",
                    local_worker_enabled=False)


def update(text="/start", mid="mid-1"):
    return {"update_type": "message_created", "timestamp": 1792830000000,
            "message": {"sender": {"user_id": 123, "is_bot": False},
                "recipient": {"chat_id": 456, "chat_type": "dialog"},
                "body": {"mid": mid, "text": text}}}


def test_adapter_deduplication_uses_message_and_callback_ids():
    first = normalize_update(update())
    retried = update()
    retried["timestamp"] += 100
    assert normalize_update(retried).key == first.key
    callback = {"update_type": "message_callback", "timestamp": 1792830000000,
                "callback": {"callback_id": "cb1", "payload": "r:target", "user": {"user_id": 123}},
                "message": update()["message"]}
    event = normalize_update(callback)
    assert event.account_id == "123" and event.callback_id == "cb1"
    assert event.key != first.key


def test_start_invitation_is_redacted_and_bot_and_group_messages_ignored():
    event = normalize_update({"update_type": "bot_started", "timestamp": 1792830000000,
                              "chat_id": 456, "user": {"user_id": 123}, "payload": "secret-invite"})
    assert event.kind == "invitation"
    assert "secret-invite" not in json.dumps(event.envelope())
    msg = update()
    msg["message"]["sender"]["is_bot"] = True
    assert normalize_update(msg) is None
    msg["message"]["sender"]["is_bot"] = False
    msg["message"]["recipient"]["chat_type"] = "chat"
    assert normalize_update(msg) is None


def test_webhook_auth_acknowledges_only_after_persistence(settings):
    repo, enqueue = Mock(), Mock()
    repo.receive.return_value = RouterResult(event_id="max:message:mid-1", status="pending")
    client = TestClient(create_app(settings, repo, enqueue))
    assert client.post("/webhook/max", json=update()).status_code == 401
    repo.receive.assert_not_called()
    headers = {"X-Max-Bot-Api-Secret": "test-secret"}
    assert client.post("/webhook/max", json=update(), headers=headers).json() == {"ok": True, "duplicate": False}
    enqueue.assert_called_once_with("max:message:mid-1")
    repo.receive.side_effect = psycopg.OperationalError("secret-DSN")
    response = client.post("/webhook/max", json=update(), headers=headers)
    assert response.status_code == 503 and "secret-DSN" not in response.text


def test_webhook_survives_broker_outage_and_duplicate_does_not_reenqueue(settings):
    repo, enqueue = Mock(), Mock(side_effect=ConnectionError())
    repo.receive.return_value = RouterResult(event_id="x", status="pending")
    client = TestClient(create_app(settings, repo, enqueue))
    headers = {"X-Max-Bot-Api-Secret": "test-secret"}
    assert client.post("/webhook/max", json=update(), headers=headers).status_code == 200
    repo.receive.return_value = RouterResult(event_id="x", status="pending", duplicate=True)
    assert client.post("/webhook/max", json=update(), headers=headers).json()["duplicate"]
    assert enqueue.call_count == 1


def test_webhook_rejects_invalid_or_oversized_bodies(settings):
    repo = Mock()
    client = TestClient(create_app(settings, repo, Mock()))
    headers = {"X-Max-Bot-Api-Secret": "test-secret"}
    assert client.post("/webhook/max", content="{", headers=headers).status_code == 400
    assert client.post("/webhook/max", json=update("x" * 4001), headers=headers).status_code == 400
    assert client.post("/webhook/max", content="x" * 65537, headers=headers).status_code == 413
    assert client.post("/webhook/max", json={"update_type": "future_event"}, headers=headers).json()["ignored"]
    repo.receive.assert_not_called()


async def test_client_uses_header_and_official_keyboard_contract(settings):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"message": {"body": {"mid": "created-mid"}}})
    message = OutgoingMessage(message_id=1, chat_id="456", text="hello", buttons=[
        Button(label="HR", callback_data="r:123"), Button(label="Сайт", url="https://example.org")])
    body = message_bodies(message)[0]
    async with MaxClient(settings, transport=httpx.MockTransport(handler)) as client:
        assert await client.send_body(message.chat_id, body) == "created-mid"
    request = seen[0]
    assert request.headers["Authorization"] == "test-token"
    assert "test-token" not in str(request.url)
    assert request.url.params["chat_id"] == "456"
    assert json.loads(request.content)["attachments"][0]["payload"]["buttons"][0][0] == {"type": "callback", "text": "HR", "payload": "r:123"}


@pytest.mark.parametrize("error,exception", [(httpx.ReadTimeout, MaxUncertain), (httpx.ConnectTimeout, MaxRejected)])
async def test_client_distinguishes_ambiguous_send_from_connection_failure(settings, error, exception):
    def handler(request):
        raise error("failure", request=request)
    async with MaxClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(exception):
            await client.send_body("456", {"text": "hello"})


def test_long_messages_keep_buttons_on_last_part():
    message = OutgoingMessage(message_id=1, chat_id="456", text="я" * 9000,
                              buttons=[Button(label="HR", callback_data="r:123")])
    parts = message_bodies(message)
    assert [len(p["text"]) for p in parts] == [4000, 4000, 1000]
    assert "attachments" not in parts[0] and "attachments" in parts[-1]


async def test_provider_error_with_http_200_is_a_known_rejection(settings):
    async with MaxClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"code": "invalid.request", "message": "provider text"}))) as client:
        with pytest.raises(MaxRejected, match="MAX_REJECTED"):
            await client.send_body("456", {"text": "hello"})
