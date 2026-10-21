"""Health checks and the weekly report (texts are German, they go to people)."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from typing import Any

import requests

from .config import Config
from .db import Database, State
from .events import check_sources
from .sinks import WebDavSink

_GB = 1024**3

# WAHA session states, see WAHA docs "Sessions"
_WAHA_HINTS = {
    "SCAN_QR_CODE": "Kopplung verloren – im WAHA-Dashboard den QR-Code mit dem Bot-Handy neu scannen",
    "FAILED": "Sitzung fehlgeschlagen – Bot-Handy online? Im WAHA-Dashboard neu starten",
    "STOPPED": "Sitzung gestoppt – im WAHA-Dashboard starten",
}


class WahaUnreachable(Exception):
    """WAHA did not answer; the caller decides when this becomes an alert."""


def waha_problems(cfg: Config) -> list[str]:
    """Problems with the WhatsApp session; empty while no session exists yet.

    Raises WahaUnreachable if the API does not answer properly.
    """
    if not (cfg.waha_url and cfg.waha_api_key):
        return []
    try:
        response = requests.get(
            f"{cfg.waha_url.rstrip('/')}/api/sessions",
            params={"all": "true"},
            headers={"X-Api-Key": cfg.waha_api_key},
            timeout=30,
        )
        response.raise_for_status()
        sessions = response.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        hint = " – API-Schlüssel prüfen" if status == 401 else ""
        raise WahaUnreachable(f"WAHA antwortet mit HTTP {status}{hint}") from exc
    except (requests.RequestException, ValueError) as exc:
        raise WahaUnreachable(f"WAHA nicht erreichbar ({type(exc).__name__})") from exc
    problems = []
    for session in sessions:
        status = str(session.get("status", "")).upper()
        if status in ("WORKING", "STARTING"):
            continue
        hint = _WAHA_HINTS.get(status, f"Status {status}")
        problems.append(f"WhatsApp-Sitzung '{session.get('name', '?')}': {hint}")
    return problems


def calendar_problems(cfg: Config) -> list[str]:
    return check_sources(cfg.calendar_sources) if cfg.calendar_sources else []


def storage_problems(cfg: Config, nextcloud: WebDavSink | None) -> list[str]:
    problems = []
    seen_devices: set[int] = set()
    for label, path in (("NAS-Archiv", cfg.archive_dir), ("NAS-Daten", cfg.data_dir)):
        try:
            device = os.stat(path).st_dev
            free = shutil.disk_usage(path).free
        except OSError as exc:
            problems.append(f"{label} nicht lesbar ({exc.strerror or exc})")
            continue
        if device in seen_devices:
            continue  # same volume, already checked
        seen_devices.add(device)
        if free < cfg.min_free_gb_nas * _GB:
            problems.append(f"{label}: nur noch {free / _GB:.1f} GB frei")
    if nextcloud is not None:
        try:
            free = nextcloud.quota_available()
        except requests.RequestException as exc:
            problems.append(f"Nextcloud-Speicher nicht abfragbar ({exc})")
        else:
            if free is not None and free < cfg.min_free_gb_nextcloud * _GB:
                problems.append(f"Nextcloud: nur noch {free / _GB:.1f} GB frei")
    return problems


def weekly_report(cfg: Config, open_problems: list[str],
                  review: tuple[int, int] = (0, 0)) -> tuple[str, dict[str, Any]]:
    """Text for e-mail and figures for Home Assistant, covering the last 7 days.

    ``review``: (duplicate pairs, quarantined/held items) on the review pages."""
    since = int(time.time()) - 7 * 24 * 3600
    db = Database(cfg.db_path)
    try:
        states, folders = db.activity_since(since)
    finally:
        db.close()

    gallery = {f: n for f, n in folders.items() if f.startswith(cfg.nc_gallery_dir + "/")}
    inbox = {f: n for f, n in folders.items() if f.startswith(cfg.nc_inbox_dir + "/")}
    uploaded = sum(gallery.values())
    quarantined = states[State.QUARANTINED] + states[State.PURGED]

    lines = [f"Wochenbericht Gruppix bis {datetime.now(cfg.tz):%d.%m.%Y}", ""]
    if gallery:
        lines.append(f"{uploaded} Bilder/Videos in der Galerie:")
        lines += [f"  {n:>4}  {f.split('/')[-1]}" for f, n in sorted(gallery.items())]
    else:
        lines.append("Keine neuen Bilder in der Galerie.")
    if inbox:
        lines += ["", "Zu prüfen in Fotos-Eingang (ohne Termin):"]
        lines += [f"  {n:>4}  {f.split('/')[-1]}" for f, n in sorted(inbox.items())]
    lines += ["", f"Aussortiert (Quarantäne): {quarantined}"]
    if states[State.HELD]:
        lines.append(f"Zurückgehalten (Termin hat noch zu wenige Bilder): {states[State.HELD]}")
    if states[State.REJECTED]:
        lines.append(f"Abgelehnt (z. B. Video zu lang): {states[State.REJECTED]}")
    if states[State.FAILED]:
        lines.append(f"Verarbeitung fehlgeschlagen: {states[State.FAILED]} (Protokoll prüfen)")
    duplicates, quarantine = review
    if duplicates or quarantine:
        lines += ["", "Zum Durchsehen (Seiten im Projektordner, über File Station öffnen):"]
        if duplicates:
            lines.append(f"  {duplicates:>4}  mögliche Duplikate      data/worker/duplicates.html")
        if quarantine:
            lines.append(f"  {quarantine:>4}  in der Quarantäne       data/worker/quarantine.html")
    lines += ["", "Offene Probleme:" if open_problems else "Keine offenen Probleme."]
    for problem in open_problems:
        first, *rest = problem.splitlines()
        lines.append(f"  - {first}")
        lines += [f"    {line}" for line in rest]

    data = {
        "uploaded": uploaded,
        "folders": len(gallery),
        "inbox_folders": len(inbox),
        "quarantined": quarantined,
        "failed": states[State.FAILED],
        "problems": len(open_problems),
        "review_duplicates": duplicates,
        "review_quarantine": quarantine,
    }
    return "\n".join(lines), data
