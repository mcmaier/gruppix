"""Ingest source: WhatsApp chat export ("Chat exportieren" → "Medien anhängen").

Supports Android and iOS exports in German and English, e.g.

    Android:  12.07.26, 18:43 - Anna: IMG-20260712-WA0007.jpg (Datei angehängt)
    iOS:      [12.07.26, 18:43:11] Anna: <Anhang: 00000012-PHOTO-2026-07-12-18-43-11.jpg>

Attachments are recognised by their file name appearing in the message line,
which is independent of language and platform.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .media import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from .pipeline import IncomingMedia, Pipeline

MEDIA_SUFFIXES = tuple(sorted(IMAGE_SUFFIXES | VIDEO_SUFFIXES))

_HEADER = re.compile(
    r"""
    ^\[?
    (?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+
    (?P<time>\d{1,2}:\d{2}(?::\d{2})?)
    (?:\s?(?P<ampm>[AaPp]\.?\s?[Mm]\.?))?
    \]?\s*(?:-\s+)?
    (?P<rest>.*)$
    """,
    re.VERBOSE,
)
# Direction marks, BOM and narrow no-break spaces that WhatsApp sprinkles into exports.
_CLEAN = {0x200E: None, 0x200F: None, 0x202A: None, 0x202C: None, 0xFEFF: None, 0x202F: " ", 0x00A0: " "}


@dataclass(frozen=True)
class ExportMessage:
    sent_at: datetime
    sender: str
    member: str  # path inside the ZIP
    seq: int     # position in the chat, keeps order within the same minute


@dataclass(frozen=True)
class ExportContent:
    chat_id: str
    messages: list[ExportMessage]
    unreferenced: list[str]  # media files in the ZIP not found in the chat text


def read_export(zip_path: Path, tz: ZoneInfo, date_order: str = "dmy") -> ExportContent:
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        texts = [n for n in names if n.lower().endswith(".txt")]
        if not texts:
            raise ValueError(f"{zip_path.name}: no chat text file in export")
        chat_file = next((n for n in texts if PurePosixPath(n).name == "_chat.txt"), texts[0])
        text = zf.read(chat_file).decode("utf-8-sig", errors="replace")
        media = {PurePosixPath(n).name: n for n in names if n.lower().endswith(MEDIA_SUFFIXES)}

    messages = list(parse_chat(text.splitlines(), media, tz, date_order))
    referenced = {m.member for m in messages}
    return ExportContent(
        chat_id=f"export:{zip_path.stem}",
        messages=messages,
        unreferenced=sorted(set(media.values()) - referenced),
    )


def select_period(content: ExportContent, since: date | None, until: date | None) -> ExportContent:
    """Keep only attachments sent between since and until (inclusive, local date).

    The original chat sequence numbers are kept, so a partial import followed
    by a full one yields the same order and source ids (no duplicates).
    """
    if since is None and until is None:
        return content
    kept = [
        m for m in content.messages
        if (since is None or m.sent_at.date() >= since) and (until is None or m.sent_at.date() <= until)
    ]
    return replace(content, messages=kept)


def parse_chat(
    lines: Iterable[str], media: dict[str, str], tz: ZoneInfo, date_order: str = "dmy"
) -> Iterable[ExportMessage]:
    seq = 0
    for raw in lines:
        line = raw.translate(_CLEAN).strip()
        match = _HEADER.match(line)
        if match is None:
            continue  # continuation of a multi-line message
        sender, sep, body = match["rest"].partition(": ")
        if not sep:
            continue  # system message ("X hat Y hinzugefügt")
        lowered = body.lower()
        if not any(suffix in lowered for suffix in MEDIA_SUFFIXES):
            continue
        hits = [name for name in media if name in body]
        if not hits:
            continue  # "<Medien ausgeschlossen>" or file missing in the ZIP
        name = max(hits, key=len)
        yield ExportMessage(_parse_time(match, tz, date_order), sender.strip(), media[name], seq)
        seq += 1


def _parse_time(match: re.Match[str], tz: ZoneInfo, date_order: str) -> datetime:
    parts = re.split(r"[./-]", match["date"])
    a, b, c = (int(p) for p in parts)
    if len(parts[0]) == 4:
        year, month, day = a, b, c
    elif date_order == "mdy":
        month, day, year = a, b, c
    else:
        day, month, year = a, b, c
    if year < 100:
        year += 2000

    clock = [int(p) for p in match["time"].split(":")]
    hour, minute, second = clock[0], clock[1], clock[2] if len(clock) > 2 else 0
    if match["ampm"]:
        hour = hour % 12 + (12 if match["ampm"].lower().startswith("p") else 0)
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


def source_id(content: ExportContent, message: ExportMessage) -> str:
    """Stable id of an attachment: export name + file name (a re-import finds it again)."""
    return f"{content.chat_id}/{PurePosixPath(message.member).name}"


def ingest_export(zip_path: Path, content: ExportContent, pipeline: Pipeline,
                  progress: Callable[[int, int], None] | None = None) -> Counter[str]:
    """Feed all attachments of an export through the pipeline.

    ``progress(done, total)`` is called after each attachment (admin portal)."""
    results: Counter[str] = Counter()
    total = len(content.messages)
    with zipfile.ZipFile(zip_path) as zf, tempfile.TemporaryDirectory(prefix="gruppix-") as tmp:
        for done, message in enumerate(content.messages, start=1):
            target = Path(tmp) / PurePosixPath(message.member).name
            with zf.open(message.member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            item = IncomingMedia(
                source="export",
                source_id=source_id(content, message),
                chat_id=content.chat_id,
                sender=message.sender,
                sent_at=message.sent_at,
                path=target,
                seq=message.seq,
            )
            results[pipeline.ingest(item)] += 1
            target.unlink(missing_ok=True)
            if progress is not None:
                progress(done, total)
    return results
