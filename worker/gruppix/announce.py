"""Bot message in the group when an event's gallery folder fills up (ANNOUNCE=on).

Rules: live intake only (never imports, photographer uploads or old events),
at most one message per event, once it has ANNOUNCE_MIN_ITEMS published items,
never during quiet hours (postponed to their end). The message carries the
folder link, never the password.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from .config import Config
from .db import Database, State

log = logging.getLogger(__name__)

GIVE_UP_AFTER = timedelta(days=1)  # a message that could not be sent for a day is dropped


def next_allowed(now: datetime, quiet: tuple[int, int]) -> datetime:
    """``now`` or, inside the quiet hours (start-end, may span midnight), their end."""
    start, end = quiet
    if start == end:
        return now
    hour = now.hour
    inside = start <= hour < end if start < end else (hour >= start or hour < end)
    if not inside:
        return now
    target = now.replace(hour=end, minute=0, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


class Announcer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.announce and bool(self.cfg.announce_url) and bool(self.cfg.group_chat_id)

    def consider(self, db: Database, media_id: str, session: str, now: datetime | None = None) -> bool:
        """Called after a live item was published; schedules the event's message once."""
        if not self.enabled:
            return False
        now = now or datetime.now(self.cfg.tz)
        row = db.get(media_id)
        key = row["event_key"]
        if row["target"] != "gallery" or not key or db.has_announcement(key):
            return False
        info = db.event_info(key)
        if info is None or info["start_ts"] < (now - timedelta(days=self.cfg.announce_max_age_days)).timestamp():
            return False  # old event, e.g. a late photo of last month's concert
        if db.count_for_event(key, State.PUBLISHED) < self.cfg.announce_min_items:
            return False
        due = next_allowed(now, self.cfg.announce_quiet)
        return db.add_announcement(key, session, int(due.timestamp()))

    def send_due(self, db: Database, now: datetime | None = None) -> int:
        if not self.enabled:
            return 0
        now = now or datetime.now(self.cfg.tz)
        sent = 0
        for ann in db.due_announcements(int(now.timestamp())):
            if now.timestamp() - ann["due_at"] > GIVE_UP_AFTER.total_seconds():
                db.mark_announced(ann["event_key"], 0)  # 0 = dropped, never retried
                log.warning("announcement for %s dropped (could not be sent for a day)", ann["event_key"])
                continue
            text = self.text(db, ann["event_key"])
            if text is None or self._post(ann["session"], text):
                db.mark_announced(ann["event_key"], int(now.timestamp()))
                sent += 1 if text else 0
        return sent

    def text(self, db: Database, event_key: str) -> str | None:
        info = db.event_info(event_key)
        rel_dir = db.event_gallery_dir(event_key)
        if info is None or rel_dir is None:
            return None
        link = self.cfg.announce_url.replace("{path}", quote("/" + rel_dir, safe=""))
        return self.cfg.announce_text.format(
            title=info["title"],
            folder=info["folder"],
            count=db.count_for_event(event_key, State.PUBLISHED),
            link=link,
        )

    def _post(self, session: str, text: str) -> bool:
        try:
            response = requests.post(
                f"{(self.cfg.waha_url or '').rstrip('/')}/api/sendText",
                headers={"X-Api-Key": self.cfg.waha_api_key or ""},
                json={"session": session, "chatId": self.cfg.group_chat_id, "text": text},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("announcement failed (retried): %s", exc)
            return False
        log.info("announcement sent to the group")
        return True
