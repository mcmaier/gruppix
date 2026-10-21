"""Units of work shared by the CLI and the long-running service."""

from __future__ import annotations

import fcntl
import logging
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .db import Database
from .events import Event, EventCalendar, load_calendar
from .ingest_drop import calendar_range, ingest_drop, pending_groups
from .pipeline import Pipeline
from .sinks import LocalSink, Sink, WebDavSink

log = logging.getLogger(__name__)


class Busy(RuntimeError):
    """Another job holds the pipeline lock (e.g. a manual import is running)."""


@contextmanager
def pipeline_lock(cfg: Config) -> Iterator[None]:
    """Serialise all jobs that change media state, across processes.

    The service and manual `docker compose run` commands share one database
    and one upload folder; without the lock a file could be processed twice.
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    with (cfg.data_dir / ".pipeline.lock").open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("another job is running (pipeline lock held)") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def build_calendar(cfg: Config, start: datetime, end: datetime) -> EventCalendar:
    """Calendar sources plus the events admins created by command."""
    if not cfg.calendar_sources:
        log.warning("CALENDAR_ICS_URLS is empty: only admin-created events are known")
        calendar = empty_calendar(cfg)
    else:
        calendar = _load_sources(cfg, start, end)
    return calendar.with_events(manual_events(cfg, start, end))


def manual_events(cfg: Config, start: datetime, end: datetime) -> list[Event]:
    db = Database(cfg.db_path)
    try:
        rows = db.manual_events(int(start.timestamp()), int(end.timestamp()))
    finally:
        db.close()
    return [manual_event(r["event_key"], r["title"], r["start_ts"], r["end_ts"], cfg) for r in rows]


def manual_event(key: str, title: str, start_ts: int, end_ts: int, cfg: Config) -> Event:
    return Event(key=key, title=title, start=datetime.fromtimestamp(start_ts, cfg.tz),
                 end=datetime.fromtimestamp(end_ts, cfg.tz), all_day=True, priority=-1)


def _load_sources(cfg: Config, start: datetime, end: datetime) -> EventCalendar:
    return load_calendar(
        cfg.calendar_sources,
        cache_dir=cfg.cache_dir,
        tz=cfg.tz,
        start=start,
        end=end,
        exclude=cfg.event_exclude,
        title_strip=cfg.event_title_strip,
        pre=cfg.window_pre,
        post=cfg.window_post,
        late=cfg.late_grace,
        day_cutoff_hour=cfg.day_cutoff_hour,
        all_day_max_days=cfg.all_day_max_days or None,
        side=cfg.event_side,
        side_gap=cfg.side_event_gap,
    )


def empty_calendar(cfg: Config) -> EventCalendar:
    return EventCalendar([], cfg.window_pre, cfg.window_post, cfg.late_grace)


def nextcloud(cfg: Config) -> WebDavSink:
    return WebDavSink.from_config(cfg)


# --- jobs ------------------------------------------------------------------


def import_drop(cfg: Config, source: LocalSink | WebDavSink) -> Counter[str] | None:
    """Collect photographer uploads. Returns None if nothing was ready."""
    groups = pending_groups(source, cfg, datetime.now(cfg.tz))
    span = calendar_range(groups, cfg.tz)
    if span is None:
        return None
    first, last = span
    # EXIF of root files may be up to ~90 days older than the upload.
    calendar = build_calendar(cfg, first - timedelta(days=120), last + cfg.window_pre + timedelta(days=1))
    with pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            return ingest_drop(source, groups, Pipeline(cfg, db, calendar, source), cfg)
        finally:
            db.close()


def retry_uploads(cfg: Config, sink: Sink) -> int:
    """Retry pending uploads; returns the number still pending."""
    with pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            return Pipeline(cfg, db, empty_calendar(cfg), sink).retry_uploads()
        finally:
            db.close()


def purge_quarantine(cfg: Config) -> int:
    with pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            # Purging never uploads, so no Nextcloud credentials are needed.
            return Pipeline(cfg, db, empty_calendar(cfg), LocalSink(cfg.outbox_dir)).purge_quarantine(
                datetime.now(cfg.tz)
            )
        finally:
            db.close()


def settle_held(cfg: Config) -> int:
    """Move held items of closed events with too few items to quarantine."""
    with pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            return Pipeline(cfg, db, empty_calendar(cfg), LocalSink(cfg.outbox_dir)).settle_held(datetime.now(cfg.tz))
        finally:
            db.close()


def release(cfg: Config, sink: Sink, names: list[str]) -> dict[str, str]:
    """Admin rescue: put quarantined/held items (by staging file name) into the gallery."""
    results: dict[str, str] = {}
    with pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            pipeline = Pipeline(cfg, db, empty_calendar(cfg), sink)
            for name in names:
                row = db.find_by_staged_name(Path(name).name)
                results[name] = "not found" if row is None else pipeline.pin(row["id"])
        finally:
            db.close()
    return results


def promote_clusters(cfg: Config, sink: Sink) -> list[str]:
    """Move closed bursts of unmatched photos to the admin inbox."""
    with pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            pipeline = Pipeline(cfg, db, empty_calendar(cfg), sink)
            return pipeline.promote_clusters(closed_before=datetime.now(cfg.tz) - cfg.cluster_max_gap)
        finally:
            db.close()


def backfill_fingerprints(cfg: Config) -> int:
    """Perceptual hashes for images imported before they existed (duplicate detection)."""
    with pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            return Pipeline(cfg, db, empty_calendar(cfg), LocalSink(cfg.outbox_dir)).backfill_fingerprints()
        finally:
            db.close()
