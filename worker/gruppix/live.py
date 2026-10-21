"""Phase 2: live intake of the WhatsApp group via WAHA webhooks.

Webhooks are turned into small tasks (media, reaction, revoke) and written to a
durable queue under data/live/queue/ – the HTTP handler never touches the
database. A background thread then

1. downloads pending media from WAHA right away (WAHA keeps files only for
   WHATSAPP_FILES_LIFETIME), without the pipeline lock, so a long manual import
   cannot make files expire;
2. processes the queue strictly in arrival order under the pipeline lock:
   ingest → pin (📸) → remove (❌ by sender/admin, "delete for everyone").

Payload layout verified against WAHA 2026.9.2 / GOWS (tests/fixtures):
- message ids are "<fromMe>_<chat>_<ID>_<participant>"; reactions reference
  their target with wrong fromMe/participant, so only <ID> is used as key;
- albums send a header message without media, then one message per item;
- senders are LIDs (…@lid); only their salted hash is ever written to disk.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
import re
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests

from . import jobs
from .announce import Announcer
from .config import Config
from .db import Database, State
from .events import MANUAL_PREFIX, EventCalendar, sanitize
from .pipeline import IncomingMedia, Pipeline, hash_sender
from .sinks import Sink

log = logging.getLogger(__name__)

PIN = "📸"
REMOVE = "❌"
MAX_ATTEMPTS = 5
BACKLOG_ALERT = timedelta(hours=2)
_CALENDAR_TTL = timedelta(minutes=30)
_ADMINS_TTL = timedelta(hours=1)
_DOWNLOAD_TIMEOUT = 120
_IDLE_SECONDS = 30
COMMAND_WORD = "ordner"
FOLDER_PAST_DAYS = 30      # "Ordner 2026-09-20 …" accepted back to this many days
FOLDER_FUTURE_DAYS = 90    # … and ahead (an outing without calendar entry)
USAGE = (
    "Befehl: Ordner <Name> – für heute\n"
    "Ordner gestern <Name>\n"
    "Ordner 2026-07-11 <Name> (oder 11.07.2026)"
)
_DAY_TOKEN = re.compile(r"^(?:(?P<iso>\d{4}-\d{1,2}-\d{1,2})|(?P<de>\d{1,2}\.\d{1,2}\.\d{4}))$")


def message_key(serialized: str | None) -> str | None:
    """'false_<chat>@g.us_<ID>_<participant>' → '<ID>'; plain ids pass through."""
    if not serialized:
        return None
    parts = serialized.split("_")
    if len(parts) >= 3 and parts[0] in ("true", "false"):
        return parts[2]
    return serialized


def _emoji(text: str) -> str:
    return text.replace("️", "").strip()  # variation selector


OPTOUT_WORDS = {"stopp": True, "stop": True, "start": False}
OPTOUT_REPLY = {
    True: "Alles klar: Deine Fotos und Videos aus der Gruppe werden ab jetzt nicht mehr in die Galerie "
          "übernommen. Mit „Start“ machst du das rückgängig. Bereits übernommene Bilder entfernt "
          "die Ansprechperson auf Wunsch.",
    False: "Danke! Deine Fotos und Videos aus der Gruppe werden wieder in die Galerie übernommen. "
           "Mit „Stopp“ schaltest du das ab.",
}


def parse_optout(text: str) -> bool | None:
    """'Stopp' / 'Stop' -> True (opt out), 'Start' -> False, anything else -> None."""
    word = text.strip().strip("!.").lower()
    return OPTOUT_WORDS.get(word)


def sender_ids(payload: dict[str, Any]) -> list[str]:
    """Every id WhatsApp gives for the sender of a direct message (LID and phone
    JID), so an opt-out matches the LID used in group messages."""
    info = (payload.get("_data") or {}).get("Info") or {}
    raw = [payload.get("from"), info.get("Sender"), info.get("SenderAlt"), info.get("Chat")]
    ids: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or "@" not in value or value.endswith("@g.us"):
            continue
        user, _, server = value.partition("@")
        user = user.split(":")[0]  # device suffix "49…:12@s.whatsapp.net"
        ids.add(f"{user}@{server}")
        if server == "s.whatsapp.net":
            ids.add(f"{user}@c.us")
    return sorted(ids)


def parse_folder_command(text: str, sent_at: datetime, cfg: Config) -> tuple[date, str] | str | None:
    """'Ordner [heute|gestern|YYYY-MM-DD|DD.MM.YYYY] <Name>' → (day, title).

    Returns None for any other text, an error message (str) for a malformed
    command. "heute" follows DAY_CUTOFF_HOUR: at 01:00 it still means yesterday.
    """
    words = text.strip().split()
    if not words or words[0].lower().lstrip("/#!") != COMMAND_WORD:
        return None
    today = (sent_at - timedelta(hours=cfg.day_cutoff_hour)).date()
    rest = words[1:]
    day = today
    if rest and rest[0].lower() in ("heute", "gestern"):
        day = today if rest[0].lower() == "heute" else today - timedelta(days=1)
        rest = rest[1:]
    elif rest and (match := _DAY_TOKEN.match(rest[0])):
        try:
            if match["iso"]:
                day = date.fromisoformat("-".join(f"{int(x):02d}" for x in match["iso"].split("-")))
            else:
                d, m, y = (int(x) for x in match["de"].split("."))
                day = date(y, m, d)
        except ValueError:
            return f"Ungültiges Datum: {rest[0]}\n\n{USAGE}"
        rest = rest[1:]
    if not rest:
        return f"Name fehlt.\n\n{USAGE}"
    title = sanitize(" ".join(rest))
    if not (today - timedelta(days=FOLDER_PAST_DAYS) <= day <= today + timedelta(days=FOLDER_FUTURE_DAYS)):
        return f"Datum {day:%d.%m.%Y} liegt zu weit zurück oder voraus.\n\n{USAGE}"
    return day, title


def tasks_from_webhook(cfg: Config, data: dict[str, Any], received_ms: int) -> list[dict[str, Any]]:
    """Translate one webhook into queue tasks; pure, no I/O. Unrelated events → []."""
    group = cfg.group_chat_id
    payload = data.get("payload")
    if not group or not isinstance(payload, dict):
        return []
    event = data.get("event")
    session = str(data.get("session") or "default")
    base = {"session": session, "chat_id": group, "received_ms": received_ms, "attempts": 0}

    if event == "message" and _is_direct_chat(payload.get("from")) and not payload.get("fromMe"):
        # Direct message to the bot: only admin commands, nothing else is kept.
        sent_at = datetime.fromtimestamp(int(payload.get("timestamp") or received_ms // 1000), cfg.tz)
        text = str(payload.get("body") or "")
        optout = parse_optout(text)
        if optout is not None:  # any member, not only admins
            return [{**base, "kind": "optout", "target": message_key(payload.get("id")) or str(received_ms),
                     "opt_out": optout, "reply_to": payload["from"],
                     "sender_hashes": [hash_sender(cfg.sender_salt, i) for i in sender_ids(payload)]}]
        parsed = parse_folder_command(text, sent_at, cfg)
        if parsed is None:
            return []
        task = {**base, "kind": "command", "target": message_key(payload.get("id")) or str(received_ms),
                "sender_hash": hash_sender(cfg.sender_salt, payload["from"]),
                "reply_to": payload["from"]}  # needed to answer; deleted with the task
        if isinstance(parsed, str):
            return [{**task, "error": parsed}]
        return [{**task, "day": parsed[0].isoformat(), "title": parsed[1]}]

    if event == "message":
        media = payload.get("media") or {}
        mimetype = str(media.get("mimetype") or "")
        if (
            payload.get("from") != group
            or payload.get("fromMe")
            or not payload.get("hasMedia")
            or not media.get("url")
            or not mimetype.startswith(("image/", "video/"))
        ):
            return []  # texts, album headers, stickers, own messages, other chats
        video = (((payload.get("_data") or {}).get("Message") or {}).get("videoMessage") or {})
        if video.get("gifPlayback"):
            return []  # WhatsApp GIF (keyboard GIF search): practically always a meme
        msg_id = message_key(payload.get("id"))
        sender = payload.get("participant") or payload.get("author")
        if not msg_id or not sender:
            return []
        return [{
            **base,
            "kind": "media",
            "msg_id": msg_id,
            "sender_hash": hash_sender(cfg.sender_salt, sender),
            "sent_at": int(payload.get("timestamp") or received_ms // 1000),
            "url": media["url"],
            "suffix": _suffix(media["url"], media.get("filename"), mimetype),
        }]

    if event == "message.reaction":
        reaction = payload.get("reaction") or {}
        emoji = _emoji(str(reaction.get("text") or ""))
        target = message_key(reaction.get("messageId"))
        sender = payload.get("participant") or payload.get("author")
        if payload.get("from") != group or emoji not in (PIN, REMOVE) or not target or not sender:
            return []  # other emojis and removed reactions ("") are ignored
        return [{**base, "kind": "reaction", "emoji": emoji, "target": target,
                 "sender_hash": hash_sender(cfg.sender_salt, sender)}]

    if event == "message.revoked":
        message = payload.get("after") or payload.get("before") or {}
        target = payload.get("revokedMessageId")
        if message.get("from") != group or not target:
            return []
        return [{**base, "kind": "revoke", "target": str(target)}]

    return []


def _is_direct_chat(chat: Any) -> bool:
    return isinstance(chat, str) and chat.endswith(("@c.us", "@lid", "@s.whatsapp.net"))


def _suffix(url: str, filename: str | None, mimetype: str) -> str:
    for name in (urlparse(url).path, filename or ""):
        suffix = PurePosixPath(name).suffix.lower()
        if suffix:
            return suffix
    return mimetypes.guess_extension(mimetype) or ".bin"


@dataclass
class _Cached:
    value: Any = None
    at: datetime | None = None


@dataclass
class LiveIntake:
    cfg: Config
    gallery: Sink
    _wake: threading.Event = field(default_factory=threading.Event)
    _calendar: _Cached = field(default_factory=_Cached)
    _admins: _Cached = field(default_factory=_Cached)
    processed: int = 0
    _order_lock: threading.Lock = field(default_factory=threading.Lock)
    _last_stamp: int = 0
    announcer: Announcer | None = None

    def __post_init__(self) -> None:
        if self.announcer is None:
            self.announcer = Announcer(self.cfg)
        for directory in (self.queue_dir, self.media_dir, self.failed_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def queue_dir(self) -> Path:
        return self.cfg.live_dir / "queue"

    @property
    def media_dir(self) -> Path:
        return self.cfg.live_dir / "media"

    @property
    def failed_dir(self) -> Path:
        return self.cfg.live_dir / "failed"

    # --- queue -----------------------------------------------------------------

    def enqueue(self, tasks: list[dict[str, Any]]) -> None:
        """File names start with a strictly increasing µs stamp: arrival order = name order."""
        with self._order_lock:
            for task in tasks:
                stamp = max(time.time_ns() // 1000, self._last_stamp + 1)
                self._last_stamp = stamp
                key = task.get("msg_id") or task["target"]
                _write_json(self.queue_dir / f"{stamp:017d}_{task['kind']}_{key}.json", task)
        if tasks:
            self._wake.set()

    def _queued(self) -> list[Path]:
        return sorted(self.queue_dir.glob("*.json"))

    def stats(self) -> dict[str, Any]:
        queued = self._queued()
        oldest = None
        if queued:
            oldest = int(time.time() - int(queued[0].name.split("_", 1)[0]) / 1_000_000)
        return {
            "queued": len(queued),
            "oldest_seconds": oldest,
            "failed": sum(1 for _ in self.failed_dir.glob("*.json")),
            "processed": self.processed,
        }

    def problems(self) -> list[str]:
        stats = self.stats()
        problems = []
        if stats["oldest_seconds"] is not None and stats["oldest_seconds"] > BACKLOG_ALERT.total_seconds():
            hours = stats["oldest_seconds"] // 3600
            problems.append(f"{stats['queued']} WhatsApp-Nachrichten warten seit {hours} h auf Verarbeitung")
        if stats["failed"]:
            problems.append(
                f"{stats['failed']} WhatsApp-Nachrichten konnten nicht übernommen werden (data/worker/live/failed)"
            )
        return problems

    # --- thread ----------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        log.info("Live intake started for %s", self.cfg.group_chat_id)
        while not stop.is_set():
            try:
                self.process_once()
                self.announce_due()
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("live intake failed")
            self._wake.wait(_IDLE_SECONDS)
            self._wake.clear()

    def process_once(self) -> int:
        """Download what is pending, then process in order. Returns tasks done."""
        files = self._queued()
        if not files:
            return 0
        for path in files:
            self._download(path)
        try:
            with jobs.pipeline_lock(self.cfg):
                db = Database(self.cfg.db_path)
                try:
                    pipeline = Pipeline(self.cfg, db, self._get_calendar(), self.gallery)
                    return self._process(pipeline, db)
                finally:
                    db.close()
        except jobs.Busy:
            log.debug("live intake waits for the pipeline lock")
            return 0

    def announce_due(self) -> int:
        """Send scheduled group messages (outside quiet hours); no pipeline lock needed."""
        if not self.announcer.enabled:
            return 0
        db = Database(self.cfg.db_path)
        try:
            return self.announcer.send_due(db)
        finally:
            db.close()

    def _download(self, path: Path) -> None:
        task = _read_json(path)
        if task is None or task["kind"] != "media" or task.get("file"):
            return
        target = self.media_dir / f"{task['msg_id']}{task['suffix']}"
        try:
            response = requests.get(
                task["url"], headers={"X-Api-Key": self.cfg.waha_api_key or ""}, timeout=_DOWNLOAD_TIMEOUT
            )
            if response.status_code in (404, 410):
                self._fail(path, task, f"media expired at WAHA (HTTP {response.status_code})")
                return
            response.raise_for_status()
        except requests.RequestException as exc:
            self._retry(path, task, f"download: {type(exc).__name__}")
            return
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(response.content)
        os.replace(tmp, target)
        task["file"] = str(target)
        _write_json(path, task)

    def _process(self, pipeline: Pipeline, db: Database) -> int:
        done = 0
        for path in self._queued():
            task = _read_json(path)
            if task is None:
                continue
            if task["kind"] == "media" and not task.get("file"):
                break  # keep order: a reaction must not overtake its photo
            try:
                result = self._handle(pipeline, db, task)
            except Exception as exc:  # noqa: BLE001 - retried, then parked in failed/
                log.warning("live task %s failed: %s", path.name, exc)
                if not self._retry(path, task, f"{type(exc).__name__}: {exc}"[:300]):
                    continue  # parked; go on with the next one
                break
            log.info("live %s %s: %s", task["kind"], task.get("msg_id") or task["target"], result)
            if result in State.PUBLISHED and task["kind"] in ("media", "reaction"):
                self.announcer.consider(db, f"waha:{task.get('msg_id') or task['target']}", task["session"])
            if task.get("file"):
                Path(task["file"]).unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            done += 1
            self.processed += 1
        return done

    def _handle(self, pipeline: Pipeline, db: Database, task: dict[str, Any]) -> str:
        if task["kind"] == "media":
            item = IncomingMedia(
                source="waha",
                source_id=task["msg_id"],
                chat_id=task["chat_id"],
                sender="",
                sender_hash=task["sender_hash"],
                sent_at=datetime.fromtimestamp(task["sent_at"], self.cfg.tz),
                path=Path(task["file"]),
                seq=task["received_ms"] % 100_000,
            )
            return pipeline.ingest(item)

        if task["kind"] == "command":
            return self._command(pipeline, db, task)
        if task["kind"] == "optout":
            db.set_optout(task["sender_hashes"], task["opt_out"])
            self._reply(task, OPTOUT_REPLY[task["opt_out"]])
            return "opted out" if task["opt_out"] else "opted in"

        media_id = f"waha:{task['target']}"
        if not db.exists(media_id):
            return "unknown target"
        if task["kind"] == "revoke":
            return pipeline.remove(media_id)
        if task["emoji"] == PIN:
            return pipeline.pin(media_id)
        row = db.get(media_id)
        if task["sender_hash"] == row["sender_hash"] or task["sender_hash"] in self._get_admins(task["session"]):
            return pipeline.remove(media_id)
        return "not allowed"

    def _command(self, pipeline: Pipeline, db: Database, task: dict[str, Any]) -> str:
        """Admin command 'Ordner …': an event for a day without calendar entry."""
        if task["sender_hash"] not in self._get_admins(task["session"]):
            return "command ignored: not a group admin"  # no answer to strangers
        if task.get("error"):
            self._reply(task, task["error"])
            return "command rejected"
        day = date.fromisoformat(task["day"])
        start = datetime.combine(day, dtime(self.cfg.day_cutoff_hour), self.cfg.tz)
        end = start + timedelta(days=1)
        key = f"{MANUAL_PREFIX}{day.isoformat()}:{task['title'].lower()}"
        created = db.add_manual_event(key, task["title"], int(start.timestamp()), int(end.timestamp()))
        event = jobs.manual_event(key, task["title"], int(start.timestamp()), int(end.timestamp()), self.cfg)
        adopted = pipeline.add_event(event)
        self._calendar.value = pipeline.calendar  # later items of this run see the event too
        folder = db.event_folder(key, event.folder_name(), event.title, int(start.timestamp()))
        lines = [f"Ordner „{folder}“ {'angelegt' if created else 'gibt es schon'}."]
        if adopted:
            lines.append(f"{adopted} Bild(er) aus der Quarantäne übernommen.")
        lines.append("Fotos dieses Tages landen ab jetzt dort.")
        self._reply(task, " ".join(lines))
        return f"folder {folder} ({'new' if created else 'exists'}, {adopted} adopted)"

    def _reply(self, task: dict[str, Any], text: str) -> None:
        try:
            response = requests.post(
                f"{(self.cfg.waha_url or '').rstrip('/')}/api/sendText",
                headers={"X-Api-Key": self.cfg.waha_api_key or ""},
                json={"session": task["session"], "chatId": task["reply_to"], "text": text},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("reply to admin failed: %s", exc)  # the command itself is done

    # --- failure handling ------------------------------------------------------------

    def _retry(self, path: Path, task: dict[str, Any], error: str) -> bool:
        """Count a failed attempt; returns False once the task is parked in failed/."""
        task["attempts"] = task.get("attempts", 0) + 1
        task["error"] = error
        if task["attempts"] >= MAX_ATTEMPTS:
            self._fail(path, task, error)
            return False
        _write_json(path, task)
        return True

    def _fail(self, path: Path, task: dict[str, Any], error: str) -> None:
        task["error"] = error
        log.error("live task %s given up: %s", path.name, error)
        if task.get("file"):
            Path(task["file"]).unlink(missing_ok=True)  # no media kept outside the pipeline
            task["file"] = None
        _write_json(self.failed_dir / path.name, task)
        path.unlink(missing_ok=True)

    # --- cached lookups ----------------------------------------------------------------

    def _get_calendar(self) -> EventCalendar:
        now = datetime.now(self.cfg.tz)
        if self._calendar.at is None or now - self._calendar.at > _CALENDAR_TTL:
            # EXIF of documents may be ~90 days old; late batches up to LATE_GRACE.
            self._calendar.value = jobs.build_calendar(self.cfg, now - timedelta(days=120), now + timedelta(days=1))
            self._calendar.at = now
        return self._calendar.value

    def _get_admins(self, session: str) -> set[str]:
        now = datetime.now(self.cfg.tz)
        if self._admins.at is None or now - self._admins.at > _ADMINS_TTL:
            try:
                ids = group_admins(self.cfg, session)
            except (requests.RequestException, ValueError) as exc:
                log.warning("group admins unavailable (%s): ❌ only works for the sender", exc)
                return self._admins.value or set()
            self._admins.value = {hash_sender(self.cfg.sender_salt, i) for i in ids}
            self._admins.at = now
            log.info("group admins: %d ids known (LID and phone count separately)", len(ids))
        return self._admins.value


def group_admins(cfg: Config, session: str) -> set[str]:
    """Ids (LID and phone JID, whatever WAHA returns) of the group's admins."""
    response = requests.get(
        f"{(cfg.waha_url or '').rstrip('/')}/api/{session}/groups/{cfg.group_chat_id}/participants",
        headers={"X-Api-Key": cfg.waha_api_key or ""},
        timeout=30,
    )
    response.raise_for_status()
    return admin_ids(response.json())


def admin_ids(participants: Any) -> set[str]:
    """Tolerant parser: list of {id|JID|LID|PhoneNumber, role|IsAdmin|IsSuperAdmin}."""
    if isinstance(participants, dict):
        participants = participants.get("participants") or list(participants.values())
    ids: set[str] = set()
    for entry in participants or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or entry.get("rank") or "").lower()
        if not (role in ("admin", "superadmin") or entry.get("IsAdmin") or entry.get("IsSuperAdmin")
                or entry.get("isAdmin") or entry.get("isSuperAdmin")):
            continue
        for key in ("id", "JID", "jid", "LID", "lid", "PhoneNumber", "phoneNumber"):
            value = entry.get(key)
            if isinstance(value, dict):
                value = value.get("_serialized")
            if isinstance(value, str) and "@" in value:
                ids.add(value)
                ids.add(value.replace("@s.whatsapp.net", "@c.us"))
    return ids


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
