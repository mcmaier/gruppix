"""SQLite persistence: media state machine and frozen event folder names."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS media (
    id            TEXT PRIMARY KEY,           -- "<source>:<source_id>"
    source        TEXT NOT NULL,              -- export | waha | drop
    chat_id       TEXT,
    sender_hash   TEXT NOT NULL,              -- salted HMAC, never the raw sender
    msg_ts        INTEGER NOT NULL,           -- message time (UTC epoch)
    seq           INTEGER NOT NULL DEFAULT 0, -- order within the same second (export)
    ts            INTEGER NOT NULL,           -- sort time: capture time if known, else msg_ts
    ts_source     TEXT NOT NULL DEFAULT 'message' CHECK (ts_source IN ('metadata', 'message', 'folder')),
    exif_ts       INTEGER,                    -- date written to EXIF (clamped into the event)
    kind          TEXT NOT NULL CHECK (kind IN ('image', 'video')),
    sha256        TEXT NOT NULL,
    staged_path   TEXT,
    event_key     TEXT,
    target        TEXT CHECK (target IN ('gallery', 'inbox')),
    remote_name   TEXT,
    remote_path   TEXT,
    archive_path  TEXT,
    pending_dir   TEXT,                       -- target folder while held / after hold expired
    hold_until    INTEGER,                    -- held: give up (-> quarantine) after this time
    pinned        INTEGER NOT NULL DEFAULT 0, -- marked for the gallery (📸), bypasses thresholds
    state         TEXT NOT NULL,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS media_sha   ON media (sha256);
CREATE INDEX IF NOT EXISTS media_state ON media (state, ts);

CREATE TABLE IF NOT EXISTS events (
    event_key  TEXT PRIMARY KEY,
    folder     TEXT NOT NULL,
    title      TEXT NOT NULL,
    start_ts   INTEGER NOT NULL
);

-- Events created by an admin command ("Ordner …") for days without a calendar entry
CREATE TABLE IF NOT EXISTS manual_events (
    event_key  TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    start_ts   INTEGER NOT NULL,
    end_ts     INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- One bot message per event when its gallery folder fills up (ANNOUNCE=on)
CREATE TABLE IF NOT EXISTS announcements (
    event_key  TEXT PRIMARY KEY,
    session    TEXT NOT NULL,
    due_at     INTEGER NOT NULL,
    sent_at    INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS optout (
    sender_hash TEXT PRIMARY KEY
);

-- Look-alike photos an admin confirmed as different (find-duplicates --keep-both)
CREATE TABLE IF NOT EXISTS distinct_pairs (
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    PRIMARY KEY (a, b)
);
"""


class State:
    STAGED = "staged"            # copied to staging, not yet decided
    HELD = "held"                # matched an event that has too few items so far
    QUARANTINED = "quarantined"  # outside every event window, purged after N days
    READY = "ready"              # processed and archived, waiting for upload
    UPLOADED = "uploaded"
    DUPLICATE = "duplicate"      # same content already known
    SKIPPED = "skipped"          # sender opted out, nothing stored
    REJECTED = "rejected"        # policy limit (e.g. video too long)
    FAILED = "failed"            # processing error, staged file kept for analysis
    PURGED = "purged"            # quarantine expired, file deleted
    REMOVED = "removed"          # deleted on request (reaction / revoke)

    LIVE = (STAGED, HELD, QUARANTINED, READY, UPLOADED)
    PUBLISHED = (READY, UPLOADED)


_COLUMNS = frozenset(
    {
        "id", "source", "chat_id", "sender_hash", "msg_ts", "seq", "ts", "ts_source", "exif_ts",
        "kind", "sha256",
        "staged_path", "event_key", "target", "remote_name", "remote_path",
        "archive_path", "pending_dir", "hold_until", "pinned", "phash", "pixels", "companion_path",
        "state", "error", "attempts", "created_at", "updated_at",
    }
)

# Columns added after the first release: (name, definition) for ALTER TABLE.
_MIGRATIONS = (
    ("pending_dir", "TEXT"),
    ("hold_until", "INTEGER"),
    ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("phash", "TEXT"),       # perceptual hash (images), finds the same photo as a different file
    ("pixels", "INTEGER"),   # width x height, tells a resized copy from a burst shot
    ("companion_path", "TEXT"),  # short clip shown as still photo: the clip (staged, then archived)
)


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit; wait for a concurrent writer (service + manual command)
        self._conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(media)")}
        for name, definition in _MIGRATIONS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE media ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self._conn.close()

    # --- media -------------------------------------------------------------

    def exists(self, media_id: str) -> bool:
        return self._conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() is not None

    def get(self, media_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if row is None:
            raise KeyError(media_id)
        return row

    def find_live_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.find_by_sha(sha256, State.LIVE)

    def find_by_sha(self, sha256: str, states: tuple[str, ...]) -> sqlite3.Row | None:
        placeholders = ",".join("?" * len(states))
        return self._conn.execute(
            f"SELECT * FROM media WHERE sha256 = ? AND state IN ({placeholders}) LIMIT 1",
            (sha256, *states),
        ).fetchone()

    def fingerprinted(self, msg_from: int, msg_to: int, sources: tuple[str, ...],
                      states: tuple[str, ...]) -> list[sqlite3.Row]:
        """Items with a perceptual hash sent within [msg_from, msg_to]."""
        src = ",".join("?" * len(sources))
        st = ",".join("?" * len(states))
        return self._conn.execute(
            f"SELECT id, source, state, phash, pixels FROM media WHERE phash IS NOT NULL "
            f"AND msg_ts BETWEEN ? AND ? AND source IN ({src}) AND state IN ({st})",
            (msg_from, msg_to, *sources, *states),
        ).fetchall()

    def missing_fingerprint(self) -> list[sqlite3.Row]:
        """Images stored before fingerprints existed (and whose file is still there)."""
        st = ",".join("?" * len(State.LIVE))
        return self._conn.execute(
            f"SELECT id, staged_path, archive_path FROM media WHERE kind = 'image' AND phash IS NULL "
            f"AND state IN ({st})",
            State.LIVE,
        ).fetchall()

    def insert(self, **fields: Any) -> None:
        self._check(fields)
        now = int(time.time())
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        columns = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        self._conn.execute(f"INSERT INTO media ({columns}) VALUES ({placeholders})", tuple(fields.values()))

    def update(self, media_id: str, **fields: Any) -> None:
        self._check(fields)
        fields["updated_at"] = int(time.time())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._conn.execute(f"UPDATE media SET {assignments} WHERE id = ?", (*fields.values(), media_id))

    def by_state(self, state: str) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM media WHERE state = ? ORDER BY ts", (state,)).fetchall()

    def find_by_staged_name(self, name: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM media WHERE staged_path LIKE ? ESCAPE '\\' LIMIT 1",
            ("%/" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),),
        ).fetchone()

    def count_for_event(self, event_key: str, states: tuple[str, ...]) -> int:
        placeholders = ",".join("?" * len(states))
        return self._conn.execute(
            f"SELECT COUNT(*) FROM media WHERE event_key = ? AND state IN ({placeholders})",
            (event_key, *states),
        ).fetchone()[0]

    def held_for_event(self, event_key: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM media WHERE event_key = ? AND state = ? ORDER BY ts, seq", (event_key, State.HELD)
        ).fetchall()

    def sender_rows(self, sender_hash: str, msg_from: int, msg_to: int) -> list[sqlite3.Row]:
        """Live items of one sender sent within [msg_from, msg_to], by send time."""
        placeholders = ",".join("?" * len(State.LIVE))
        return self._conn.execute(
            f"SELECT * FROM media WHERE sender_hash = ? AND msg_ts BETWEEN ? AND ? "
            f"AND state IN ({placeholders}) ORDER BY msg_ts, seq",
            (sender_hash, msg_from, msg_to, *State.LIVE),
        ).fetchall()

    def state_counts(self) -> Counter[str]:
        rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM media GROUP BY state").fetchall()
        return Counter({row["state"]: row["n"] for row in rows})

    def folder_counts(self) -> Counter[str]:
        rows = self._conn.execute(
            "SELECT remote_path FROM media WHERE state IN (?, ?) AND remote_path IS NOT NULL",
            (State.READY, State.UPLOADED),
        ).fetchall()
        counts: Counter[str] = Counter()
        for row in rows:
            folder = row["remote_path"].rsplit("/", 1)[0].removesuffix("/Videos")
            counts[folder] += 1
        return counts

    def activity_since(self, since: int) -> tuple[Counter[str], Counter[str]]:
        """States of items ingested since `since`, and uploads per folder."""
        states: Counter[str] = Counter()
        folders: Counter[str] = Counter()
        rows = self._conn.execute(
            "SELECT state, remote_path FROM media WHERE created_at >= ?", (since,)
        ).fetchall()
        for row in rows:
            states[row["state"]] += 1
            if row["state"] in (State.READY, State.UPLOADED) and row["remote_path"]:
                folders[row["remote_path"].rsplit("/", 1)[0].removesuffix("/Videos")] += 1
        return states, folders

    # --- opt-out -----------------------------------------------------------

    # --- announcements ------------------------------------------------------

    def event_info(self, event_key: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM events WHERE event_key = ?", (event_key,)).fetchone()

    def event_gallery_dir(self, event_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT pending_dir FROM media WHERE event_key = ? AND target = 'gallery' "
            "AND pending_dir IS NOT NULL LIMIT 1",
            (event_key,),
        ).fetchone()
        return row["pending_dir"] if row else None

    def add_announcement(self, event_key: str, session: str, due_at: int) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO announcements (event_key, session, due_at, created_at) VALUES (?, ?, ?, ?)",
            (event_key, session, due_at, int(time.time())),
        )
        return cursor.rowcount == 1

    def has_announcement(self, event_key: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM announcements WHERE event_key = ?", (event_key,)
        ).fetchone() is not None

    def due_announcements(self, now: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM announcements WHERE sent_at IS NULL AND due_at <= ? ORDER BY due_at", (now,)
        ).fetchall()

    def mark_announced(self, event_key: str, sent_at: int) -> None:
        self._conn.execute("UPDATE announcements SET sent_at = ? WHERE event_key = ?", (sent_at, event_key))

    def set_optout(self, sender_hashes: list[str], opted_out: bool) -> None:
        for h in sender_hashes:
            if opted_out:
                self._conn.execute("INSERT OR IGNORE INTO optout (sender_hash) VALUES (?)", (h,))
            else:
                self._conn.execute("DELETE FROM optout WHERE sender_hash = ?", (h,))

    def add_distinct_pair(self, a: str, b: str) -> None:
        self._conn.execute("INSERT OR IGNORE INTO distinct_pairs (a, b) VALUES (?, ?)", tuple(sorted((a, b))))

    def distinct_pairs(self) -> set[tuple[str, str]]:
        return {(r["a"], r["b"]) for r in self._conn.execute("SELECT a, b FROM distinct_pairs")}

    def is_opted_out(self, sender_hash: str) -> bool:
        return self._conn.execute("SELECT 1 FROM optout WHERE sender_hash = ?", (sender_hash,)).fetchone() is not None

    # --- events ------------------------------------------------------------

    def event_folder(self, event_key: str, folder: str, title: str, start_ts: int) -> str:
        """Return the frozen folder name for an event, creating it on first use."""
        row = self._conn.execute("SELECT folder FROM events WHERE event_key = ?", (event_key,)).fetchone()
        if row is not None:
            return row["folder"]
        self._conn.execute(
            "INSERT INTO events (event_key, folder, title, start_ts) VALUES (?, ?, ?, ?)",
            (event_key, folder, title, start_ts),
        )
        return folder

    def add_manual_event(self, event_key: str, title: str, start_ts: int, end_ts: int) -> bool:
        """Store an admin-created event; False if it already existed."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO manual_events (event_key, title, start_ts, end_ts, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_key, title, start_ts, end_ts, int(time.time())),
        )
        return cursor.rowcount == 1

    def manual_events(self, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM manual_events WHERE end_ts >= ? AND start_ts <= ? ORDER BY start_ts",
            (start_ts, end_ts),
        ).fetchall()

    @staticmethod
    def _check(fields: dict[str, Any]) -> None:
        unknown = set(fields) - _COLUMNS
        if unknown:
            raise ValueError(f"Unknown media columns: {sorted(unknown)}")
