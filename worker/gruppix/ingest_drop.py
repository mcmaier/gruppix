"""Ingest source: the photographers' upload folder in Nextcloud.

Convention: one sub-folder per event whose name starts with the date
("2026-07-11 Sommerfest" or "11.07.2026 Sommerfest"). Files are collected
once their folder has been quiet for DROP_QUIET_MINUTES, processed like
WhatsApp media, and deleted from the upload folder afterwards.
"""

from __future__ import annotations

import logging
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, time
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import requests

from .config import Config
from .media import classify
from .pipeline import FAILED_STATES, IncomingMedia, Pipeline, parse_drop_folder
from .sinks import DropSource, RemoteFile

log = logging.getLogger(__name__)

ROOT = ""  # group key for files directly in the upload folder
UNDELETED = "undeleted"  # result key: processed, but still in the upload folder

_warned: set[str] = set()  # ignored files already reported (service runs every 15 min)


def pending_groups(source: DropSource, cfg: Config, now: datetime) -> dict[str, list[RemoteFile]]:
    """Media files per top-level folder, only for folders quiet long enough."""
    groups: dict[str, list[RemoteFile]] = defaultdict(list)
    skipped: Counter[str] = Counter()
    for remote in source.list_tree(cfg.nc_drop_dir):
        rel = PurePosixPath(remote.path).relative_to(cfg.nc_drop_dir)
        name = rel.name
        if name.startswith(".") or name.endswith(".part"):
            continue  # upload in progress
        group = rel.parts[0] if len(rel.parts) > 1 else ROOT
        if classify(Path(name)) is None:
            if remote.path not in _warned:
                _warned.add(remote.path)
                skipped[group or "(upload folder)"] += 1
            continue
        groups[group].append(remote)

    for group, count in skipped.items():
        log.warning("%s: %d files ignored (no JPEG/HEIC/video, e.g. RAW)", group, count)

    ready: dict[str, list[RemoteFile]] = {}
    for group, files in groups.items():
        newest = max(f.uploaded for f in files)
        if now - newest < cfg.drop_quiet:
            log.debug("%s: upload still running, waiting", group or "(upload folder)")
            continue
        ready[group] = files
    return ready


def calendar_range(groups: dict[str, list[RemoteFile]], tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """Earliest and latest relevant moment, for loading the calendar."""
    moments: list[datetime] = []
    for group, files in groups.items():
        day, _ = parse_drop_folder(group or None)
        if day is not None:
            moments.append(datetime.combine(day, time.min, tzinfo=tz))
        moments.extend(f.uploaded for f in files)
    if not moments:
        return None
    return min(moments), max(moments)


def ingest_drop(source: DropSource, groups: dict[str, list[RemoteFile]], pipeline: Pipeline, cfg: Config) -> Counter[str]:
    """Process ready folders; a file that cannot be removed afterwards (e.g. HTTP 423,
    locked by Nextcloud) stays, is counted as UNDELETED and retried next run."""
    results: Counter[str] = Counter()
    with tempfile.TemporaryDirectory(prefix="gruppix-drop-") as tmp:
        for group, files in sorted(groups.items()):
            all_done = True
            # Camera file names (DSC_0001 …) give the shot order.
            for seq, remote in enumerate(sorted(files, key=lambda f: f.path)):
                source_id = f"{remote.path}@{int(remote.uploaded.timestamp())}"
                final = pipeline.state_of("drop", source_id)
                if final is None:  # new; known files only need removing again
                    local = Path(tmp) / PurePosixPath(remote.path).name
                    source.download(remote.path, local)
                    item = IncomingMedia(
                        source="drop",
                        source_id=source_id,
                        chat_id="drop",
                        sender="drop",
                        sent_at=remote.uploaded.astimezone(cfg.tz),
                        path=local,
                        seq=seq,
                        folder_hint=group or None,
                    )
                    results[pipeline.ingest(item)] += 1
                    local.unlink(missing_ok=True)
                    final = pipeline.state_of("drop", source_id)

                if final in FAILED_STATES:
                    all_done = False  # stays in the upload folder for a look
                    continue
                if not _remove(source, remote.path):
                    results[UNDELETED] += 1
                    all_done = False

            if group and all_done:
                folder = f"{cfg.nc_drop_dir}/{group}"
                if not source.list_tree(folder):
                    _remove(source, folder)
            log.info("%s: %d files processed", group or "(upload folder)", len(files))
    return results


def _remove(source: DropSource, path: str) -> bool:
    try:
        source.delete(path)
    except (requests.RequestException, OSError) as exc:
        log.warning("%s: could not remove from the upload folder (%s), retried next run", path, exc)
        return False
    return True
