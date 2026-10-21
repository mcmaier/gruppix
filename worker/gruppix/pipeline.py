"""Ingest pipeline: de-duplicate, match to events, process, archive, upload."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import media
from .config import Config
from .db import Database, State
from .events import MANUAL_PREFIX, Event, EventCalendar, sanitize
from .sinks import Sink

log = logging.getLogger(__name__)

IGNORED = "ignored"  # not an image/video; nothing stored
KNOWN = "known"      # this exact message was ingested before
FAILED_STATES = frozenset({State.FAILED})

# Same photo arriving as a different file (export vs. live, HD + standard size):
CHAT_SOURCES = ("export", "waha")
DROP_SOURCE = "drop"
SIMILAR_WINDOW_SECONDS = 15 * 60  # export times have minute precision, phone clocks drift
SIMILAR_MAX_BITS = 5              # dHash distance (of 64) still counted as the same photo


@dataclass(frozen=True)
class IncomingMedia:
    source: str            # export | waha | drop
    source_id: str         # stable id within the source
    chat_id: str | None
    sender: str            # raw sender id or display name; hashed before storage
    sent_at: datetime      # message time, timezone-aware
    path: Path             # raw file as received
    seq: int = 0           # keeps order of items sharing the same timestamp
    folder_hint: str | None = None  # drop source: top-level upload folder name
    sender_hash: str | None = None  # already hashed (live intake keeps no raw ids on disk)


def hash_sender(salt: str, sender: str) -> str:
    """Salted, non-reversible sender id; the only form in which senders are stored."""
    return hmac.new(salt.encode(), sender.encode(), hashlib.sha256).hexdigest()[:16]


_FOLDER_DATE = (
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?P<rest>.*)$"),
    re.compile(r"^(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})(?P<rest>.*)$"),
)


def parse_drop_folder(name: str | None) -> tuple[date | None, str | None]:
    """'2026-07-11 Sommerfest' or '11.07.2026 Sommerfest' → (date, '2026-07-11 Sommerfest')."""
    if not name:
        return None, None
    for pattern in _FOLDER_DATE:
        match = pattern.match(name.strip())
        if match is None:
            continue
        try:
            day = date(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            break
        rest = sanitize(match["rest"].strip(" _-.")) if match["rest"].strip(" _-.") else ""
        return day, f"{day:%Y-%m-%d} {rest}".strip()
    return None, sanitize(name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Pipeline:
    def __init__(self, cfg: Config, db: Database, calendar: EventCalendar, gallery: Sink) -> None:
        self._cfg = cfg
        self._db = db
        self._calendar = calendar
        self._gallery = gallery
        cfg.staging_dir.mkdir(parents=True, exist_ok=True)
        cfg.outbox_dir.mkdir(parents=True, exist_ok=True)

    def _staging_path(self, item: IncomingMedia, digest: str) -> Path:
        """Readable name for files waiting in staging/quarantine.

        staging/2026-07/2026-07-11_184311_00007_IMG-20260711-WA0007_a1b2c3.jpg
        → send time, position in the chat, original file name, short hash.
        """
        sent = item.sent_at.astimezone(self._cfg.tz)
        stem = sanitize(item.path.stem, max_len=40).replace(" ", "_")
        name = f"{sent:%Y-%m-%d_%H%M%S}_{item.seq:05d}_{stem}_{digest[:6]}{item.path.suffix.lower()}"
        return self._cfg.staging_dir / f"{sent:%Y-%m}" / name

    def sender_hash(self, sender: str) -> str:
        return hash_sender(self._cfg.sender_salt, sender)

    # --- ingest ------------------------------------------------------------

    def ingest(self, item: IncomingMedia) -> str:
        kind = media.classify(item.path)
        if kind is None:
            return IGNORED
        media_id = f"{item.source}:{item.source_id}"
        if self._db.exists(media_id):
            return KNOWN

        digest = sha256_file(item.path)
        sender_hash = item.sender_hash or self.sender_hash(item.sender)
        msg_ts = int(item.sent_at.timestamp())
        # Live/Motion photos arrive as 1–3 s clips: shown as a still photo in
        # the photo timeline, the clip itself is archived next to it.
        still = self._still_from_short_clip(item.path) if kind == "video" else None
        clip_captured = None
        if still is not None:
            clip_captured = media.read_capture_time(item.path, "video", self._cfg.tz)
            kind = "image"
        print_ = media.fingerprint(still or item.path) if kind == "image" else None
        base = {
            "id": media_id,
            "source": item.source,
            "chat_id": item.chat_id,
            "sender_hash": sender_hash,
            "msg_ts": msg_ts,
            "ts": msg_ts,
            "kind": kind,
            "sha256": digest,
            "phash": print_[0] if print_ else None,
            "pixels": print_[1] if print_ else None,
        }

        if self._db.find_live_by_sha(digest) is not None:
            _discard(still)
            self._db.insert(**base, state=State.DUPLICATE)
            return State.DUPLICATE
        if self._db.find_by_sha(digest, (State.REMOVED,)) is not None:
            # Deleted on request (❌) but still in the chat: a later export
            # import must not bring it back.
            _discard(still)
            self._db.insert(**base, state=State.REMOVED)
            return State.REMOVED
        if print_ is not None and item.source in CHAT_SOURCES:
            twin = self._similar_chat_item(item.source, print_, msg_ts)
            if twin is not None:
                state = State.REMOVED if twin["state"] == State.REMOVED else State.DUPLICATE
                _discard(still)
                self._db.insert(**base, state=state)
                log.debug("%s looks like %s (%s)", media_id, twin["id"], state)
                return state
        if self._db.is_opted_out(sender_hash):
            _discard(still)
            self._db.insert(**base, state=State.SKIPPED)
            return State.SKIPPED

        staged = self._staging_path(item, digest)
        staged.parent.mkdir(parents=True, exist_ok=True)
        if still is not None:
            shutil.copyfile(item.path, staged)            # the clip, archived later
            base["companion_path"] = str(staged)
            staged = staged.with_suffix(".jpg")
            shutil.move(still, staged)
            captured = clip_captured
        else:
            shutil.copyfile(item.path, staged)
            captured = media.read_capture_time(staged, kind, self._cfg.tz)
        if item.source == "drop":
            return self._ingest_drop(base, item, staged, captured)
        if captured is not None and media.is_plausible(captured, item.sent_at):
            sort_time, ts_source = captured, "metadata"
        else:
            sort_time, ts_source = item.sent_at, "message"
        base["ts"] = int(sort_time.timestamp())
        self._db.insert(**base, seq=item.seq, ts_source=ts_source, staged_path=str(staged), state=State.STAGED)

        event = self._calendar.match(sort_time)
        if event is not None:
            return self._assign(media_id, event)

        self._db.update(media_id, state=State.QUARANTINED)
        if ts_source == "message":
            # Photos are usually posted days after the event, as a batch.
            # Single late images (typically memes) stay in quarantine.
            return self._try_late_batch(media_id)
        return State.QUARANTINED

    def _still_from_short_clip(self, path: Path) -> Path | None:
        limit = self._cfg.clip_as_photo_seconds
        if limit <= 0:
            return None
        duration = media.short_clip_duration(path, limit)
        if duration is None:
            return None
        still = self._cfg.staging_dir / f".still-{path.stem}-{id(path)}.jpg"
        still.parent.mkdir(parents=True, exist_ok=True)
        try:
            media.extract_still(path, still, duration / 2)
        except Exception as exc:  # noqa: BLE001 - fall back to a normal video
            log.warning("no still from %s: %s", path.name, exc)
            _discard(still)
            return None
        return still if still.exists() and still.stat().st_size else None

    def _similar_chat_item(self, source: str, print_: tuple[str, int], msg_ts: int):
        """An earlier chat item showing the same photo, sent around the same time.

        From the other source (export ↔ live) any size counts. Within one
        source only a differently sized copy (HD + standard in one export),
        so two near-identical burst shots both stay.
        """
        phash, pixels = print_
        rows = self._db.fingerprinted(
            msg_ts - SIMILAR_WINDOW_SECONDS, msg_ts + SIMILAR_WINDOW_SECONDS,
            CHAT_SOURCES, (*State.LIVE, State.REMOVED),
        )
        best, best_distance = None, SIMILAR_MAX_BITS + 1
        for row in rows:
            if row["source"] == source and row["pixels"] == pixels:
                continue
            distance = media.hash_distance(phash, row["phash"])
            if distance < best_distance:
                best, best_distance = row, distance
        return best

    def similar_pairs(self) -> list[tuple]:
        """Stored photos that look like the same picture (report only, an admin decides).

        Returns (keep, extra, distance) tuples; the larger copy is kept, the
        other one is offered for removal. Two kinds of pairs:

        - chat ↔ chat (export, live) sent within SIMILAR_WINDOW_SECONDS;
        - photographer upload ↔ chat photo of the same event, at any time apart
          (camera originals uploaded days after a few were posted in the group).
          On equal size the upload is kept.

        Pairs an admin marked as different (``distinct_pairs``) are left out.
        """
        rows = self._db.fingerprinted_rows((*CHAT_SOURCES, DROP_SOURCE), State.PUBLISHED)
        distinct = self._db.distinct_pairs()
        pairs: list[tuple] = []
        seen: set[str] = set()  # extras already offered; each copy is offered once

        def offer(keep, extra, distance: int) -> None:
            pairs.append((keep, extra, distance))
            seen.add(extra["id"])

        chat = [r for r in rows if r["source"] in CHAT_SOURCES]
        for i, a in enumerate(chat):
            if a["id"] in seen:
                continue
            for b in chat[i + 1:]:
                if b["msg_ts"] - a["msg_ts"] > SIMILAR_WINDOW_SECONDS:
                    break
                if b["id"] in seen or (a["source"] == b["source"] and a["pixels"] == b["pixels"]):
                    continue
                if tuple(sorted((a["id"], b["id"]))) in distinct:
                    continue
                distance = media.hash_distance(a["phash"], b["phash"])
                if distance <= SIMILAR_MAX_BITS:
                    keep, extra = (a, b) if (a["pixels"] or 0) >= (b["pixels"] or 0) else (b, a)
                    offer(keep, extra, distance)

        uploads: dict[str, list] = {}
        for row in rows:
            if row["source"] == DROP_SOURCE and row["event_key"]:
                uploads.setdefault(row["event_key"], []).append(row)
        for c in chat:
            if c["id"] in seen or not c["event_key"] or c["event_key"] not in uploads:
                continue
            best, best_distance = None, SIMILAR_MAX_BITS + 1
            for d in uploads[c["event_key"]]:
                if d["id"] in seen or tuple(sorted((c["id"], d["id"]))) in distinct:
                    continue
                distance = media.hash_distance(c["phash"], d["phash"])
                if distance < best_distance:
                    best, best_distance = d, distance
            if best is not None:
                keep, extra = (best, c) if (best["pixels"] or 0) >= (c["pixels"] or 0) else (c, best)
                offer(keep, extra, best_distance)
        return pairs

    def drop_duplicate(self, extra_id: str, keep_id: str) -> str:
        """Take a confirmed second copy out of the gallery (find-duplicates --delete).

        Unlike ``remove`` the archived original stays and the photo is not
        blocked: the kept copy still shows it."""
        row = self._db.get(extra_id)
        if row["state"] not in State.PUBLISHED:
            return row["state"]
        if row["state"] == State.UPLOADED and row["remote_path"]:
            self._gallery.delete(row["remote_path"])  # 404 (already deleted by hand) is fine
        if row["remote_name"]:
            (self._cfg.outbox_dir / row["remote_name"]).unlink(missing_ok=True)
        self._db.update(extra_id, state=State.DUPLICATE, error=f"duplicate of {keep_id}")
        log.info("%s removed from the gallery as duplicate of %s", extra_id, keep_id)
        return State.DUPLICATE

    def backfill_fingerprints(self, progress: Callable[[int, int], None] | None = None) -> int:
        """Fingerprint images stored before fingerprints existed. Returns the count.

        ``progress(done, total)`` is called after each image (admin portal)."""
        done = 0
        rows = self._db.missing_fingerprint()
        for n, row in enumerate(rows, start=1):
            path = next((Path(p) for p in (row["staged_path"], row["archive_path"]) if p and Path(p).exists()), None)
            print_ = media.fingerprint(path) if path else None
            if print_ is not None:
                self._db.update(row["id"], phash=print_[0], pixels=print_[1])
                done += 1
            if progress is not None:
                progress(n, len(rows))
        return done

    def _ingest_drop(self, base: dict, item: IncomingMedia, staged: Path, captured: datetime | None) -> str:
        """Photographer uploads: the folder date decides, EXIF orders.

        If the folder date has no event (a typo, the second day of a trip), the
        camera time picks the event, else an event with a similar name one day
        before or after. Nothing from the upload folder goes to quarantine:
        what cannot be placed lands in the admin inbox.
        """
        media_id = base["id"]
        day, folder = parse_drop_folder(item.folder_hint)
        plausible = captured is not None and media.is_plausible(captured, item.sent_at)
        event: Event | None = None
        if day is not None:
            # Camera clocks are often off by hours or reset to a default date:
            # for ordering, trust EXIF only if it agrees with the folder date.
            if captured is not None and abs((captured.date() - day).days) <= 1:
                sort_time, ts_source = captured, "metadata"
            else:
                sort_time, ts_source = datetime.combine(day, time(12), tzinfo=self._cfg.tz), "folder"
            event = self._calendar.on_day(day, prefer=sort_time)
            if event is None and plausible:
                event = self._calendar.match(captured)
                if event is not None:
                    sort_time, ts_source = captured, "metadata"
            if event is None:
                event = self._calendar.near_day(day, folder[len("YYYY-MM-DD "):] if folder else "")
                if event is not None and ts_source == "folder":
                    # The folder date was wrong: keep the photos inside the event.
                    sort_time = min(max(sort_time, event.start), event.end - timedelta(seconds=1))
        elif plausible:
            sort_time, ts_source = captured, "metadata"
        else:
            sort_time, ts_source = item.sent_at, "message"
        self._db.insert(
            **{**base, "ts": int(sort_time.timestamp())},
            seq=item.seq,
            ts_source=ts_source,
            staged_path=str(staged),
            state=State.STAGED,
        )

        # Photographer uploads are deliberate: no minimum per event.
        if day is not None:
            if event is not None:
                return self._assign(media_id, event, force=True)
            return self._publish(media_id, f"{day:%Y}/{folder}", target="gallery")
        if folder is not None:  # sub-folder without a recognisable date
            return self._publish(media_id, f"{sort_time:%Y}/{folder}", target="inbox")
        if ts_source == "metadata":
            event = self._calendar.match(sort_time)
            if event is not None:
                return self._assign(media_id, event, force=True)
        upload_day = sort_time.date()
        return self._publish(media_id, f"{upload_day:%Y}/{upload_day:%Y-%m-%d} Upload", target="inbox")

    def state_of(self, source: str, source_id: str) -> str | None:
        try:
            return self._db.get(f"{source}:{source_id}")["state"]
        except KeyError:
            return None

    def _try_late_batch(self, media_id: str) -> str:
        """Assign a late batch (one sender, several media in a few minutes) to the
        most recent event within the late-grace period."""
        row = self._db.get(media_id)
        gap = int(self._cfg.late_batch_gap.total_seconds())
        span = gap * 50  # generous search range; the chain below decides
        rows = [r for r in self._db.sender_rows(row["sender_hash"], row["msg_ts"] - span, row["msg_ts"] + span)
                if r["ts_source"] == "message"]

        # Split the sender's items into chains with gaps <= LATE_BATCH_GAP and
        # take the chain containing this item.
        chain: list = []
        for r in rows:
            if chain and r["msg_ts"] - chain[-1]["msg_ts"] > gap:
                if any(c["id"] == media_id for c in chain):
                    break
                chain = []
            chain.append(r)
        if len(chain) < self._cfg.late_min_batch:
            return State.QUARANTINED

        result = State.QUARANTINED
        for member in chain:
            if member["state"] != State.QUARANTINED:
                continue
            sent = datetime.fromtimestamp(member["msg_ts"], self._cfg.tz)
            event = self._calendar.match(sent, late=True)
            if event is None:
                continue
            state = self._assign(member["id"], event)
            if member["id"] == media_id:
                result = state
        return result

    def _assign(self, media_id: str, event: Event, *, force: bool = False) -> str:
        """Put an item into its event folder — or hold it while the event has
        fewer than MIN_EVENT_ITEMS items (single memes in an event window)."""
        row = self._db.get(media_id)
        sort_time = datetime.fromtimestamp(row["ts"], self._cfg.tz)
        # An estimated date must not lie after the event: a Saturday photo
        # posted on Tuesday shows up on Saturday in timeline views.
        exif_time = sort_time
        if row["ts_source"] == "message" and sort_time >= event.end:
            exif_time = event.end - timedelta(seconds=1)
        folder = self._db.event_folder(event.key, event.folder_name(), event.title, int(event.start.timestamp()))
        rel_dir = f"{event.start:%Y}/{folder}"
        self._db.update(
            media_id,
            exif_ts=int(exif_time.timestamp()),
            event_key=event.key,
            pending_dir=rel_dir,
            # Late batches may still arrive until the end of the late-grace period.
            hold_until=int((event.end + max(self._cfg.late_grace, self._cfg.window_post)).timestamp()),
        )

        minimum = self._cfg.min_event_items
        open_event = self._db.count_for_event(event.key, State.PUBLISHED) > 0
        # An admin-created event ("Ordner …") is deliberate: no minimum either.
        manual = event.key.startswith(MANUAL_PREFIX)
        if force or manual or row["pinned"] or minimum <= 1 or open_event:
            return self._publish(media_id, rel_dir, target="gallery", event_key=event.key)

        self._db.update(media_id, state=State.HELD)
        if self._db.count_for_event(event.key, (State.HELD,)) < minimum:
            return State.HELD
        # Threshold reached: release everything held for this event.
        result = State.HELD
        for held in self._db.held_for_event(event.key):
            state = self._publish(held["id"], held["pending_dir"], target="gallery", event_key=event.key)
            if held["id"] == media_id:
                result = state
        return result

    @property
    def calendar(self) -> EventCalendar:
        return self._calendar

    def add_event(self, event: Event) -> int:
        """Admin command: make the event known and take over quarantined items
        captured/sent during it (pre-window to end; not the post-window, which
        would pull in the next days' memes). Returns the number taken over."""
        self._calendar = self._calendar.with_events([event])
        start = (event.start - self._cfg.window_pre).timestamp()
        end = event.end.timestamp()
        adopted = 0
        for row in self._db.by_state(State.QUARANTINED):
            if start <= row["ts"] <= end and row["staged_path"]:
                if self._assign(row["id"], event) in State.PUBLISHED:
                    adopted += 1
        return adopted

    def settle_held(self, now: datetime) -> int:
        """Events whose window has closed without reaching MIN_EVENT_ITEMS:
        their held items go to quarantine (restorable with 📸 via `pin`)."""
        moved = 0
        for row in self._db.by_state(State.HELD):
            if row["hold_until"] is not None and row["hold_until"] <= now.timestamp():
                self._db.update(row["id"], state=State.QUARANTINED)
                moved += 1
        return moved

    def pin(self, media_id: str) -> str:
        """Mark an item for the gallery (📸 reaction): publish it regardless of
        thresholds, also from quarantine. Returns the new state."""
        row = self._db.get(media_id)
        self._db.update(media_id, pinned=1)
        if row["state"] not in (State.HELD, State.QUARANTINED) or not row["staged_path"]:
            return row["state"]
        if row["pending_dir"]:
            return self._publish(media_id, row["pending_dir"], target="gallery", event_key=row["event_key"])
        # No event: admin inbox, folder per day
        day = (datetime.fromtimestamp(row["ts"], self._cfg.tz) - timedelta(hours=self._cfg.day_cutoff_hour)).date()
        return self._publish(media_id, f"{day:%Y}/{day:%Y-%m-%d} Markiert", target="inbox")

    def remove(self, media_id: str) -> str:
        """Delete an item everywhere (❌ reaction, "delete for everyone"):
        gallery copy, archived original, staged and outbox files."""
        row = self._db.get(media_id)
        if row["state"] not in State.LIVE and row["state"] not in (State.FAILED, State.REJECTED):
            return row["state"]
        if row["state"] == State.UPLOADED and row["remote_path"]:
            self._gallery.delete(row["remote_path"])  # raises -> retried by the caller
        if row["remote_name"]:
            (self._cfg.outbox_dir / row["remote_name"]).unlink(missing_ok=True)
        if row["archive_path"]:
            Path(row["archive_path"]).unlink(missing_ok=True)
        if row["companion_path"]:
            Path(row["companion_path"]).unlink(missing_ok=True)
        if row["staged_path"]:
            staged = Path(row["staged_path"])
            staged.unlink(missing_ok=True)
            _remove_if_empty(staged.parent, stop=self._cfg.staging_dir)
        self._db.update(media_id, state=State.REMOVED, staged_path=None, archive_path=None, pinned=0,
                        companion_path=None)
        log.info("Removed %s on request", media_id)
        return State.REMOVED

    # --- publishing --------------------------------------------------------

    def _publish(self, media_id: str, rel_dir: str, *, target: str, event_key: str | None = None,
                 long_video: bool = False) -> str:
        row = self._db.get(media_id)
        staged = Path(row["staged_path"])
        # File name drives the order in the shared folder: sort time, then the
        # sequence within that second, so a sender's batch stays together.
        sort_time = datetime.fromtimestamp(row["ts"], self._cfg.tz)
        taken_at = datetime.fromtimestamp(row["exif_ts"] or row["ts"], self._cfg.tz)
        name = f"{sort_time:%Y%m%d-%H%M%S}-{row['seq']:05d}_{row['sha256'][:6]}"
        base_rel = rel_dir
        if row["kind"] == "video":
            rel_dir = f"{rel_dir}/Videos"
        out_name = name + media.OUTPUT_SUFFIX[row["kind"]]
        out = self._cfg.outbox_dir / out_name

        try:
            if row["kind"] == "image":
                media.process_image(
                    staged, out, taken_at,
                    max_edge=self._cfg.img_max_edge,
                    max_bytes=self._cfg.img_max_bytes,
                    quality=self._cfg.img_quality,
                )
            else:
                media.process_video(
                    staged, out, taken_at,
                    max_edge=self._cfg.video_max_edge,
                    max_seconds=self._cfg.video_inbox_max_seconds if long_video else self._cfg.video_max_seconds,
                    crf=self._cfg.video_crf,
                )
        except media.VideoTooLong as exc:
            out.unlink(missing_ok=True)
            if not long_video and exc.seconds <= self._cfg.video_inbox_max_seconds:
                # Too long for the gallery: an admin decides (Fotos-Eingang, same folder structure).
                log.info("%s: %s -> admin inbox", media_id, exc)
                return self._publish(media_id, base_rel, target="inbox", event_key=event_key, long_video=True)
            return self._reject_keep_original(media_id, staged, rel_dir, name, str(exc))
        except media.MediaRejected as exc:
            out.unlink(missing_ok=True)
            return self._reject_keep_original(media_id, staged, rel_dir, name, str(exc))
        except Exception as exc:  # noqa: BLE001 - keep staged file for analysis
            log.exception("Processing failed for %s", media_id)
            out.unlink(missing_ok=True)
            self._db.update(media_id, state=State.FAILED, error=str(exc)[:500])
            return State.FAILED

        # Archive the original exactly as received; inbox items keep a separate tree.
        archive_rel = rel_dir if target == "gallery" else f"_Eingang/{rel_dir}"
        archive_path = self._cfg.archive_dir / archive_rel / (name + staged.suffix)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, archive_path)
        staged.unlink()
        companion = self._archive_companion(row, archive_path)
        _remove_if_empty(staged.parent, stop=self._cfg.staging_dir)

        base_dir = self._cfg.nc_gallery_dir if target == "gallery" else self._cfg.nc_inbox_dir
        self._db.update(
            media_id,
            state=State.READY,
            target=target,
            event_key=event_key,
            remote_name=out_name,
            remote_path=f"{base_dir}/{rel_dir}/{out_name}",
            archive_path=str(archive_path),
            staged_path=None,
            companion_path=companion,
            error=None,
        )
        return self._upload(media_id)

    def _archive_companion(self, row, archive_path: Path) -> str | None:
        """Short clip shown as still: the clip goes to the archive next to the still."""
        if not row["companion_path"]:
            return None
        clip = Path(row["companion_path"])
        if not clip.exists():
            return None
        target = archive_path.with_suffix(clip.suffix.lower())
        shutil.copy2(clip, target)
        clip.unlink()
        return str(target)

    def _reject_keep_original(self, media_id: str, staged: Path, rel_dir: str, name: str, error: str) -> str:
        """Not publishable (e.g. a very long video): keep the original in the archive only."""
        archive_path = self._cfg.archive_dir / "_Eingang" / rel_dir / (name + staged.suffix)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, archive_path)
        staged.unlink()
        companion = self._archive_companion(self._db.get(media_id), archive_path)
        _remove_if_empty(staged.parent, stop=self._cfg.staging_dir)
        self._db.update(media_id, state=State.REJECTED, error=error, staged_path=None,
                        archive_path=str(archive_path), companion_path=companion)
        log.info("%s rejected (%s), original archived", media_id, error)
        return State.REJECTED

    def _upload(self, media_id: str) -> str:
        row = self._db.get(media_id)
        out = self._cfg.outbox_dir / row["remote_name"]
        try:
            self._gallery.put(out, row["remote_path"])
        except Exception as exc:  # noqa: BLE001 - stays READY, retried later
            log.warning("Upload failed for %s: %s", media_id, exc)
            self._db.update(media_id, attempts=row["attempts"] + 1, error=str(exc)[:500])
            return State.READY
        out.unlink(missing_ok=True)
        self._db.update(media_id, state=State.UPLOADED, error=None)
        return State.UPLOADED

    def retry_uploads(self) -> int:
        """Retry all pending uploads; returns the number still pending."""
        pending = 0
        for row in self._db.by_state(State.READY):
            if self._upload(row["id"]) == State.READY:
                pending += 1
        return pending

    # --- quarantine --------------------------------------------------------

    def promote_clusters(self, closed_before: datetime | None = None) -> list[str]:
        """Publish bursts of quarantined photos (no calendar entry) to the admin inbox.

        A burst is a run of items with gaps <= CLUSTER_MAX_GAP_HOURS that has at
        least CLUSTER_MIN_ITEMS items from CLUSTER_MIN_SENDERS distinct senders.
        With ``closed_before`` only bursts that ended before that time qualify.
        """
        gap = self._cfg.cluster_max_gap.total_seconds()
        clusters: list[list] = []
        for row in self._db.by_state(State.QUARANTINED):
            if clusters and row["ts"] - clusters[-1][-1]["ts"] <= gap:
                clusters[-1].append(row)
            else:
                clusters.append([row])

        promoted: list[str] = []
        for cluster in clusters:
            if len(cluster) < self._cfg.cluster_min_items:
                continue
            if len({r["sender_hash"] for r in cluster}) < self._cfg.cluster_min_senders:
                continue
            if closed_before is not None and cluster[-1]["ts"] > closed_before.timestamp():
                continue
            first = datetime.fromtimestamp(cluster[0]["ts"], self._cfg.tz)
            day = (first - timedelta(hours=self._cfg.day_cutoff_hour)).date()
            folder = f"{day:%Y-%m-%d} Unbenannt"
            for row in cluster:
                self._publish(row["id"], f"{day:%Y}/{folder}", target="inbox")
            promoted.append(folder)
            log.info("Cluster %s: %d items promoted to inbox", folder, len(cluster))
        return promoted

    def purge_quarantine(self, now: datetime) -> int:
        """Delete quarantined files older than QUARANTINE_DAYS (data minimisation)."""
        limit = (now - timedelta(days=self._cfg.quarantine_days)).timestamp()
        purged = 0
        for row in self._db.by_state(State.QUARANTINED):
            # updated_at = when it entered quarantine (held items enter late)
            if row["updated_at"] >= limit:
                continue
            if row["companion_path"]:
                Path(row["companion_path"]).unlink(missing_ok=True)
            if row["staged_path"]:
                staged = Path(row["staged_path"])
                staged.unlink(missing_ok=True)
                _remove_if_empty(staged.parent, stop=self._cfg.staging_dir)
            self._db.update(row["id"], state=State.PURGED, staged_path=None, companion_path=None)
            purged += 1
        return purged

    def quarantined(self) -> list:
        """Quarantined and held items, oldest first, for review."""
        rows = self._db.by_state(State.QUARANTINED) + self._db.by_state(State.HELD)
        return sorted(rows, key=lambda r: (r["msg_ts"], r["seq"]))


def _discard(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _remove_if_empty(directory: Path, stop: Path) -> None:
    if directory != stop and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
