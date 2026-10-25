"""Ollama native API contract (mock server): request shape, context budget, technical errors."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

seen = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen[self.path] = body
        if self.path == "/api/chat":
            out = {"message": {"role": "assistant", "content": json.dumps({"ok": True})}, "done": True}
        elif self.path == "/api/embed":
            out = {"embeddings": [[0.1, 0.2, 0.3] for _ in body["input"]]}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


@pytest.fixture
def ollama(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("OLLAMA_MODEL", "poliplastik-qwen:latest")
    monkeypatch.setenv("EMBED_MODEL", "bge-m3")
    from korsik.config import get_settings
    get_settings.cache_clear()
    yield
    srv.shutdown()
    get_settings.cache_clear()


def test_chat_and_embed_contract(ollama):
    from korsik.llm import LLMError, chat_json, embed
    assert chat_json("sys", "user", {"type": "object"}) == {"ok": True}
    req = seen["/api/chat"]
    assert req["model"] == "poliplastik-qwen:latest" and req["stream"] is False
    assert req["format"] == {"type": "object"} and req["options"]["num_ctx"] == 8192
    assert "think" not in req                                   # not forced on non-thinking models
    assert len(embed(["a", "b"])) == 2 and seen["/api/embed"]["model"] == "bge-m3"
    with pytest.raises(LLMError, match="context_overflow"):
        chat_json("sys", "я" * 30000, {"type": "object"})        # one budget check, before the call
