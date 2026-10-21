"""Admin portal: overview, quarantine and duplicates in the browser (LAN only).

Served by the worker on ADMIN_PORT (8081 inside the container) when
ADMIN_PASSWORD is set; docker-compose publishes it on ADMIN_BIND. HTTP Basic
auth (user "admin"); every change is a POST form that must come from the
portal itself (Origin/Referer check against Host, against CSRF), runs under
the pipeline lock and is logged. Pages are built from the database on each
request, thumbnails are rendered on demand and cached in memory.

Not meant for the internet: quarantine pictures must never leave the LAN.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import secrets
import shutil
import threading
import time
import zipfile
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

from . import jobs, media
from .config import Config
from .db import Database, State
from .ingest_export import ingest_export, read_export, select_period, source_id
from .pipeline import SIMILAR_MAX_BITS, Pipeline
from .review import SOURCE_LABEL, STYLE, german_day, thumbnail_jpeg
from .sinks import LocalSink

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

ADMIN_PORT = 8081            # inside the container; the host port is set in docker-compose
ADMIN_USER = "admin"
_MAX_FORM_BYTES = 1 << 20
_LOG_LINES = 300
_SMALL, _LARGE = 320, 480    # thumbnail edge: quarantine grid, duplicate pairs
_MAX_UPLOAD = 8 << 30        # 8 GiB: a long chat export with videos
_UPLOAD_DIR = "import-uploads"  # below DATA_DIR; emptied after each import (the ZIP holds all chat texts)
_IMPORT_STATES = {
    State.UPLOADED: "in die Galerie übernommen",
    State.READY: "warten auf den Upload",
    State.HELD: "zurückgehalten (Termin hat noch zu wenige Bilder)",
    State.QUARANTINED: "in der Quarantäne",
    State.DUPLICATE: "schon vorhanden (Duplikat)",
    "known": "schon früher importiert",
    State.REMOVED: "früher gelöscht, bleiben weg",
    State.SKIPPED: "Absender hat „Stopp“ geschickt",
    State.REJECTED: "abgelehnt (z. B. Video zu lang)",
    State.FAILED: "fehlgeschlagen",
}

_JOB_LABELS = {
    "import-drop": "Fotografen-Uploads abholen",
    "retry-uploads": "Uploads wiederholen",
    "promote-clusters": "Häufungen in den Eingang",
    "settle-held": "Zurückgehaltene auflösen",
    "purge-quarantine": "Quarantäne löschen",
    "backfill-fingerprints": "Fingerabdrücke nachtragen",
    "heartbeat": "Lebenszeichen",
    "daily-check": "Tägliche Prüfung",
    "waha-check": "WhatsApp-Verbindung",
    "weekly-report": "Wochenbericht",
}
_STATUS = {"ok": "Alles in Ordnung", "warning": "Warnung", "error": "Fehler"}


class LogBuffer(logging.Handler):
    """The last log lines in memory, for the overview page."""

    def __init__(self, size: int = _LOG_LINES) -> None:
        super().__init__(level=logging.INFO)
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%d.%m. %H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never fail
            self.handleError(record)


@dataclass
class DuplicateScan:
    """Background duplicate search started from the portal; the result stays until the next one."""

    running: bool = False
    step: int = 0               # 1 fingerprints, 2 compare, 3 thumbnails
    done: int = 0
    total: int = 0
    phase_started: float = 0.0  # time.monotonic() of the current step
    started: datetime | None = None
    finished: datetime | None = None
    pairs: list[tuple[str, str, int]] = field(default_factory=list)  # keep id, extra id, distance
    error: str | None = None

    STEPS = {1: "Fingerabdrücke älterer Bilder nachtragen", 2: "Bilder vergleichen", 3: "Vorschaubilder vorbereiten"}

    def begin(self, step: int, total: int) -> None:
        self.step, self.done, self.total, self.phase_started = step, 0, total, time.monotonic()

    def remaining(self) -> int | None:
        """Seconds left in the current step, from the rate so far."""
        if not self.done or self.total <= self.done:
            return None
        elapsed = time.monotonic() - self.phase_started
        return int(elapsed / self.done * (self.total - self.done))


@dataclass
class ImportJob:
    """A WhatsApp export uploaded in the portal and imported in the background."""

    running: bool = False
    name: str = ""
    step: str = ""
    done: int = 0
    total: int = 0
    phase_started: float = 0.0
    started: datetime | None = None
    finished: datetime | None = None
    period: str = ""
    results: Counter[str] = field(default_factory=Counter)
    clusters: list[str] = field(default_factory=list)
    waiting_for: str | None = None
    error: str | None = None

    def remaining(self) -> int | None:
        if not self.done or self.total <= self.done:
            return None
        elapsed = time.monotonic() - self.phase_started
        return int(elapsed / self.done * (self.total - self.done))


class ImportProblem(RuntimeError):
    """Shown to the admin as is (German text)."""


@dataclass(frozen=True)
class Task:
    """One portal action: release a quarantined item, or decide one duplicate pair."""

    kind: str          # release | drop | keep
    media_id: str      # the quarantined item, or the copy that would be removed
    keep_id: str = ""  # duplicates: the copy that stays
    target: str = ""   # release: "" automatic, "general", or "event:<event_key>"


_RESULTS = {
    "released": "{} in die Galerie übernommen",
    "placed": "{} in den gewählten Terminordner übernommen",
    "general": "{} in den Jahres-Sammelordner übernommen",
    "inbox": "{} ohne Termin nach Fotos-Eingang („… Markiert“) – bitte in der Nextcloud in den passenden "
             "Terminordner verschieben",
    "removed": "{} Kopien gelöscht",
    "kept": "{} Paare als verschieden gemerkt",
    "skipped": "{} übersprungen (inzwischen geändert)",
    "failed": "{} fehlgeschlagen – siehe Protokoll",
}


class ActionQueue:
    """Portal actions run one by one in a background thread, so a click returns at once.

    Each task takes the pipeline lock on its own; while another job holds it
    (a long photographer import) the queue waits and retries instead of
    failing. In memory only: after a restart the items are simply unchanged.
    """

    def __init__(self, apply: Callable[[Task], str], busy_label: Callable[[], str]) -> None:
        self._apply = apply
        self._busy_label = busy_label
        self._cond = threading.Condition()
        self._pending: deque[Task] = deque()
        self._thread: threading.Thread | None = None
        self.current: Task | None = None
        self.waiting_for: str | None = None
        self.done = 0
        self.total = 0
        self.results: Counter[str] = Counter()
        self.finished_at: float | None = None
        self.retry_delay = 5.0
        self.show_result_for = 600.0  # seconds the summary of the last batch stays on the pages

    def submit(self, tasks: list[Task]) -> int:
        """Queue new tasks; items already queued are ignored (double clicks). Returns the count added."""
        with self._cond:
            queued = {t.media_id for t in self._pending} | ({self.current.media_id} if self.current else set())
            new = []
            for task in tasks:
                if task.media_id not in queued:
                    queued.add(task.media_id)
                    new.append(task)
            if not new:
                return 0
            if not self._pending and self.current is None:  # a new batch
                self.done, self.total, self.results, self.finished_at = 0, 0, Counter(), None
            self._pending.extend(new)
            self.total += len(new)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="admin-actions", daemon=True)
                self._thread.start()
            self._cond.notify()
        return len(new)

    def queued_ids(self) -> set[str]:
        with self._cond:
            ids = {t.media_id for t in self._pending} | {t.keep_id for t in self._pending if t.keep_id}
            if self.current:
                ids |= {self.current.media_id, self.current.keep_id} - {""}
            return ids

    @property
    def active(self) -> bool:
        with self._cond:
            return bool(self._pending) or self.current is not None

    def status(self) -> dict[str, Any]:
        with self._cond:
            active = bool(self._pending) or self.current is not None
            recent = (self.finished_at is not None
                      and time.monotonic() - self.finished_at < self.show_result_for)
            return {
                "active": active,
                "done": self.done,
                "total": self.total,
                "waiting_for": self.waiting_for if active else None,
                "summary": summary(self.results) if (active or recent) and self.results else "",
            }

    def join(self, timeout: float = 30) -> bool:
        """Wait until the queue is empty (tests)."""
        end = time.monotonic() + timeout
        while self.active:
            if time.monotonic() > end:
                return False
            time.sleep(0.02)
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                self.current = self._pending.popleft()
            outcome = self._execute(self.current)
            with self._cond:
                self.results[outcome] += 1
                self.done += 1
                self.current = None
                if not self._pending:
                    self.finished_at = time.monotonic()
                    log.info("admin actions done: %s", dict(self.results))

    def _execute(self, task: Task) -> str:
        while True:
            try:
                outcome = self._apply(task)  # takes the pipeline lock, raises Busy
                self.waiting_for = None
                return outcome
            except jobs.Busy:
                self.waiting_for = self._busy_label()
                time.sleep(self.retry_delay)
            except Exception:  # noqa: BLE001 - one broken item must not stop the queue
                log.exception("admin action %s failed", task)
                self.waiting_for = None
                return "failed"


def summary(results: Counter[str]) -> str:
    return ", ".join(text.format(results[key]) for key, text in _RESULTS.items() if results[key])


@dataclass
class Response:
    code: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


class AdminPortal:
    def __init__(self, cfg: Config, service: Service, logs: LogBuffer | None = None) -> None:
        self.cfg = cfg
        self.service = service
        self.logs = logs
        self.fail_delay = 1.0  # seconds after a wrong password (slows down guessing)
        self.scan = DuplicateScan()
        self._scan_lock = threading.Lock()
        self.queue = ActionQueue(self.apply, self._busy_job)
        self.importing = ImportJob()
        self._import_lock = threading.Lock()
        # Leftovers of an import interrupted by a restart: chat texts must not linger.
        shutil.rmtree(cfg.data_dir / _UPLOAD_DIR, ignore_errors=True)

    # --- request entry -----------------------------------------------------------

    def dispatch(self, method: str, path: str, headers: dict[str, str], body: bytes = b"",
                 client: str = "?") -> Response:
        headers = {k.lower(): v for k, v in headers.items()}
        if not self._authorized(headers):
            if "authorization" in headers:
                log.warning("admin: wrong password from %s", client)
                time.sleep(self.fail_delay)
            return Response(401, "Anmeldung erforderlich".encode(),
                            "text/plain; charset=utf-8",
                            {"WWW-Authenticate": 'Basic realm="gruppix", charset="UTF-8"'})
        url = urlsplit(path)
        route = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        if method == "GET":
            pages: dict[str, Callable[[dict[str, list[str]]], Response]] = {
                "/": self.overview, "/quarantine": self.quarantine,
                "/duplicates": self.duplicates, "/thumb": self.thumb, "/queue": self.queue_status,
                "/import": self.import_page,
            }
            page = pages.get(route)
            return page(query) if page else self._page("Nicht gefunden", "<p>Diese Seite gibt es nicht.</p>", code=404)
        if method == "POST":
            if not _same_origin(headers):
                log.warning("admin: rejected POST %s without matching Origin from %s", route, client)
                return Response(403, "Anfrage nicht von dieser Seite".encode(), "text/plain; charset=utf-8")
            form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            actions = {"/quarantine": self.release, "/duplicates": self.resolve_duplicates,
                       "/duplicates/scan": self.start_scan}
            action = actions.get(route)
            if action is None:
                return Response(404, b"not found", "text/plain")
            return action(form, client)
        return Response(405, b"method not allowed", "text/plain", {"Allow": "GET, POST"})

    def _authorized(self, headers: dict[str, str]) -> bool:
        scheme, _, value = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic" or not self.cfg.admin_password:
            return False
        try:
            user, _, password = base64.b64decode(value, validate=True).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        ok_user = hmac.compare_digest(user.encode(), ADMIN_USER.encode())
        ok_password = hmac.compare_digest(password.encode(), self.cfg.admin_password.encode())
        return ok_user and ok_password

    # --- pages ---------------------------------------------------------------------

    def overview(self, query: dict[str, list[str]]) -> Response:
        code, health = self.service.health()
        db = Database(self.cfg.db_path)
        try:
            states = db.state_counts()
        finally:
            db.close()
        status = health["status"]
        notes = "".join(f'<li class="err">{_e(p)}</li>' for p in health["problems"])
        notes += "".join(f'<li class="warn">{_e(w)}</li>' for w in health["warnings"])
        running = health.get("running")
        if running:
            since = datetime.fromisoformat(running["since"])
            notes += (f'<li>Läuft gerade: {_e(_JOB_LABELS.get(running["job"], running["job"]))} '
                      f'seit {since:%H:%M} Uhr</li>')
        tiles = [
            ("In der Galerie", states[State.UPLOADED] + states[State.READY], None),
            ("Quarantäne", states[State.QUARANTINED], "/quarantine"),
            ("Zurückgehalten", states[State.HELD], "/quarantine"),
            ("Fehlgeschlagen", states[State.FAILED], None),
        ]
        if self.scan.finished and not self.scan.running:
            tiles.append(("Duplikate (letzte Prüfung)", len(self._open_pairs(self.scan.pairs)), "/duplicates"))
        live = health.get("live")
        if live:
            tiles.append(("Live-Warteschlange", live["queued"], None))
        tile_html = "".join(
            (f'<a class="stat card" href="{href}">' if href else '<div class="stat card">')
            + f'<span class="num">{n}</span><span class="muted">{_e(label)}</span>'
            + ("</a>" if href else "</div>")
            for label, n, href in tiles
        )
        jobs_rows = "".join(
            f"<tr><td>{_e(_JOB_LABELS.get(name, name))}</td><td>{_when(j['last_ok'])}</td>"
            f"<td>{_when(j['next_run'])}</td><td class='err'>{_e(j['last_error'] or '')}</td></tr>"
            for name, j in health["jobs"].items()
        )
        report_path = self.cfg.data_dir / "weekly-report.txt"
        report = ""
        if report_path.exists():
            stamp = datetime.fromtimestamp(report_path.stat().st_mtime, self.cfg.tz)
            report = (f'<h2>Letzter Wochenbericht <span class="muted">({stamp:%d.%m.%Y %H:%M})</span></h2>'
                      f'<pre class="box">{_e(report_path.read_text(encoding="utf-8"))}</pre>')
        log_lines = list(self.logs.lines)[-120:] if self.logs else []
        log_html = (f'<h2>Protokoll <span class="muted">(neueste oben)</span></h2>'
                    f'<pre class="box log">{_e(chr(10).join(reversed(log_lines)))}</pre>') if log_lines else ""
        body = (
            f'<p class="status {status}">{_STATUS.get(status, status)}</p>'
            + (f"<ul class='notes'>{notes}</ul>" if notes else "")
            + f'<div class="stats">{tile_html}</div>'
            + '<h2>Aufgaben</h2><table class="jobs"><thead><tr><th>Aufgabe</th><th>Zuletzt erfolgreich</th>'
              f'<th>Nächster Lauf</th><th>Letzter Fehler</th></tr></thead><tbody>{jobs_rows}</tbody></table>'
            + report + log_html
        )
        return self._page("Übersicht", body, code=200)

    def quarantine(self, query: dict[str, list[str]]) -> Response:
        db = Database(self.cfg.db_path)
        try:
            rows = [r for r in self._pipeline(db).quarantined() if r["staged_path"]]
        finally:
            db.close()
        flash = _flash(query)
        queued = self.queue.queued_ids()
        if not rows:
            return self._page("Quarantäne", flash + "<p>Die Quarantäne ist leer.</p>")
        days: dict[str, list[str]] = {}
        for row in rows:
            sent = datetime.fromtimestamp(row["msg_ts"], self.cfg.tz)
            if row["state"] == State.HELD or row["pending_dir"]:
                note = "→ " + (row["pending_dir"] or "").rsplit("/", 1)[-1]
            else:
                purge = datetime.fromtimestamp(row["updated_at"], self.cfg.tz) + timedelta(days=self.cfg.quarantine_days)
                note = f"ohne Termin → Fotos-Eingang · wird gelöscht am {purge:%d.%m.}"
            source = SOURCE_LABEL.get(row["source"], row["source"])
            days.setdefault(german_day(f"{sent:%A, %d.%m.%Y}"), []).append(
                f'<label class="tile card{" queued" if row["id"] in queued else ""}">{self._img(row["id"], row["kind"], "s")}'
                f'<span class="cap"><b>{sent:%H:%M}</b> · {_e(source)} · Absender {_e(row["sender_hash"][:6])}'
                f'<br>{_e(note)}</span>'
                + ('<span class="tick muted">wird übernommen …</span></label>' if row["id"] in queued else
                   f'<span class="tick"><input type="checkbox" name="id" value="{_e(row["id"])}"> In die Galerie</span>'
                   f'</label>')
            )
        groups = "".join(
            f'<section class="day"><h2>{_e(day)} <button type="button" class="link all">alle</button></h2>'
            f'<div class="grid">{"".join(tiles)}</div></section>'
            for day, tiles in days.items()
        )
        lead = (
            f'<p class="lead">{len(rows)} {"Eintrag" if len(rows) == 1 else "Einträge"} ohne passenden Termin oder mit zu wenigen Bildern '
            "für ihren Termin. Ankreuzen, unten das <b>Ziel</b> wählen, übernehmen. „Automatisch“ wirkt wie 📸: "
            "Bilder mit Termin (→ Ordnername) in ihren Terminordner, Bilder <b>ohne Termin nach Fotos-Eingang</b> "
            "(dort von Hand einsortieren). Besser gleich einen <b>Terminordner</b> wählen oder für schöne Einzelbilder "
            f"den Jahres-Sammelordner „{_e(self.cfg.general_folder)}“. Alles andere wird nach "
            f"{self.cfg.quarantine_days} Tagen gelöscht. Gleicher Absender-Code = gleiche Person.</p>"
        )
        events = "".join(
            f'<option value="event:{_e(key)}">{datetime.fromtimestamp(row["start_ts"], self.cfg.tz):%d.%m.%Y} · '
            f'{_e(row["title"])}</option>'
            for key, row in self._event_choices().items()
        )
        target = (
            '<label class="target"><span class="muted">Ziel</span> <select name="target" form="f">'
            '<option value="auto" selected>Automatisch – Terminordner, sonst Fotos-Eingang</option>'
            f'<option value="general">Jahres-Sammelordner „{_e(self.cfg.general_folder)}“</option>'
            + (f'<optgroup label="Terminordner">{events}</optgroup>' if events else "")
            + "</select></label>"
        )
        bar = ('<div class="bar"><div><span id="sum" class="grow muted">Nichts ausgewählt</span>'
               + target + '<button type="submit" form="f" id="go">In die Galerie übernehmen</button></div></div>')
        body = flash + lead + f'<form id="f" method="post" action="/quarantine">{groups}</form>'
        return self._page("Quarantäne", body, bar=bar, script=_QUARANTINE_JS)

    def duplicates(self, query: dict[str, list[str]]) -> Response:
        scan = self.scan
        flash = _flash(query)
        start = ('<form method="post" action="/duplicates/scan" class="inline">'
                 '<button type="submit">{}</button></form>')
        if scan.running:
            label = DuplicateScan.STEPS.get(scan.step, "Vorbereiten")
            count = f" · {scan.done} von {scan.total}" if scan.total > 1 else ""
            left = scan.remaining()
            eta = "" if left is None else (" · noch unter einer Minute" if left < 60 else f" · noch etwa {left // 60 + 1} min")
            body = (f'<div class="card scan"><p><span class="spinner"></span> <b>Prüfung läuft</b> – '
                    f'Schritt {scan.step} von 3: {_e(label)}{count}{eta}</p>'
                    f'<progress max="{max(scan.total, 1)}" value="{scan.done}"></progress>'
                    '<p class="muted">Die Seite aktualisiert sich von selbst. Du kannst sie auch schließen; '
                    "die Prüfung läuft weiter.</p></div>")
            return self._page("Duplikate", flash + body, refresh=3)
        if scan.started is None:
            body = ('<div class="card scan"><p>Noch nicht geprüft. Die Prüfung vergleicht alle Bilder der Galerie: '
                    "WhatsApp-Bilder untereinander und Fotografen-Uploads mit den WhatsApp-Bildern desselben "
                    "Termins. Gelöscht wird erst, wenn du es bestätigst.</p>"
                    + start.format("Prüfung starten") + "</div>")
            return self._page("Duplikate", flash + body)
        took = int((scan.finished - scan.started).total_seconds()) if scan.finished else 0
        status = (f'<div class="card scan"><p>Geprüft am {scan.started:%d.%m. um %H:%M} Uhr '
                  f'<span class="muted">(Dauer {_duration(took)})</span></p>'
                  + (f'<p class="err">Fehler: {_e(scan.error)}</p>' if scan.error else "")
                  + start.format("Erneut prüfen") + "</div>")
        queued = self.queue.queued_ids()
        pairs = [p for p in self._open_pairs(scan.pairs) if p[0]["id"] not in queued and p[1]["id"] not in queued]
        if not pairs:
            return self._page("Duplikate", flash + status + "<p>Keine doppelten Bilder (mehr) offen.</p>")
        cards = []
        for n, (keep, extra, distance) in enumerate(pairs, start=1):
            folder = (keep["remote_path"] or "").rsplit("/", 1)[0]
            cards.append(
                f'<section class="card pairbox"><header><span class="n">{n}</span>'
                f'<span class="grow">{_e(folder)}</span>'
                f'<span class="muted" title="Unterschied im Bildvergleich, 0 = gleich">{distance} von 64 Bit</span></header>'
                f'<div class="pair">{self._figure(keep, "big", "Größer")}{self._figure(extra, "small", "Kleiner")}</div>'
                f'<input type="hidden" name="k{n}" value="{_e(keep["id"])}">'
                f'<input type="hidden" name="e{n}" value="{_e(extra["id"])}">'
                f'<fieldset class="choice">'
                f'<label><input type="radio" name="a{n}" value="drop" checked> Rechts (kleiner) löschen</label>'
                f'<label><input type="radio" name="a{n}" value="swap"> Links (größer) löschen</label>'
                f'<label><input type="radio" name="a{n}" value="keep"> Kein Duplikat – beide behalten</label>'
                f'<label><input type="radio" name="a{n}" value="skip"> Später entscheiden</label>'
                f'</fieldset></section>'
            )
        lead = (
            f'<p class="lead">{len(pairs)} Paare, links jeweils die größere Datei. Vorgeschlagen ist, die kleinere '
            "zu löschen; soll absichtlich die kleinere bleiben (z. B. besserer Ausschnitt), „Links löschen“ wählen. "
            "Gelöscht wird nur die Kopie in der Nextcloud – <b>beide Originale bleiben im Archiv</b>. "
            "„Beide behalten“ merkt sich das Paar, es erscheint nicht wieder.</p>"
        )
        bar = ('<div class="bar"><div><span id="sum" class="grow muted"></span>'
               '<button type="submit" form="f">Auswahl ausführen</button></div></div>')
        body = flash + status + lead + f'<form id="f" method="post" action="/duplicates">{"".join(cards)}</form>'
        return self._page("Duplikate", body, bar=bar, script=_DUPLICATES_JS)

    def _open_pairs(self, found: list[tuple[str, str, int]]) -> list[tuple]:
        """Pairs of the last scan that still need a decision (both copies in the gallery)."""
        db = Database(self.cfg.db_path)
        try:
            distinct = db.distinct_pairs()
            pairs = []
            for keep_id, extra_id, distance in found:
                try:
                    keep, extra = db.get(keep_id), db.get(extra_id)
                except KeyError:
                    continue
                if (keep["state"] in State.PUBLISHED and extra["state"] in State.PUBLISHED
                        and tuple(sorted((keep_id, extra_id))) not in distinct):
                    pairs.append((keep, extra, distance))
            return pairs
        finally:
            db.close()

    def thumb(self, query: dict[str, list[str]]) -> Response:
        media_id = (query.get("id") or [""])[0]
        edge = _LARGE if (query.get("size") or ["s"])[0] == "l" else _SMALL
        db = Database(self.cfg.db_path)
        try:
            row = db.get(media_id)
        except KeyError:
            return Response(404, b"", "text/plain")
        finally:
            db.close()
        path = next((p for p in (row["staged_path"], row["archive_path"]) if p and Path(p).exists()), None)
        if path is None:
            return Response(404, b"", "text/plain")
        data = _cached_thumb(path, Path(path).stat().st_mtime_ns, row["kind"], edge)
        if data is None:
            return Response(404, b"", "text/plain")
        return Response(200, data, "image/jpeg", {"Cache-Control": "private, max-age=86400"})

    # --- actions -------------------------------------------------------------------

    def start_scan(self, form: dict[str, list[str]], client: str) -> Response:
        with self._scan_lock:
            if self.scan.running:
                return _redirect("/duplicates")
            scan = DuplicateScan(running=True, started=datetime.now(self.cfg.tz))
            self.scan = scan
        log.info("admin (%s): duplicate scan started", client)
        threading.Thread(target=self.run_scan, args=(scan,), name="duplicate-scan", daemon=True).start()
        return _redirect("/duplicates")

    def run_scan(self, scan: DuplicateScan) -> None:
        """Fingerprint what is missing, compare, pre-render the thumbnails of the pairs.

        Runs without the pipeline lock: it only adds fingerprints and reads,
        so a long photographer import does not block it."""
        try:
            db = Database(self.cfg.db_path)
            try:
                pipeline = self._pipeline(db)
                scan.begin(1, len(db.missing_fingerprint()))

                def progress(done: int, total: int) -> None:
                    scan.done, scan.total = done, total

                pipeline.backfill_fingerprints(progress)
                scan.begin(2, 1)
                found = pipeline.similar_pairs()
                scan.done = 1
                scan.begin(3, 2 * len(found))
                for keep, extra, _ in found:
                    for row in (keep, extra):
                        path = row["archive_path"] or row["staged_path"]
                        if path and Path(path).exists():
                            _cached_thumb(path, Path(path).stat().st_mtime_ns, row["kind"], _LARGE)
                        scan.done += 1
                scan.pairs = [(k["id"], e["id"], d) for k, e, d in found]
            finally:
                db.close()
            log.info("duplicate scan: %d pairs", len(scan.pairs))
        except Exception as exc:  # noqa: BLE001 - shown on the page
            log.exception("duplicate scan failed")
            scan.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            scan.finished = datetime.now(self.cfg.tz)
            scan.running = False

    def release(self, form: dict[str, list[str]], client: str) -> Response:
        ids = list(dict.fromkeys(form.get("id", [])))
        if ids and self.service.nextcloud is None:
            return _redirect("/quarantine", nonc=1)
        target = (form.get("target") or ["auto"])[0]
        if target == "auto":
            target = ""
        elif target != "general" and target.removeprefix("event:") not in self._event_choices():
            return _redirect("/quarantine")  # unknown or forged target: change nothing
        added = self.queue.submit([Task("release", media_id, target=target) for media_id in ids])
        log.info("admin (%s): %d items queued for the gallery (%s)", client, added, target or "automatic")
        return _redirect("/quarantine")

    def _event_choices(self) -> dict[str, Any]:
        """Event folders offered as target, newest first: key -> events row."""
        db = Database(self.cfg.db_path)
        try:
            return {row["event_key"]: row for row in db.recent_events()}
        finally:
            db.close()

    def resolve_duplicates(self, form: dict[str, list[str]], client: str) -> Response:
        tasks = []
        for key, values in form.items():
            if key.startswith("a") and key[1:].isdigit() and values[0] in ("drop", "swap", "keep"):
                n = key[1:]
                keep, extra = (form.get(f"k{n}") or [""])[0], (form.get(f"e{n}") or [""])[0]
                if keep and extra:
                    if values[0] == "swap":  # keep the smaller copy, remove the larger one
                        tasks.append(Task("drop", keep, extra))
                    else:
                        tasks.append(Task(values[0], extra, keep))
        if tasks and self.service.nextcloud is None:
            return _redirect("/duplicates", nonc=1)
        added = self.queue.submit(tasks)
        log.info("admin (%s): %d duplicate decisions queued", client, added)
        return _redirect("/duplicates")

    def queue_status(self, query: dict[str, list[str]]) -> Response:
        return Response(200, json.dumps(self.queue.status(), ensure_ascii=False).encode(),
                        "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def apply(self, task: Task) -> str:
        """Run one queued action under the pipeline lock (raises jobs.Busy while another job runs)."""
        with jobs.pipeline_lock(self.cfg):
            db = Database(self.cfg.db_path)
            try:
                pipeline = self._pipeline(db, gallery=True)
                if task.kind == "release":
                    try:
                        row = db.get(task.media_id)
                    except KeyError:
                        return "skipped"
                    if row["state"] not in (State.QUARANTINED, State.HELD):
                        return "skipped"
                    if task.target == "general":
                        state, outcome = pipeline.place(task.media_id, pipeline.general_dir(task.media_id)), "general"
                    elif task.target.startswith("event:"):
                        event = db.event_info(task.target.removeprefix("event:"))
                        if event is None:
                            return "skipped"
                        rel_dir = f"{datetime.fromtimestamp(event['start_ts'], self.cfg.tz):%Y}/{event['folder']}"
                        state, outcome = pipeline.place(task.media_id, rel_dir, event["event_key"]), "placed"
                    else:
                        state = pipeline.pin(task.media_id)
                        outcome = "inbox" if db.get(task.media_id)["target"] == "inbox" else "released"
                    return outcome if state in (*State.PUBLISHED, State.REJECTED) else "failed"
                # Duplicates: act only if both copies are still in the gallery and still look
                # alike, so a stale or forged form changes nothing.
                try:
                    keep, extra = db.get(task.keep_id), db.get(task.media_id)
                except KeyError:
                    return "skipped"
                if (keep["state"] not in State.PUBLISHED or extra["state"] not in State.PUBLISHED
                        or not keep["phash"] or not extra["phash"]
                        or media.hash_distance(keep["phash"], extra["phash"]) > SIMILAR_MAX_BITS
                        or tuple(sorted((keep["id"], extra["id"]))) in db.distinct_pairs()):
                    return "skipped"
                if task.kind == "keep":
                    db.add_distinct_pair(keep["id"], extra["id"])
                    return "kept"
                return "removed" if pipeline.drop_duplicate(extra["id"], keep["id"]) == State.DUPLICATE else "skipped"
            finally:
                db.close()

    # --- WhatsApp export import ------------------------------------------------------

    def import_page(self, query: dict[str, list[str]]) -> Response:
        job = self.importing
        if job.running:
            count = f" · {job.done} von {job.total}" if job.total else ""
            left = job.remaining()
            eta = "" if left is None else (" · noch unter einer Minute" if left < 60 else f" · noch etwa {left // 60 + 1} min")
            wait = f" · wartet auf „{_e(job.waiting_for)}“" if job.waiting_for else ""
            body = (f'<div class="card scan"><p><span class="spinner"></span> <b>Import läuft</b> – '
                    f'{_e(job.name)}{" (" + _e(job.period) + ")" if job.period else ""}</p>'
                    f'<p>{_e(job.step)}{count}{eta}{wait}</p>'
                    f'<progress max="{max(job.total, 1)}" value="{job.done}"></progress>'
                    '<p class="muted">Die Seite aktualisiert sich von selbst. Du kannst sie schließen; '
                    "der Import läuft weiter.</p></div>")
            return self._page("Import", body, refresh=3)
        last = ""
        if job.finished:
            if job.error:
                last = f'<p class="flash warn">Import von {_e(job.name)} abgebrochen: {_e(job.error)}</p>'
            else:
                lines = "".join(f"<li>{n} {_e(_IMPORT_STATES.get(state, state))}</li>"
                                for state, n in sorted(job.results.items(), key=lambda kv: -kv[1]))
                clusters = ("<p>Häufungen ohne Termin, jetzt in Fotos-Eingang: "
                            + ", ".join(_e(c) for c in job.clusters) + "</p>") if job.clusters else ""
                took = int((job.finished - job.started).total_seconds()) if job.started else 0
                last = (f'<div class="flash"><p><b>Import fertig</b>: {_e(job.name)} ({_e(job.period)}, '
                        f"Dauer {_duration(took)})</p><ul>{lines}</ul>{clusters}"
                        '<p>Bilder in der Quarantäne kannst du <a href="/quarantine">hier</a> übernehmen.</p></div>')
        db = Database(self.cfg.db_path)
        try:
            latest = db.latest_message_ts(("export", "waha"))
        finally:
            db.close()
        since = f"{datetime.fromtimestamp(latest, self.cfg.tz):%Y-%m-%d}" if latest else ""
        hint = (f" Vorgeschlagen ist der Tag des neuesten schon übernommenen Bildes ({datetime.fromtimestamp(latest, self.cfg.tz):%d.%m.%Y}); "
                "was schon da ist, wird übersprungen.") if latest else ""
        body = (
            last
            + '<p class="lead">WhatsApp-Export der Gruppe hochladen, z. B. um eine Lücke nach einem Ausfall des '
            "Bots zu schließen: in WhatsApp Gruppe → ⋮ → <i>Mehr</i> → <i>Chat exportieren</i> → "
            "<b>Medien einbeziehen</b>, die ZIP-Datei hier auswählen. Bilder und Videos laufen durch dieselben "
            "Regeln wie live. Die ZIP-Datei enthält auch alle Texte des Chats; sie wird nach dem Import sofort "
            f"gelöscht.{hint}</p>"
            '<form id="up" class="card importform">'
            '<label>Export-Datei (.zip)<input type="file" id="zip" accept=".zip,application/zip" required></label>'
            f'<label>Ab Datum<input type="date" id="since" value="{since}"></label>'
            '<label>Bis Datum (optional)<input type="date" id="until"></label>'
            '<label>Datumsformat im Chat<select id="order"><option value="dmy" selected>TT.MM.JJ (deutsch)</option>'
            '<option value="mdy">MM/TT/JJ (englisch, USA)</option></select></label>'
            '<div class="row"><button type="submit" id="go">Hochladen und importieren</button>'
            '<progress id="bar" max="100" value="0" hidden></progress><span id="msg" class="muted"></span></div>'
            "</form>"
        )
        return self._page("Import", body, script=_IMPORT_JS)

    def receive_upload(self, path: str, headers: dict[str, str], stream: IO[bytes], length: int,
                       client: str = "?") -> Response:
        """POST /import/upload: the ZIP as raw body, streamed to disk, then imported in the background."""
        headers = {k.lower(): v for k, v in headers.items()}
        if not self._authorized(headers):
            return Response(401, b"", "text/plain", {"WWW-Authenticate": 'Basic realm="gruppix", charset="UTF-8"'})
        if not _same_origin(headers):
            log.warning("admin: rejected upload without matching Origin from %s", client)
            return _json(403, error="Anfrage nicht von dieser Seite")
        query = parse_qs(urlsplit(path).query)
        name = Path((query.get("name") or [""])[0]).name
        try:
            since = date.fromisoformat(query["since"][0]) if query.get("since", [""])[0] else None
            until = date.fromisoformat(query["until"][0]) if query.get("until", [""])[0] else None
        except ValueError:
            return _json(400, error="Datum ungültig")
        order = (query.get("order") or ["dmy"])[0]
        if not name.lower().endswith(".zip") or order not in ("dmy", "mdy"):
            return _json(400, error="Bitte die ZIP-Datei des WhatsApp-Exports wählen")
        if since and until and since > until:
            return _json(400, error="„Ab“ liegt nach „Bis“")
        if self.service.nextcloud is None:
            return _json(400, error="Nextcloud ist nicht eingerichtet")
        if length <= 0 or length > _MAX_UPLOAD:
            return _json(413, error=f"Datei zu groß (höchstens {_MAX_UPLOAD >> 30} GB)")
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.cfg.data_dir).free < length + (1 << 30):
            return _json(507, error="Zu wenig Speicherplatz auf dem Server")
        with self._import_lock:
            if self.importing.running:
                return _json(409, error="Es läuft bereits ein Import")
            job = ImportJob(running=True, name=name, step="Hochladen", started=datetime.now(self.cfg.tz))
            self.importing = job
        folder = self.cfg.data_dir / _UPLOAD_DIR / datetime.now(self.cfg.tz).strftime("%Y%m%d-%H%M%S")
        target = folder / name  # keeps the export's name: it is part of the stored ids (re-imports match)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            remaining = length
            with target.open("wb") as out:
                while remaining:
                    chunk = stream.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ImportProblem("Upload abgebrochen")
                    out.write(chunk)
                    remaining -= len(chunk)
            if not zipfile.is_zipfile(target):
                raise ImportProblem("Keine gültige ZIP-Datei")
        except (ImportProblem, OSError) as exc:
            shutil.rmtree(folder, ignore_errors=True)
            job.error, job.finished, job.running = str(exc), datetime.now(self.cfg.tz), False
            return _json(400, error=str(exc))
        log.info("admin (%s): export %s uploaded (%d MB), importing", client, name, length >> 20)
        threading.Thread(target=self.run_import, args=(job, target, since, until, order),
                         name="export-import", daemon=True).start()
        return _json(202, ok=True)

    def run_import(self, job: ImportJob, zip_path: Path, since: date | None, until: date | None,
                   order: str = "dmy") -> None:
        cfg = self.cfg
        try:
            job.step = "Chat lesen"
            content = read_export(zip_path, cfg.tz, order)
            content = select_period(content, since, until)
            if not content.messages:
                raise ImportProblem("Keine Fotos oder Videos im gewählten Zeitraum gefunden")
            first = min(m.sent_at for m in content.messages)
            last = max(m.sent_at for m in content.messages)
            job.period, job.total = f"{first:%d.%m.%Y} – {last:%d.%m.%Y}", len(content.messages)
            job.step = "Kalender laden"
            calendar = jobs.build_calendar(cfg, first - cfg.late_grace - timedelta(days=7),
                                           last + cfg.window_pre + timedelta(days=1))
            job.step = "Bilder übernehmen"
            while True:
                try:
                    with jobs.pipeline_lock(cfg):
                        job.waiting_for, job.done, job.phase_started = None, 0, time.monotonic()
                        db = Database(cfg.db_path)
                        try:
                            pipeline = Pipeline(cfg, db, calendar, self.service.nextcloud)

                            def progress(done: int, total: int) -> None:
                                job.done = done

                            ids = [f"export:{source_id(content, m)}" for m in content.messages]
                            known = {i for i in ids if db.exists(i)}
                            ingest_export(zip_path, content, pipeline, progress)
                            job.step = "Termine abschließen"
                            pipeline.settle_held(datetime.now(cfg.tz))
                            job.clusters = pipeline.promote_clusters()
                            # Final state of each new item (a held item may have been published by a later one)
                            results: Counter[str] = Counter({"known": len(known)} if known else {})
                            for media_id in ids:
                                if media_id not in known and db.exists(media_id):
                                    results[db.get(media_id)["state"]] += 1
                            job.results = results
                        finally:
                            db.close()
                    break
                except jobs.Busy:
                    job.waiting_for = self._busy_job()
                    time.sleep(10)
            log.info("portal import %s: %s", job.name, dict(job.results))
        except (ImportProblem, ValueError) as exc:
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - shown on the page
            log.exception("portal import failed")
            job.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            shutil.rmtree(zip_path.parent, ignore_errors=True)
            job.finished = datetime.now(cfg.tz)
            job.running = False

    # --- helpers -------------------------------------------------------------------

    def _pipeline(self, db: Database, gallery: bool = False) -> Pipeline:
        sink = self.service.nextcloud if gallery else None
        if gallery and sink is None:
            raise RuntimeError("Nextcloud is not configured")
        return Pipeline(self.cfg, db, jobs.empty_calendar(self.cfg), sink or LocalSink(self.cfg.outbox_dir))

    def _queue_banner(self) -> str:
        """Progress of queued actions; the page script keeps it up to date."""
        st = self.queue.status()
        if st["active"]:
            wait = f" · wartet auf „{_e(st['waiting_for'])}“" if st["waiting_for"] else ""
            return (f'<div id="queue" class="card queue" data-active="1"><span class="spinner"></span> '
                    f'<span id="queue-text">Aufträge werden ausgeführt: {st["done"]} von {st["total"]}{wait}</span></div>')
        if st["summary"]:
            return f'<div id="queue" class="flash" data-active="0"><span id="queue-text">Erledigt: {_e(st["summary"])}</span></div>'
        return '<div id="queue" data-active="0" hidden><span id="queue-text"></span></div>'

    def _busy_job(self) -> str:
        running = self.service.running
        return _JOB_LABELS.get(running[0], running[0]) if running else "ein Import"

    def _img(self, media_id: str, kind: str, size: str) -> str:
        badge = '<span class="badge">Video</span>' if kind == "video" else ""
        return (f'<span class="frame"><img src="/thumb?{urlencode({"id": media_id, "size": size})}" '
                f'alt="" loading="lazy">{badge}</span>')

    def _figure(self, row, role: str, label: str) -> str:
        """One copy of a pair; CSS shows "bleibt"/"wird gelöscht" from the chosen radio."""
        sent = datetime.fromtimestamp(row["msg_ts"], self.cfg.tz).strftime("%d.%m.%Y %H:%M")
        name = (row["remote_path"] or "").rsplit("/", 1)[-1]
        source = SOURCE_LABEL.get(row["source"], row["source"])
        size = f'{row["pixels"] / 1e6:.1f} MP · ' if row["pixels"] else ""
        return (f'<figure class="{role}">{self._img(row["id"], row["kind"], "l")}<figcaption>'
                f'<b>{label}</b> <span class="fate stays">bleibt</span><span class="fate goes">wird gelöscht</span>'
                f' · {size}{_e(source)} · gesendet {sent}<br><code>{_e(name)}</code></figcaption></figure>')

    def _page(self, title: str, body: str, *, code: int = 200, bar: str = "", script: str = "",
              refresh: int | None = None) -> Response:
        nonce = secrets.token_urlsafe(16)
        nav = "".join(
            f'<a href="{href}"{" class=on" if label == title else ""}>{label}</a>'
            for href, label in (("/", "Übersicht"), ("/quarantine", "Quarantäne"), ("/duplicates", "Duplikate"),
                                ("/import", "Import"))
        )
        script = _COMMON_JS + script
        page = (
            _PAGE.replace("{{style}}", STYLE + _EXTRA_STYLE)
            .replace("{{queue}}", self._queue_banner())
            .replace("{{title}}", _e(title))
            .replace("{{club}}", _e(self.cfg.club_name))
            .replace("{{nav}}", nav)
            .replace("{{bar}}", bar)
            .replace("{{script}}", f'<script nonce="{nonce}">{script}</script>')
            .replace("{{refresh}}", f'<meta http-equiv="refresh" content="{refresh}">' if refresh else "")
            .replace("{{nonce}}", nonce)
            .replace("{{body}}", body)
        )
        return Response(code, page.encode(), headers={
            "Content-Security-Policy": (
                f"default-src 'none'; img-src 'self'; connect-src 'self'; style-src 'nonce-{nonce}'; "
                f"script-src 'nonce-{nonce}'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
            ),
            "Cache-Control": "no-store",
        })


@lru_cache(maxsize=600)  # ~20 KB each
def _cached_thumb(path: str, mtime_ns: int, kind: str, edge: int) -> bytes | None:
    return thumbnail_jpeg(path, kind, edge)[0]


def _same_origin(headers: dict[str, str]) -> bool:
    """POST only from the portal's own pages (CSRF protection)."""
    source = headers.get("origin") or headers.get("referer")
    if not source or source == "null":
        return False
    netloc = urlsplit(source).netloc
    hosts = {headers.get("host"), headers.get("x-forwarded-host")}
    return bool(netloc) and netloc in hosts


def _json(code: int, **body: Any) -> Response:
    return Response(code, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")


def _redirect(path: str, **params: Any) -> Response:
    query = urlencode({k: v for k, v in params.items() if v})
    return Response(303, b"", "text/plain", {"Location": path + (f"?{query}" if query else "")})


def _flash(query: dict[str, list[str]]) -> str:
    if query.get("nonc"):
        return ('<p class="flash warn">Nextcloud ist nicht eingerichtet (NC_URL, NC_USER, NC_APP_PASSWORD) – '
                "es wurde nichts geändert.</p>")
    return ""


def _duration(seconds: int) -> str:
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min {seconds % 60} s"


def _when(iso: str | None) -> str:
    if not iso:
        return "–"
    return f"{datetime.fromisoformat(iso):%d.%m. %H:%M}"


def _e(text: Any) -> str:
    return html.escape(str(text))


def handler(portal: AdminPortal) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "gruppix-admin"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._handle("GET", b"")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            if self.path.startswith("/import/upload"):
                try:
                    response = portal.receive_upload(self.path, dict(self.headers.items()), self.rfile, length,
                                                     client=self.client_address[0])
                except Exception:  # noqa: BLE001
                    log.exception("admin: upload failed")
                    response = _json(500, error="Interner Fehler – siehe Protokoll")
                self.close_connection = response.code != 202
                self._answer(response)
                return
            if length > _MAX_FORM_BYTES:
                self._answer(Response(413, b"too large", "text/plain"))
                return
            self._handle("POST", self.rfile.read(length))

        def _handle(self, method: str, body: bytes) -> None:
            try:
                response = portal.dispatch(method, self.path, dict(self.headers.items()), body,
                                           client=self.client_address[0])
            except Exception:  # noqa: BLE001 - one broken page must not take the portal down
                log.exception("admin: %s %s failed", method, self.path)
                response = Response(500, "Interner Fehler – siehe Protokoll".encode(), "text/plain; charset=utf-8")
            self._answer(response)

        def _answer(self, response: Response) -> None:
            self.send_response(response.code)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("admin http: " + fmt, *args)

    return Handler


_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{title}} · gruppix</title>
{{refresh}}
<style nonce="{{nonce}}">{{style}}</style></head>
<body><header class="top"><div><b>gruppix</b><span class="muted">{{club}}</span><nav>{{nav}}</nav></div></header>
<main><h1>{{title}}</h1>
{{queue}}
{{body}}
</main>
{{bar}}
{{script}}
</body></html>
"""

_EXTRA_STYLE = """
header.top { background:var(--card); border-bottom:1px solid var(--line); }
header.top div { max-width:1100px; margin:0 auto; padding:10px 16px; display:flex; gap:14px; align-items:baseline; flex-wrap:wrap; }
header.top nav { margin-left:auto; display:flex; gap:4px; flex-wrap:wrap; }
header.top nav a { color:var(--text); text-decoration:none; padding:6px 10px; border-radius:6px; }
header.top nav a.on { background:var(--bg); font-weight:600; }
.status { font-size:18px; font-weight:600; margin:0 0 8px; }
.status.ok { color:var(--keep); } .status.warning { color:#b7791f; } .status.error { color:var(--drop); }
.notes { margin:0 0 16px; padding-left:20px; } .notes .err { color:var(--drop); } .notes .warn { color:#b7791f; }
.stats { display:grid; grid-template-columns:repeat(auto-fill, minmax(160px, 1fr)); gap:12px; margin-bottom:8px; }
.stat { display:flex; flex-direction:column; color:var(--text); text-decoration:none; }
.stat .num { font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }
table.jobs { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; }
table.jobs th, table.jobs td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-size:14px; vertical-align:top; }
table.jobs td.err { color:var(--drop); }
@media (max-width:700px) { table.jobs th:nth-child(4), table.jobs td:nth-child(4) { display:none; } }
pre.box { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; overflow:auto;
          white-space:pre-wrap; font:13px/1.45 ui-monospace, Consolas, monospace; }
pre.log { max-height:420px; }
.flash { background:var(--card); border-left:4px solid var(--keep); padding:10px 12px; border-radius:6px; }
.flash.warn { border-color:#b7791f; }
.tile:has(input:checked) { outline:3px solid var(--keep); outline-offset:-1px; }
.day h2 { display:flex; gap:10px; align-items:baseline; }
button.link { background:none; border:0; color:var(--accent); cursor:pointer; font:inherit; font-size:14px; padding:0; }
.pairbox { margin:0 0 16px; }
fieldset.choice { border:0; padding:0; margin:12px 0 0; display:flex; gap:18px; flex-wrap:wrap; }
fieldset.choice label { display:inline-flex; gap:6px; align-items:center; cursor:pointer; }
.pairbox figure.big, .pairbox figure.small { border-top:4px solid var(--keep); }
.fate { font-size:12px; padding:1px 6px; border-radius:4px; margin-left:4px; display:none; }
.fate.stays { background:var(--keep); color:#fff; } .fate.goes { background:var(--drop); color:#fff; }
.pairbox:has(input[value=drop]:checked) figure.small { border-color:var(--drop); }
.pairbox:has(input[value=swap]:checked) figure.big { border-color:var(--drop); }
.pairbox:has(input[value=drop]:checked) figure.big .stays,
.pairbox:has(input[value=drop]:checked) figure.small .goes,
.pairbox:has(input[value=swap]:checked) figure.small .stays,
.pairbox:has(input[value=swap]:checked) figure.big .goes,
.pairbox:has(input[value=keep]:checked) .stays { display:inline; }
.pairbox:has(input[value=skip]:checked) { opacity:.55; }
.bar .grow { flex:1; }
.scan { margin:0 0 16px; } .scan p { margin:0 0 10px; } .scan .err { color:var(--drop); }
.scan progress { width:100%; height:10px; margin:0 0 10px; accent-color:var(--accent); }
form.inline { margin:0; }
form.inline button, .bar button { font:inherit; padding:8px 14px; border:0; border-radius:6px;
                                  background:var(--accent); color:#fff; cursor:pointer; }
.spinner { display:inline-block; width:12px; height:12px; border:2px solid var(--line); border-top-color:var(--accent);
           border-radius:50%; animation:spin 1s linear infinite; vertical-align:-1px; }
@keyframes spin { to { transform:rotate(360deg); } }
.bar button:disabled { opacity:.5; cursor:default; }
.queue { margin:0 0 16px; } #queue.flash { margin:0 0 16px; }
.tile.queued { opacity:.55; cursor:default; }
.importform { display:grid; gap:14px; max-width:560px; padding:16px; }
.importform label { display:grid; gap:4px; font-weight:600; }
.importform input, .importform select { font:inherit; font-weight:400; padding:7px 8px; border:1px solid var(--line);
                                        border-radius:6px; background:var(--bg); color:var(--text); }
.importform .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.importform button { font:inherit; padding:8px 14px; border:0; border-radius:6px; background:var(--accent);
                     color:#fff; cursor:pointer; }
.importform button:disabled { opacity:.5; cursor:default; }
.importform progress { flex:1; min-width:160px; height:10px; accent-color:var(--accent); }
.flash ul { margin:6px 0; padding-left:20px; } .flash p { margin:4px 0; } div.flash { margin:0 0 16px; }
.bar .target { display:inline-flex; gap:6px; align-items:center; }
@media (max-width:600px) { .bar #sum { flex-basis:100%; } }
.bar select { font:inherit; padding:7px 8px; border:1px solid var(--line); border-radius:6px; background:var(--bg);
              color:var(--text); max-width:min(420px, 70vw); }
"""

_COMMON_JS = """
let dirty = false;
document.querySelectorAll('input').forEach(i => i.addEventListener('change', () => { dirty = true; }));
document.querySelectorAll('form').forEach(f => f.addEventListener('submit', () => {
  // one click is enough: block a second submit while the page reloads
  document.querySelectorAll('button[type=submit]').forEach(b => { b.disabled = true; b.textContent = 'Wird übertragen …'; });
}));
const banner = document.getElementById('queue');
async function poll() {
  try {
    const st = await (await fetch('/queue', {cache: 'no-store'})).json();
    const text = document.getElementById('queue-text');
    if (st.active) {
      text.textContent = 'Aufträge werden ausgeführt: ' + st.done + ' von ' + st.total +
        (st.waiting_for ? ' · wartet auf „' + st.waiting_for + '“' : '');
      setTimeout(poll, 2000);
    } else if (!dirty) {
      location.reload();
    } else {
      banner.className = 'flash';
      text.textContent = 'Erledigt: ' + st.summary + ' – Seite neu laden, um den aktuellen Stand zu sehen.';
    }
  } catch (e) { setTimeout(poll, 5000); }
}
if (banner && banner.dataset.active === '1') setTimeout(poll, 2000);
"""

_IMPORT_JS = """
const upForm = document.getElementById('up');
upForm.addEventListener('submit', e => {
  e.preventDefault();
  const file = document.getElementById('zip').files[0];
  const go = document.getElementById('go'), bar = document.getElementById('bar'), msg = document.getElementById('msg');
  if (!file) return;
  const q = new URLSearchParams({name: file.name, since: document.getElementById('since').value,
                                 until: document.getElementById('until').value, order: document.getElementById('order').value});
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/import/upload?' + q.toString());
  xhr.setRequestHeader('Content-Type', 'application/zip');
  bar.hidden = false; go.disabled = true; go.textContent = 'Wird hochgeladen …';
  xhr.upload.onprogress = ev => {
    if (ev.lengthComputable) {
      bar.value = Math.round(ev.loaded / ev.total * 100);
      msg.textContent = (ev.loaded / 1048576).toFixed(0) + ' von ' + (ev.total / 1048576).toFixed(0) + ' MB';
    }
  };
  xhr.onload = () => {
    if (xhr.status === 202) { location.href = '/import'; return; }
    let text = 'Fehler ' + xhr.status;
    try { text = JSON.parse(xhr.responseText).error || text; } catch (err) {}
    msg.textContent = text; go.disabled = false; go.textContent = 'Hochladen und importieren';
  };
  xhr.onerror = () => { msg.textContent = 'Verbindung abgebrochen'; go.disabled = false; go.textContent = 'Hochladen und importieren'; };
  xhr.send(file);
});
"""

_QUARANTINE_JS = """
const boxes = [...document.querySelectorAll('input[name=id]')];
const sum = document.getElementById('sum'), go = document.getElementById('go');
function update() {
  const n = boxes.filter(b => b.checked).length;
  sum.textContent = n ? n + ' ausgewählt' : 'Nichts ausgewählt';
  go.disabled = !n;
}
boxes.forEach(b => b.addEventListener('change', update));
document.querySelectorAll('button.all').forEach(btn => btn.addEventListener('click', () => {
  const day = [...btn.closest('.day').querySelectorAll('input[name=id]')];
  const on = !day.every(b => b.checked);
  day.forEach(b => { b.checked = on; });
  update();
}));
update();
"""

_DUPLICATES_JS = """
const radios = [...document.querySelectorAll('fieldset.choice input')];
const sum = document.getElementById('sum');
function update() {
  const c = {drop: 0, swap: 0, keep: 0, skip: 0};
  radios.filter(r => r.checked).forEach(r => c[r.value]++);
  sum.textContent = (c.drop + c.swap) + ' löschen · ' + c.keep + ' beide behalten · ' + c.skip + ' später';
}
radios.forEach(r => r.addEventListener('change', update));
update();
"""
