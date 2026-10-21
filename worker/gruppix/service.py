"""Long-running service: built-in scheduler, checks, notifications, health endpoint.

Replaces DSM task-scheduler entries; receives WAHA webhooks (see webhook.py).
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import checks, jobs, review
from .config import Config, ConfigError
from .events import CalendarSourceError
from .ingest_drop import UNDELETED
from .notify import Notifier
from .sinks import WebDavSink
from .live import LiveIntake
from .webhook import WebhookInbox

log = logging.getLogger(__name__)

_TICK_SECONDS = 30
_MAX_FAILURES = 3            # consecutive failures before a job raises an alert
_STALL_AFTER = timedelta(minutes=5)
_MAX_WEBHOOK_BYTES = 10 * 1024 * 1024  # payloads carry a media URL, never the file

_JOB_TITLES = {
    "import-drop": "Fotografen-Uploads",
    "retry-uploads": "Upload in die Nextcloud",
    "promote-clusters": "Häufungen in den Eingang",
    "purge-quarantine": "Quarantäne löschen",
    "settle-held": "Zurückgehaltene Bilder auflösen",
}


@dataclass
class Job:
    name: str
    run: Callable[[], str | None]   # returns a short summary to log, or None
    next_run: datetime
    every: timedelta | None = None  # fixed interval …
    daily_hour: int | None = None   # … or once a day at this hour …
    weekday: int | None = None      # … optionally only on this weekday (0 = Monday)
    last_ok: datetime | None = None
    last_error: str | None = None
    failures: int = 0

    def reschedule(self, now: datetime) -> None:
        if self.every is not None:
            self.next_run = now + self.every
        else:
            self.next_run = next_daily(now, self.daily_hour or 0, self.weekday)

    def status(self) -> dict[str, Any]:
        return {
            "next_run": self.next_run.isoformat(timespec="seconds"),
            "last_ok": self.last_ok.isoformat(timespec="seconds") if self.last_ok else None,
            "last_error": self.last_error,
            "failures": self.failures,
        }


def next_daily(now: datetime, hour: int, weekday: int | None = None) -> datetime:
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    if weekday is not None:
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
    return candidate


@dataclass
class Service:
    cfg: Config
    stop: threading.Event = field(default_factory=threading.Event)
    jobs: list[Job] = field(default_factory=list)
    heartbeat: datetime | None = None
    inbox: WebhookInbox = None  # type: ignore[assignment]
    live: LiveIntake | None = None
    waha_misses: int = 0
    notifier: Notifier = None  # type: ignore[assignment]
    nextcloud: WebDavSink | None = None

    def __post_init__(self) -> None:
        if self.notifier is None:
            self.notifier = Notifier(self.cfg)
        if self.inbox is None:
            self.inbox = WebhookInbox(self.cfg)

    # --- setup -------------------------------------------------------------

    def build_jobs(self) -> None:
        cfg = self.cfg
        now = datetime.now(cfg.tz)
        try:
            self.nextcloud = jobs.nextcloud(cfg)
        except ConfigError as exc:
            log.warning("Nextcloud not configured (%s): upload-folder and upload jobs disabled", exc)
        nc = self.nextcloud

        if nc is not None:
            def drop() -> str | None:
                results = jobs.import_drop(cfg, nc)
                stuck = results[UNDELETED] if results else 0
                # A file Nextcloud refuses to delete (HTTP 423, locked) is already in the
                # gallery: a warning, not a worker fault; cleared once the delete works.
                self._sync_alerts("drop-cleanup", "Upload-Ordner", [
                    f"{stuck} Datei(en) sind schon in der Galerie, lassen sich aber nicht aus dem "
                    "Upload-Ordner löschen (Nextcloud meldet z. B. HTTP 423 = gesperrt). "
                    "Der Worker versucht es alle 15 min erneut."
                ] if stuck else [])
                return None if results is None else _summary(results)

            def retry() -> str | None:
                pending = jobs.retry_uploads(cfg, nc)
                return f"{pending} uploads still pending" if pending else None

            def clusters() -> str | None:
                folders = jobs.promote_clusters(cfg, nc)
                return f"clusters to inbox: {', '.join(folders)}" if folders else None

            self.jobs += [
                Job("import-drop", drop, now + timedelta(minutes=1), every=cfg.drop_interval),
                Job("retry-uploads", retry, now + timedelta(minutes=2), every=cfg.retry_interval),
                Job("promote-clusters", clusters, now + timedelta(minutes=3), daily_hour=cfg.maintenance_hour),
            ]

        def purge() -> str | None:
            purged = jobs.purge_quarantine(cfg)
            return f"{purged} quarantined files purged" if purged else None

        def fingerprints() -> str | None:
            done = jobs.backfill_fingerprints(cfg)
            return f"{done} older images fingerprinted" if done else None

        def settle() -> str | None:
            moved = jobs.settle_held(cfg)
            return f"{moved} items of small events moved to quarantine" if moved else None

        self.jobs += [
            Job("settle-held", settle, now + timedelta(minutes=2), every=cfg.drop_interval),
            Job("purge-quarantine", purge, now + timedelta(minutes=4), daily_hour=cfg.maintenance_hour),
            Job("backfill-fingerprints", fingerprints, now + timedelta(minutes=6), daily_hour=cfg.maintenance_hour),
            Job("heartbeat", self._heartbeat, now, every=timedelta(minutes=5)),
            Job("daily-check", self._daily_check, now + timedelta(minutes=5), daily_hour=cfg.maintenance_hour),
        ]
        if cfg.waha_api_key:
            self.jobs.append(Job("waha-check", self._waha_check, now + timedelta(minutes=1), every=timedelta(minutes=15)))
        if 0 <= cfg.report_weekday <= 6:
            self.jobs.append(
                Job(
                    "weekly-report", self._weekly_report,
                    next_daily(now, cfg.report_hour, cfg.report_weekday),
                    daily_hour=cfg.report_hour, weekday=cfg.report_weekday,
                )
            )

    def _start_live(self) -> None:
        cfg = self.cfg
        if not cfg.live_ingest:
            return
        if not cfg.group_chat_id or self.nextcloud is None:
            log.error("LIVE_INGEST=on needs GROUP_CHAT_ID and Nextcloud – live intake disabled")
            return
        if cfg.announce and not cfg.announce_url:
            log.warning("ANNOUNCE=on needs GALLERY_FOLDER_URL – no group messages")
        self.live = LiveIntake(cfg, self.nextcloud)
        self.inbox.live = self.live
        threading.Thread(target=self.live.run, args=(self.stop,), name="live", daemon=True).start()

    # --- checks and reports -------------------------------------------------

    def _heartbeat(self) -> None:
        # healthchecks.io is the dead man's switch only: as long as this loop
        # runs, it gets a success ping. Problems are alerted by the notifier.
        self.notifier.ping(healthy=True)
        if self.live is not None:
            self._sync_alerts("live", "WhatsApp-Übernahme", self.live.problems())
        self.notifier.publish_status()

    def _waha_check(self) -> str | None:
        try:
            problems = checks.waha_problems(self.cfg)
        except checks.WahaUnreachable as exc:
            # A busy NAS (e.g. during a long import) can delay WAHA for a while:
            # alert only after several consecutive misses.
            self.waha_misses += 1
            log.warning("waha-check: %s (%d in a row)", exc, self.waha_misses)
            if self.waha_misses >= _MAX_FAILURES:
                self.notifier.alert("waha", "WhatsApp-Verbindung", f"{exc} (seit {self.waha_misses} Prüfungen)")
            return None
        self.waha_misses = 0
        self._sync_alerts("waha", "WhatsApp-Verbindung", problems)
        return None

    def _daily_check(self) -> str | None:
        self._sync_alerts("calendar", "Kalender", checks.calendar_problems(self.cfg))
        self._sync_alerts("storage", "Speicherplatz", checks.storage_problems(self.cfg, self.nextcloud))
        return None

    def _weekly_report(self) -> str | None:
        try:
            pages = review.refresh(self.cfg)  # renew both review pages, the report names the counts
            counts = (pages.duplicates, pages.quarantine)
        except Exception:  # noqa: BLE001 - the report itself matters more
            log.exception("review pages could not be renewed")
            counts = (0, 0)
        text, data = checks.weekly_report(self.cfg, list(self.notifier.active.values()), counts)
        self.notifier.publish_report(data)
        sent = self.notifier.mail("[Gruppix] Wochenbericht", text)
        return f"weekly report ({data['uploaded']} uploaded{', mailed' if sent else ''})"

    def _sync_alerts(self, key: str, title: str, problems: list[str]) -> None:
        if problems:
            self.notifier.alert(key, title, "\n".join(problems))
        else:
            self.notifier.resolve(key, title)

    # --- main loop ---------------------------------------------------------

    def run(self) -> int:
        if threading.current_thread() is threading.main_thread():
            # docker stop sends SIGTERM: finish the running job, then exit
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop.set())
        self.build_jobs()
        self._start_live()
        self.notifier.announce()
        server = ThreadingHTTPServer(("0.0.0.0", self.cfg.http_port), _handler(self))
        threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
        log.info(
            "Service started: %s; notifications: %s; health on :%d/health",
            ", ".join(f"{j.name} ({_schedule(j)})" for j in self.jobs),
            self.notifier.describe(),
            self.cfg.http_port,
        )
        try:
            while not self.stop.is_set():
                self.heartbeat = datetime.now(self.cfg.tz)
                for job in self.jobs:
                    if self.stop.is_set():
                        break
                    if job.next_run <= datetime.now(self.cfg.tz):
                        self._execute(job)
                self.stop.wait(_TICK_SECONDS)
        finally:
            server.shutdown()
            log.info("Service stopped")
        return 0

    def _execute(self, job: Job) -> None:
        started = time.monotonic()
        try:
            summary = job.run()
        except jobs.Busy:
            log.info("%s skipped: another job holds the pipeline lock, retry in 5 min", job.name)
            job.next_run = datetime.now(self.cfg.tz) + timedelta(minutes=5)
            return
        except (CalendarSourceError, ConfigError) as exc:
            self._failed(job, str(exc))
        except Exception as exc:  # noqa: BLE001 - one failing job must not stop the service
            log.exception("%s failed", job.name)
            self._failed(job, f"{type(exc).__name__}: {exc}"[:300])
        else:
            job.last_ok = datetime.now(self.cfg.tz)
            job.last_error = None
            if job.failures >= _MAX_FAILURES:
                self.notifier.resolve(f"job:{job.name}", _JOB_TITLES.get(job.name, job.name))
            job.failures = 0
            if summary:
                log.info("%s: %s (%.0f s)", job.name, summary, time.monotonic() - started)
        job.reschedule(datetime.now(self.cfg.tz))

    def _failed(self, job: Job, message: str) -> None:
        job.failures += 1
        job.last_error = message
        log.error("%s failed (%d in a row): %s", job.name, job.failures, message)
        if job.failures >= _MAX_FAILURES:
            title = _JOB_TITLES.get(job.name, job.name)
            self.notifier.alert(f"job:{job.name}", title, f"{title} schlägt seit {job.failures} Versuchen fehl:\n{message}")

    # --- health ------------------------------------------------------------

    def health(self) -> tuple[int, dict[str, Any]]:
        """503 only for faults of the worker itself (failing jobs, stalled loop).

        External conditions (WAHA, storage, calendar) are reported as warnings:
        they are alerted by mail/MQTT and must not mark the container unhealthy.
        """
        now = datetime.now(self.cfg.tz)
        active = self.notifier.active
        problems = [msg for key, msg in active.items() if key.startswith("job:")]
        warnings = [msg for key, msg in active.items() if not key.startswith("job:")]
        if self.heartbeat is None or now - self.heartbeat > _STALL_AFTER:
            problems.append("scheduler loop stalled")
        body = {
            "status": "error" if problems else ("warning" if warnings else "ok"),
            "problems": problems,
            "warnings": warnings,
            "jobs": {j.name: j.status() for j in self.jobs},
            "webhooks": self.inbox.stats(),
            "live": self.live.stats() if self.live else None,
        }
        return (200 if not problems else 503), body


def _handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path.rstrip("/") == "/health":
                code, body = service.health()
                self._send(code, body)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path.rstrip("/") == "/webhook":
                length = int(self.headers.get("Content-Length") or 0)
                if length > _MAX_WEBHOOK_BYTES:
                    self._send(413, {"error": "too large"})
                    return
                body = self.rfile.read(length)
                code, answer = service.inbox.handle(body, dict(self.headers.items()))
                self._send(code, answer)
            else:
                self._send(404, {"error": "not found"})

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http: " + fmt, *args)

    return Handler


def _schedule(job: Job) -> str:
    if job.every is not None:
        return f"every {int(job.every.total_seconds() // 60)} min"
    days = "Mon Tue Wed Thu Fri Sat Sun".split()
    prefix = f"{days[job.weekday]} " if job.weekday is not None else "daily "
    return f"{prefix}{job.daily_hour:02d}:00"


def _summary(results: dict[str, int]) -> str:
    return ", ".join(f"{count} {state}" for state, count in sorted(results.items()))
