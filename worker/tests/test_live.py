"""Live intake (phase 2) against anonymised WAHA 2026.9.2 / GOWS payloads."""

import hashlib
import hmac
import json
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from conftest import TZ, event, published

from gruppix import live
from gruppix.db import Database, State
from gruppix.events import EventCalendar
from gruppix.live import LiveIntake, admin_ids, message_key, tasks_from_webhook
from gruppix.sinks import LocalSink
from gruppix.webhook import WebhookInbox

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "waha_gows_2026.9.2.json").read_text())
BY_NAME = {f["name"]: f["body"] for f in FIXTURES}
GROUP = "120363000000000001@g.us"
KEY = "hook-key"
EVENING = event("probe", "Herbstkonzert", datetime(2026, 10, 14, 19, tzinfo=TZ), datetime(2026, 10, 14, 23, 30, tzinfo=TZ))


@pytest.fixture
def live_cfg(cfg):
    return replace(cfg, group_chat_id=GROUP, live_ingest=True, hook_hmac_key=KEY,
                   waha_url="http://waha:3000", waha_api_key="k")


def test_message_key_ignores_wrong_fromme_in_reactions():
    reaction_target = BY_NAME["reaction_camera"]["payload"]["reaction"]["messageId"]
    photo_id = BY_NAME["image_reacted"]["payload"]["id"]
    assert reaction_target.startswith("true_") and photo_id.startswith("false_")
    assert message_key(reaction_target) == message_key(photo_id) == "AC85F5C07F46367E265D66F179462904"
    assert message_key("ABC") == "ABC" and message_key(None) is None


def test_tasks_from_captured_payloads(live_cfg):
    kinds = {}
    for name, body in BY_NAME.items():
        tasks = tasks_from_webhook(live_cfg, body, 1792012262000)
        kinds[name] = [t["kind"] for t in tasks]
        assert "@lid" not in json.dumps(tasks)  # senders only as hash
    assert kinds == {
        "image": ["media"], "image_hd": ["media"], "document_jpeg": ["media"], "album_header": [],
        "album_item": ["media"], "video": ["media"], "image_with_caption": ["media"], "text": [],
        "image_reacted": ["media"], "reaction_camera": ["reaction"], "image_revoked": ["media"],
        "reaction_cross": ["reaction"], "reaction_removed": [], "revoked": ["revoke"],
    }
    caption = tasks_from_webhook(live_cfg, BY_NAME["image_with_caption"], 0)[0]
    assert "caption" not in json.dumps(caption)  # texts are never kept
    assert tasks_from_webhook(live_cfg, BY_NAME["video"], 0)[0]["suffix"] == ".mp4"
    assert tasks_from_webhook(replace(live_cfg, group_chat_id="other@g.us"), BY_NAME["image"], 0) == []


def test_admin_ids_tolerant():
    assert admin_ids([{"id": "1@c.us", "role": "admin"}, {"id": "2@c.us", "role": "participant"}]) == {"1@c.us"}
    gows = [{"JID": "49@s.whatsapp.net", "LID": "7@lid", "IsAdmin": True}, {"JID": "50@s.whatsapp.net"}]
    assert admin_ids(gows) == {"49@s.whatsapp.net", "49@c.us", "7@lid"}


def _sign(body: bytes) -> dict[str, str]:
    return {"X-Webhook-Hmac": hmac.new(KEY.encode(), body, hashlib.sha512).hexdigest(),
            "X-Webhook-Hmac-Algorithm": "sha512"}


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content, self.status_code = content, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise live.requests.HTTPError(str(self.status_code))


@pytest.fixture
def waha_files(monkeypatch, make_jpeg, tmp_path):
    """WAHA file server: a different image per URL, a real tiny MP4 for videos."""
    video = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=5",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    served: dict[str, bytes] = {}

    def fake_get(url, **_):
        if url not in served:
            served[url] = video.read_bytes() if url.endswith(".mp4") else make_jpeg().read_bytes()
        return _FakeResponse(served[url])

    monkeypatch.setattr(live.requests, "get", fake_get)
    monkeypatch.setattr(live, "group_admins", lambda cfg, session: set())
    return served


def _intake(cfg, gallery):
    intake = LiveIntake(cfg, LocalSink(gallery))
    calendar = EventCalendar([EVENING], cfg.window_pre, cfg.window_post, cfg.late_grace)
    intake._get_calendar = lambda: calendar
    inbox = WebhookInbox(cfg)
    inbox.live = intake
    return intake, inbox


def test_end_to_end_with_captured_sequence(live_cfg, gallery, waha_files):
    intake, inbox = _intake(live_cfg, gallery)
    for item in FIXTURES:
        body = json.dumps(item["body"]).encode()
        code, _ = inbox.handle(body, _sign(body))
        assert code == 202
    assert intake.stats()["queued"] == 11          # 8 media, 2 reactions, 1 revoke
    assert intake.process_once() == 11
    assert intake.stats() == {"queued": 0, "oldest_seconds": None, "failed": 0, "processed": 11}

    db = Database(live_cfg.db_path)
    try:
        states = {r["id"]: r["state"] for r in db._conn.execute("SELECT id, state FROM media")}
    finally:
        db.close()
    revoked = "waha:ACDC17E7561F106AB4A2EE23EABCEEFB"
    assert states.pop(revoked) == State.REMOVED          # ❌ by its sender
    assert set(states.values()) == {State.UPLOADED}       # 6 images + 1 video
    files = published(gallery)
    assert len(files) == 7
    assert any(f.endswith(".mp4") for f in files)
    assert all(f.startswith("Fotos/2026/2026-10-14 Herbstkonzert/") for f in files)
    # nothing left behind outside the pipeline
    assert list(intake.media_dir.iterdir()) == []


def test_cross_from_someone_else_is_ignored_and_bad_signature_rejected(live_cfg, gallery, waha_files):
    intake, inbox = _intake(replace(live_cfg, min_event_items=1), gallery)
    photo = json.dumps(BY_NAME["image_revoked"]).encode()
    cross = BY_NAME["reaction_cross"]
    cross = {**cross, "payload": {**cross["payload"], "participant": "999@lid"}}
    cross_body = json.dumps(cross).encode()
    assert inbox.handle(cross_body, {"X-Webhook-Hmac": "00"})[0] == 401
    inbox.handle(photo, _sign(photo))
    inbox.handle(cross_body, _sign(cross_body))
    assert intake.process_once() == 2
    assert len(published(gallery)) == 1


def test_expired_media_is_parked(live_cfg, gallery, monkeypatch):
    intake, inbox = _intake(live_cfg, gallery)
    monkeypatch.setattr(live.requests, "get", lambda url, **_: _FakeResponse(b"", 404))
    body = json.dumps(BY_NAME["image"]).encode()
    inbox.handle(body, _sign(body))
    intake.process_once()
    stats = intake.stats()
    assert stats["queued"] == 0 and stats["failed"] == 1
    assert intake.problems() == [
        "1 WhatsApp-Nachrichten konnten nicht übernommen werden (data/worker/live/failed)"
    ]


# --- admin command "Ordner …" (direct message to the bot) ----------------------------

ADMIN = "100000000000001@lid"


def _dm(text: str, when: datetime, sender: str = ADMIN) -> dict:
    """Direct message in the shape of the captured group messages (from = the chat)."""
    return {"event": "message", "session": "default", "payload": {
        "id": f"false_{sender}_CMD{int(when.timestamp())}", "timestamp": int(when.timestamp()),
        "from": sender, "fromMe": False, "to": "4900000000001@c.us", "body": text,
        "hasMedia": False, "media": None}}


@pytest.mark.parametrize("text, expected", [
    ("Ordner Grillabend", (datetime(2026, 10, 15).date(), "Grillabend")),
    ("ordner gestern Probe am See", (datetime(2026, 10, 14).date(), "Probe am See")),
    ("/Ordner 2026-10-03 Ausflug", (datetime(2026, 10, 3).date(), "Ausflug")),
    ("Ordner 3.10.2026 Ausflug", (datetime(2026, 10, 3).date(), "Ausflug")),
    ("Ordner 2026-12-24 Weihnachtsfeier", (datetime(2026, 12, 24).date(), "Weihnachtsfeier")),
])
def test_folder_command_parsing(live_cfg, text, expected):
    sent = datetime(2026, 10, 15, 20, tzinfo=TZ)
    assert live.parse_folder_command(text, sent, live_cfg) == expected


def test_folder_command_errors_and_cutoff(live_cfg):
    sent = datetime(2026, 10, 15, 20, tzinfo=TZ)
    assert live.parse_folder_command("Hallo Bot", sent, live_cfg) is None
    assert live.parse_folder_command("Ordner", sent, live_cfg).startswith("Name fehlt")
    assert live.parse_folder_command("Ordner 2026-02-30 X", sent, live_cfg).startswith("Ungültiges Datum")
    assert "zu weit" in live.parse_folder_command("Ordner 2025-01-01 X", sent, live_cfg)
    # 01:00 after a party still counts as the evening before (DAY_CUTOFF_HOUR=5)
    night = datetime(2026, 10, 16, 1, tzinfo=TZ)
    assert live.parse_folder_command("Ordner Fest", night, live_cfg)[0] == datetime(2026, 10, 15).date()


def test_folder_command_adopts_quarantine_and_answers(live_cfg, gallery, waha_files, monkeypatch):
    sent = []
    monkeypatch.setattr(live.requests, "post", lambda url, json=None, **_: sent.append((url, json)) or _FakeResponse(b""))
    monkeypatch.setattr(live, "group_admins", lambda cfg, session: {ADMIN})
    intake = LiveIntake(live_cfg, LocalSink(gallery))
    intake._get_calendar = lambda: EventCalendar([], live_cfg.window_pre, live_cfg.window_post, live_cfg.late_grace)
    inbox = WebhookInbox(live_cfg)
    inbox.live = intake

    photo = json.dumps(BY_NAME["image"]).encode()     # 14.10.2026 23:11, no calendar event
    inbox.handle(photo, _sign(photo))
    intake.process_once()
    assert published(gallery) == []                   # quarantined

    stranger = json.dumps(_dm("Ordner gestern Test", datetime(2026, 10, 15, 9, tzinfo=TZ), "999@lid")).encode()
    command = json.dumps(_dm("Ordner gestern Herbstabend", datetime(2026, 10, 15, 9, tzinfo=TZ))).encode()
    chatter = json.dumps(_dm("Danke!", datetime(2026, 10, 15, 9, 1, tzinfo=TZ))).encode()
    for body in (stranger, command, chatter):
        inbox.handle(body, _sign(body))
    assert intake.stats()["queued"] == 2               # "Danke!" never becomes a task
    assert "Danke" not in "".join(p.read_text() for p in intake.queue_dir.glob("*.json"))
    intake.process_once()

    files = published(gallery)
    assert len(files) == 1 and files[0].startswith("Fotos/2026/2026-10-14 Herbstabend/")
    assert len(sent) == 1                               # only the admin gets an answer
    url, payload = sent[0]
    assert url.endswith("/api/sendText") and payload["chatId"] == ADMIN
    assert "2026-10-14 Herbstabend" in payload["text"] and "1 Bild" in payload["text"]

    # The event is persistent: the next photo of that evening goes straight into the folder.
    db = Database(live_cfg.db_path)
    try:
        assert [r["title"] for r in db.manual_events(0, 2**31)] == ["Herbstabend"]
    finally:
        db.close()


def test_manual_events_are_part_of_every_calendar(cfg):
    from gruppix import jobs

    start = datetime(2026, 10, 14, 5, tzinfo=TZ)
    db = Database(cfg.db_path)
    try:
        assert db.add_manual_event("manual:2026-10-14:fest", "Fest", int(start.timestamp()), int(start.timestamp()) + 86400)
        assert not db.add_manual_event("manual:2026-10-14:fest", "Fest", 0, 0)  # idempotent
    finally:
        db.close()
    calendar = jobs.build_calendar(cfg, datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 10, 31, tzinfo=TZ))
    event = calendar.match(datetime(2026, 10, 14, 22, tzinfo=TZ))
    assert event is not None and event.folder_name() == "2026-10-14 Fest"


# --- opt-out by direct message ("Stopp" / "Start") -----------------------------------

def test_optout_parsing_and_sender_ids():
    assert live.parse_optout(" Stopp! ") is True and live.parse_optout("start") is False
    assert live.parse_optout("Stopp bitte nicht") is None
    payload = {"from": "4900000000002@c.us",
               "_data": {"Info": {"Sender": "4900000000002:3@s.whatsapp.net", "SenderAlt": "100000000000001@lid"}}}
    assert live.sender_ids(payload) == ["100000000000001@lid", "4900000000002@c.us", "4900000000002@s.whatsapp.net"]


def test_stopp_skips_own_photos_until_start(live_cfg, gallery, waha_files, monkeypatch):
    sent = []
    monkeypatch.setattr(live.requests, "post", lambda url, json=None, **_: sent.append(json) or _FakeResponse(b""))
    intake, inbox = _intake(replace(live_cfg, min_event_items=1), gallery)

    def feed(body: dict) -> None:
        raw = json.dumps(body).encode()
        inbox.handle(raw, _sign(raw))

    # The DM arrives with the phone JID; the LID used in the group is in SenderAlt.
    stop = _dm("Stopp", datetime(2026, 10, 14, 20, tzinfo=TZ), "4900000000002@c.us")
    stop["payload"]["_data"] = {"Info": {"SenderAlt": ADMIN}}
    feed(stop)
    feed(BY_NAME["image"])                                   # posted by ADMIN's LID
    intake.process_once()
    assert published(gallery) == []
    assert "nicht mehr" in sent[0]["text"]

    feed(_dm("Start", datetime(2026, 10, 14, 21, tzinfo=TZ), ADMIN))
    feed(BY_NAME["image_hd"])
    intake.process_once()
    assert len(published(gallery)) == 1
    assert "wieder" in sent[1]["text"]


# --- bot message when an event folder fills up (ANNOUNCE) ----------------------------

def test_quiet_hours():
    from gruppix.announce import next_allowed

    d = lambda day, h: datetime(2026, 10, day, h, tzinfo=TZ)  # noqa: E731
    assert next_allowed(d(14, 23), (22, 9)) == d(15, 9)
    assert next_allowed(d(15, 3), (22, 9)) == d(15, 9)
    assert next_allowed(d(15, 12), (22, 9)) == d(15, 12)
    assert next_allowed(d(15, 3), (0, 0)) == d(15, 3)


def test_one_message_per_event_with_folder_link(live_cfg, gallery, waha_files, monkeypatch):
    posts = []
    monkeypatch.setattr(live.requests, "post", lambda *a, **k: _FakeResponse(b""))
    import gruppix.announce as announce
    monkeypatch.setattr(announce.requests, "post", lambda url, json=None, **_: posts.append(json) or _FakeResponse(b""))
    cfg = replace(live_cfg, announce=True, announce_min_items=3, announce_quiet=(0, 0),
                  announce_max_age_days=100_000, announce_url="https://cloud.example/s/TOKEN?dir={path}")
    intake, inbox = _intake(cfg, gallery)

    def feed(*names):
        for name in names:
            raw = json.dumps(BY_NAME[name]).encode()
            inbox.handle(raw, _sign(raw))
        intake.process_once()
        intake.announce_due()

    feed("image", "image_hd")                        # 2 items: held, nothing published yet
    assert posts == []
    feed("document_jpeg")                            # 3rd item releases the folder -> one message
    assert len(posts) == 1
    text = posts[0]["text"]
    assert posts[0]["chatId"] == GROUP
    assert "„Herbstkonzert“ mit 3 Bildern" in text
    assert "https://cloud.example/s/TOKEN?dir=%2F2026%2F2026-10-14%20Herbstkonzert" in text
    feed("album_item", "image_with_caption")         # more photos: no second message
    assert len(posts) == 1


def test_no_message_for_old_events_or_when_off(live_cfg, db, make_pipeline, tmp_path):
    from gruppix.announce import Announcer
    from gruppix.pipeline import IncomingMedia

    pipe = make_pipeline(EVENING)
    for n in range(3):
        pipe.ingest(IncomingMedia("waha", f"OLD{n}", GROUP, "A", datetime(2026, 10, 14, 20, n, tzinfo=TZ),
                                  _jpeg(tmp_path, n)))
    on = replace(live_cfg, announce=True, announce_min_items=3, announce_url="https://x/s/T?dir={path}")
    later = datetime(2026, 11, 30, 12, tzinfo=TZ)
    assert Announcer(on).consider(db, "waha:OLD2", "default", now=later) is False     # event too old
    assert Announcer(replace(on, announce=False)).consider(db, "waha:OLD2", "default") is False
    assert Announcer(on).consider(db, "waha:OLD2", "default", now=datetime(2026, 10, 15, 10, tzinfo=TZ)) is True


def _jpeg(tmp_path, n):
    from PIL import Image

    path = tmp_path / f"old{n}.jpg"
    Image.new("RGB", (64, 48), (n * 60, 10, 10)).save(path, "JPEG")
    return path


def test_whatsapp_gifs_are_ignored(live_cfg):
    gif = json.loads(json.dumps(BY_NAME["video"]))
    gif["payload"]["_data"] = {"Message": {"videoMessage": {"gifPlayback": True, "seconds": 2}}}
    assert tasks_from_webhook(live_cfg, gif, 0) == []
    assert [t["kind"] for t in tasks_from_webhook(live_cfg, BY_NAME["video"], 0)] == ["media"]
