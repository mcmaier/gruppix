"""WAHA webhook intake.

Always: verify the HMAC signature and count events per type and per group (so
the group id can be read from /health). Optionally record raw webhooks of
GROUP_CHAT_ID (WEBHOOK_CAPTURE) to check payloads of a new WAHA version.
With LIVE_INGEST=on, events of GROUP_CHAT_ID become tasks for live.LiveIntake.

Privacy: without GROUP_CHAT_ID nothing is recorded; other chats only show up
as counters. Texts never leave the capture directory of the test group.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from .config import Config
from .live import tasks_from_webhook

if TYPE_CHECKING:
    from .live import LiveIntake

log = logging.getLogger(__name__)

CAPTURE_LIMIT = 500          # files; capture stops silently afterwards
_MEDIA_CHECK_TIMEOUT = 30


def group_ids(value: Any) -> set[str]:
    """All group JIDs (…@g.us) anywhere in a payload, independent of the engine's layout."""
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and item.endswith("@g.us"):
            found.add(item)
    return found


def check_signature(key: str | None, body: bytes, headers: dict[str, str]) -> str:
    """'ok', 'invalid', 'missing' (no header) or 'off' (no key configured)."""
    if not key:
        return "off"
    received = headers.get("x-webhook-hmac")
    if not received:
        return "missing"
    algorithm = (headers.get("x-webhook-hmac-algorithm") or "sha512").lower()
    digest = getattr(hashlib, algorithm, None)
    if digest is None:
        return "invalid"
    expected = hmac.new(key.encode(), body, digest).hexdigest()
    return "ok" if hmac.compare_digest(expected, received.strip().lower()) else "invalid"


class WebhookInbox:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.events: Counter[str] = Counter()
        self.groups: Counter[str] = Counter()
        self.signatures: Counter[str] = Counter()
        self.captured = 0
        self._lock = threading.Lock()
        self._warned_no_group = False
        self.live: LiveIntake | None = None
        if cfg.webhook_capture:
            cfg.capture_dir.mkdir(parents=True, exist_ok=True)
            self.captured = sum(1 for _ in cfg.capture_dir.glob("*.json"))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "events": dict(self.events),
                "groups": dict(self.groups),
                "signatures": dict(self.signatures),
                "captured": self.captured if self.cfg.webhook_capture else None,
            }

    def handle(self, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        headers = {k.lower(): v for k, v in headers.items()}
        signature = check_signature(self.cfg.hook_hmac_key, body, headers)
        try:
            data = json.loads(body)
        except ValueError:
            with self._lock:
                self.signatures[signature] += 1
            return 400, {"error": "invalid json"}
        if not isinstance(data, dict):
            return 400, {"error": "expected an object"}

        event = str(data.get("event") or "?")
        groups = group_ids(data.get("payload"))
        with self._lock:
            self.signatures[signature] += 1
            self.events[event] += 1
            for gid in groups:
                self.groups[gid] += 1

        if self.cfg.webhook_capture and self._wanted(event, groups):
            self._capture(event, signature, headers, data)
        if self.live is None:
            # Not live: never reject, WAHA would only retry.
            return 202, {"status": "ignored"}
        if signature in ("invalid", "missing"):
            log.warning("webhook rejected: signature %s", signature)
            return 401, {"error": f"signature {signature}"}
        tasks = tasks_from_webhook(self.cfg, data, int(datetime.now().timestamp() * 1000))
        self.live.enqueue(tasks)
        return 202, {"status": "queued" if tasks else "ignored", "tasks": len(tasks)}

    # --- capture --------------------------------------------------------------

    def _wanted(self, event: str, groups: set[str]) -> bool:
        if not self.cfg.group_chat_id:
            if not self._warned_no_group:
                log.warning("WEBHOOK_CAPTURE=on but GROUP_CHAT_ID is empty – nothing is recorded")
                self._warned_no_group = True
            return False
        return event.startswith("session.") or self.cfg.group_chat_id in groups

    def _capture(self, event: str, signature: str, headers: dict[str, str], data: dict[str, Any]) -> None:
        with self._lock:
            if self.captured >= CAPTURE_LIMIT:
                return
            self.captured += 1
            number = self.captured
        record = {
            "received_at": datetime.now(self.cfg.tz).isoformat(timespec="seconds"),
            "signature": signature,
            "headers": {k: v for k, v in headers.items() if k.startswith("x-webhook") and k != "x-webhook-hmac"},
            "body": data,
        }
        path = self.cfg.capture_dir / f"{number:04d}_{event.replace('.', '-')}.json"
        media = (data.get("payload") or {}).get("media") if isinstance(data.get("payload"), dict) else None
        if isinstance(media, dict) and media.get("url"):
            # Fetch in the background so WAHA gets its answer immediately.
            threading.Thread(target=self._check_media, args=(path, record, media["url"]), daemon=True).start()
        else:
            _write(path, record)

    def _check_media(self, path: Path, record: dict[str, Any], url: str) -> None:
        """Verify the worker can download the file; only size and type are recorded."""
        result: dict[str, Any]
        try:
            response = requests.get(
                url, headers={"X-Api-Key": self.cfg.waha_api_key or ""}, timeout=_MEDIA_CHECK_TIMEOUT
            )
            result = {
                "status": response.status_code,
                "bytes": len(response.content),
                "content_type": response.headers.get("Content-Type"),
            }
        except requests.RequestException as exc:
            result = {"error": type(exc).__name__}
        record["media_check"] = result
        _write(path, record)


def _write(path: Path, record: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("webhook capture: %s", exc)
