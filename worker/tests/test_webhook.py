"""Webhook intake (phase 1): signature, counters, capture of the test group only."""

import hashlib
import hmac
import json
import threading
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

from gruppix.webhook import WebhookInbox, check_signature, group_ids

KEY = "secret"
GROUP = "120363000000000001@g.us"
OTHER = "120363000000000002@g.us"


def _body(event: str, chat: str, **payload) -> bytes:
    return json.dumps({"event": event, "session": "default",
                       "payload": {"from": chat, "participant": "49111@c.us", **payload}}).encode()


def _sign(body: bytes) -> dict[str, str]:
    return {"X-Webhook-Hmac": hmac.new(KEY.encode(), body, hashlib.sha512).hexdigest(),
            "X-Webhook-Hmac-Algorithm": "sha512"}


def test_signature_states():
    body = b'{"event":"message"}'
    assert check_signature(None, body, {}) == "off"
    assert check_signature(KEY, body, {}) == "missing"
    headers = {k.lower(): v for k, v in _sign(body).items()}
    assert check_signature(KEY, body, headers) == "ok"
    assert check_signature("other", body, headers) == "invalid"


def test_group_ids_found_anywhere():
    payload = {"from": GROUP, "reaction": {"messageId": f"false_{GROUP}_ABC"}, "list": [{"x": OTHER}]}
    assert group_ids(payload) == {GROUP, OTHER}


def test_counts_all_but_captures_only_the_configured_group(cfg):
    cfg = replace(cfg, hook_hmac_key=KEY, group_chat_id=GROUP, webhook_capture=True)
    inbox = WebhookInbox(cfg)
    for chat in (GROUP, OTHER):
        body = _body("message", chat, body="Hallo")
        assert inbox.handle(body, _sign(body))[0] == 202
    body = _body("session.status", "", status="WORKING")
    inbox.handle(body, _sign(body))

    stats = inbox.stats()
    assert stats["events"] == {"message": 2, "session.status": 1}
    assert stats["groups"] == {GROUP: 1, OTHER: 1}
    assert stats["signatures"] == {"ok": 3}
    files = sorted(p.name for p in cfg.capture_dir.glob("*.json"))
    assert files == ["0001_message.json", "0002_session-status.json"]
    record = json.loads((cfg.capture_dir / files[0]).read_text())
    assert record["signature"] == "ok" and GROUP in json.dumps(record["body"])
    assert "x-webhook-hmac" not in record["headers"]


def test_capture_needs_group_id(cfg):
    cfg = replace(cfg, webhook_capture=True)
    inbox = WebhookInbox(cfg)
    inbox.handle(_body("message", GROUP), {})
    assert list(cfg.capture_dir.glob("*.json")) == []
    assert inbox.stats()["groups"] == {GROUP: 1}


def test_invalid_json_is_rejected(cfg):
    assert WebhookInbox(cfg).handle(b"not json", {})[0] == 400


def test_http_roundtrip(cfg):
    from gruppix.service import Service, _handler

    svc = Service(replace(cfg, hook_hmac_key=KEY))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(svc))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = _body("message", GROUP)
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/webhook", data=body,
                                         headers={"Content-Type": "application/json", **_sign(body)})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
    finally:
        server.shutdown()
    assert svc.inbox.stats()["signatures"] == {"ok": 1}
