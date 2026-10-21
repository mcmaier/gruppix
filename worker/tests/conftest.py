"""Shared fixtures: isolated config, database, pipeline and test images."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import piexif
import pytest
from PIL import Image

from gruppix.config import Config
from gruppix.db import Database
from gruppix.events import Event, EventCalendar
from gruppix.pipeline import IncomingMedia, Pipeline
from gruppix.sinks import LocalSink

TZ = ZoneInfo("Europe/Berlin")


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    for name in ("CALENDAR_ICS_URLS", "EVENT_EXCLUDE_REGEX", "MQTT_HOST", "SMTP_HOST", "HEALTHCHECK_PING_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SENDER_SALT", "test-salt")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    return Config.from_env()


@pytest.fixture
def db(cfg: Config):
    database = Database(cfg.db_path)
    yield database
    database.close()


@pytest.fixture
def gallery(tmp_path: Path) -> Path:
    return tmp_path / "nextcloud"


def event(key: str, title: str, start: datetime, end: datetime) -> Event:
    return Event(key=key, title=title, start=start, end=end, all_day=False, priority=0)


@pytest.fixture
def make_pipeline(cfg: Config, db: Database, gallery: Path):
    def factory(*events: Event) -> Pipeline:
        calendar = EventCalendar(list(events), cfg.window_pre, cfg.window_post, cfg.late_grace)
        return Pipeline(cfg, db, calendar, LocalSink(gallery))

    return factory


@pytest.fixture
def make_jpeg(tmp_path: Path):
    """Create a JPEG; each call yields different pixels (unique hash)."""
    counter = {"n": 0}

    def factory(size=(800, 600), exif_date: str | None = None, gps: bool = False, quality: int = 80) -> Path:
        counter["n"] += 1
        n = counter["n"]
        path = tmp_path / "in" / f"IMG-{n:04d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"quality": quality}
        if exif_date or gps:
            data = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
            if exif_date:
                data["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_date.encode()
            if gps:
                data["GPS"] = {piexif.GPSIFD.GPSLatitudeRef: b"N", piexif.GPSIFD.GPSLatitude: ((48, 1), (37, 1), (0, 1))}
            kwargs["exif"] = piexif.dump(data)
        Image.new("RGB", size, ((n * 37) % 256, (n * 91) % 256, 128)).save(path, "JPEG", **kwargs)
        return path

    return factory


@pytest.fixture
def send(make_jpeg):
    """Simulate a WhatsApp message with one image."""
    counter = {"n": 0}

    def factory(pipeline: Pipeline, when: datetime, sender: str = "A", **image_kwargs) -> str:
        counter["n"] += 1
        path = make_jpeg(**image_kwargs)
        item = IncomingMedia("waha", f"msg{counter['n']}", "group", sender, when, path, seq=counter["n"])
        return pipeline.ingest(item)

    return factory


def published(gallery: Path) -> list[str]:
    return sorted(p.relative_to(gallery).as_posix() for p in gallery.rglob("*") if p.is_file())
