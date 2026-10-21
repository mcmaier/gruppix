"""Command line interface: python -m gruppix <command>."""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import jobs, review
from .config import Config, ConfigError
from .db import Database
from .events import CalendarSourceError
from .ingest_export import ingest_export, read_export, select_period
from .notify import Notifier
from .pipeline import Pipeline
from .service import Service
from .sinks import LocalSink, WebDavSink

log = logging.getLogger("gruppix")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        cfg = Config.from_env()
        if getattr(args, "dry_run", None):
            cfg = replace(cfg, data_dir=args.dry_run / "data", archive_dir=args.dry_run / "archive")
        return args.func(cfg, args)
    except (ConfigError, CalendarSourceError) as exc:
        log.error("%s", exc)
        return 2
    except jobs.Busy as exc:
        log.error("%s – try again when it has finished", exc)
        return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gruppix", description="Event photo archive worker")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run as a service: scheduler for all recurring jobs + health endpoint")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("notify-test", help="send a test message on every configured notification channel")
    p.set_defaults(func=cmd_notify_test)

    p = sub.add_parser("calendar", help="list events and their photo windows")
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD")
    p.set_defaults(func=cmd_calendar)

    p = sub.add_parser("import-export", help="import a WhatsApp chat export (ZIP with media)")
    p.add_argument("zip", type=Path)
    p.add_argument("--date-order", choices=("dmy", "mdy"), default="dmy", help="date format of the export")
    p.add_argument("--no-clusters", action="store_true", help="skip cluster detection after import")
    p.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="only attachments sent on or after this day")
    p.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="only attachments sent on or before this day")
    _dry_run(p)
    p.set_defaults(func=cmd_import_export)

    p = sub.add_parser("quarantine", help="list quarantined media and write the review page quarantine.html")
    p.add_argument("--csv", type=Path, metavar="FILE", help="write the list as CSV (semicolon, for Excel)")
    _dry_run(p)
    p.set_defaults(func=cmd_quarantine)

    p = sub.add_parser("release", help="move quarantined or held media into the gallery (like a 📸 reaction)")
    p.add_argument("files", nargs="*", metavar="FILE",
                   help="staging file name(s) as shown by 'quarantine', or numbers with --sheet")
    p.add_argument("--sheet", metavar="STAMP", help="numbers from quarantine.html (the page shows the command)")
    _dry_run(p)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("find-duplicates",
                       help="list photos stored twice (HD + standard, export + live), write duplicates.html")
    p.add_argument("--delete", metavar="STAMP",
                   help="remove the second copies of that list from Nextcloud (the page shows the command)")
    p.add_argument("--keep-both", type=_numbers, default=set(), metavar="N,N",
                   help="with --delete: pairs that are two different photos (not listed again)")
    _dry_run(p)
    p.set_defaults(func=cmd_find_duplicates)

    for name, func, text in (
        ("import-drop", cmd_import_drop, "collect photographer uploads now (the service does this every 15 min)"),
        ("promote-clusters", cmd_promote_clusters, "move bursts of unmatched photos to the admin inbox"),
        ("retry-uploads", cmd_retry_uploads, "retry pending uploads"),
        ("purge-quarantine", cmd_purge_quarantine, "delete expired quarantined files"),
        ("status", cmd_status, "show counts per state and per folder"),
        ("backfill-fingerprints", cmd_backfill_fingerprints,
         "fingerprint older images for duplicate detection (the service does this nightly)"),
    ):
        p = sub.add_parser(name, help=text)
        _dry_run(p)
        p.set_defaults(func=func)
    return parser


def _dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", type=Path, metavar="DIR",
        help="write database, archive and 'Nextcloud' below DIR instead of the real targets",
    )


def _numbers(text: str) -> set[int]:
    try:
        return {int(part) for part in text.replace(" ", ",").split(",") if part}
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected numbers like 3,7,12, got {text!r}") from exc


def _gallery(cfg: Config, args: argparse.Namespace) -> LocalSink | WebDavSink:
    if getattr(args, "dry_run", None):
        return LocalSink(args.dry_run / "nextcloud")
    return jobs.nextcloud(cfg)


# --- commands --------------------------------------------------------------


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    return Service(cfg).run()


def cmd_notify_test(cfg: Config, args: argparse.Namespace) -> int:
    results = Notifier(cfg).test()
    if not results:
        print("No notification channel configured (MQTT_HOST, SMTP_HOST/SMTP_TO, HEALTHCHECK_PING_URL)")
        return 1
    for channel, outcome in results.items():
        print(f"  {channel:<16} {outcome}")
    return 1 if any(o.startswith("FEHLER") for o in results.values()) else 0


def cmd_calendar(cfg: Config, args: argparse.Namespace) -> int:
    start = datetime.combine(args.start, time.min, tzinfo=cfg.tz)
    end = datetime.combine(args.end, time.max, tzinfo=cfg.tz)
    calendar = jobs.build_calendar(cfg, start, end)
    for event in calendar.events:
        win_start, win_end = calendar.window(event)
        print(f"{event.folder_name():<50}  window {win_start:%d.%m. %H:%M} – {win_end:%d.%m. %H:%M}")
    return 0


def cmd_import_export(cfg: Config, args: argparse.Namespace) -> int:
    content = read_export(args.zip, cfg.tz, args.date_order)
    if not content.messages:
        log.error("No media attachments found in %s", args.zip.name)
        return 1
    if args.since or args.until:
        if args.since and args.until and args.since > args.until:
            log.error("--since must not be after --until")
            return 2
        total = len(content.messages)
        content = select_period(content, args.since, args.until)
        log.info("Period %s – %s: %d of %d attachments",
                 args.since or "start", args.until or "end", len(content.messages), total)
        if not content.messages:
            log.error("No attachments in the selected period")
            return 1
    first = min(m.sent_at for m in content.messages)
    last = max(m.sent_at for m in content.messages)
    log.info("%d attachments from %s to %s", len(content.messages), f"{first:%d.%m.%Y}", f"{last:%d.%m.%Y}")
    if content.unreferenced:
        log.warning("%d media files in the ZIP are not referenced in the chat text", len(content.unreferenced))

    # Events that can claim a photo start at most late_grace (+ duration) before it.
    calendar = jobs.build_calendar(
        cfg, first - cfg.late_grace - timedelta(days=7), last + cfg.window_pre + timedelta(days=1)
    )
    with jobs.pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            pipeline = Pipeline(cfg, db, calendar, _gallery(cfg, args))
            results = ingest_export(args.zip, content, pipeline)
            # Events whose window has closed with too few items -> quarantine
            settled = pipeline.settle_held(datetime.now(cfg.tz))
            if settled:
                log.info("%d items of events with fewer than %d items moved to quarantine",
                         settled, cfg.min_event_items)
            clusters = [] if args.no_clusters else pipeline.promote_clusters()
            print("\nThis import (state right after ingest, before cluster detection):")
            _print_counts(results)
            print("\nDatabase total:")
            _print_counts(db.state_counts())
            if clusters:
                print("\nClusters without calendar entry (admin inbox):")
                for folder in clusters:
                    print(f"  {folder}")
            _print_folders(db)
        finally:
            db.close()
    return 0


def cmd_import_drop(cfg: Config, args: argparse.Namespace) -> int:
    results = jobs.import_drop(cfg, _gallery(cfg, args))
    if results is None:
        print("Nothing to import")
        return 0
    print("\nThis import:")
    _print_counts(results)
    return 0


def cmd_promote_clusters(cfg: Config, args: argparse.Namespace) -> int:
    for folder in jobs.promote_clusters(cfg, _gallery(cfg, args)):
        print(folder)
    return 0


def cmd_retry_uploads(cfg: Config, args: argparse.Namespace) -> int:
    pending = jobs.retry_uploads(cfg, _gallery(cfg, args))
    print(f"{pending} uploads still pending")
    return 1 if pending else 0


def cmd_purge_quarantine(cfg: Config, args: argparse.Namespace) -> int:
    print(f"{jobs.purge_quarantine(cfg)} quarantined files purged")
    return 0


def cmd_quarantine(cfg: Config, args: argparse.Namespace) -> int:
    stamp, items = review.quarantine_report(cfg)
    header = ("no", "sent", "state", "kind", "sender", "file")
    lines = [
        (
            str(n),
            f"{datetime.fromtimestamp(r['msg_ts'], cfg.tz):%Y-%m-%d %H:%M}",
            r["state"],
            r["kind"],
            r["sender_hash"][:6],  # same short code = same sender, never the name
            str(Path(r["staged_path"]).relative_to(cfg.data_dir)),
        )
        for n, r in items
    ]
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh, delimiter=";").writerows([header, *lines])
        print(f"{len(lines)} quarantined items written to {args.csv}")
    else:
        for line in lines:
            print("  ".join(line))
    if not items:
        print("Quarantine is empty")
        return 0
    print(f"\n{len(lines)} items (quarantined or held)")
    _print_sheet(cfg, review.QUARANTINE, f"release --sheet {stamp} 3,7,12")
    return 0


def cmd_release(cfg: Config, args: argparse.Namespace) -> int:
    if args.sheet:
        numbers = _numbers(",".join(args.files))
        try:
            results = review.release_from_sheet(cfg, _gallery(cfg, args), args.sheet, numbers)
        except review.ReportError as exc:
            log.error("%s", exc)
            return 1
        for n, state in results.items():
            print(f"  {n:>4}  {state}")
        return 0
    if not args.files:
        log.error("give staging file names or --sheet STAMP with numbers")
        return 1
    results = jobs.release(cfg, _gallery(cfg, args), args.files)
    for name, state in results.items():
        print(f"  {state:<12} {Path(name).name}")
    return 1 if any(s == "not found" for s in results.values()) else 0


def cmd_find_duplicates(cfg: Config, args: argparse.Namespace) -> int:
    if args.delete:
        try:
            removed, kept, unchanged = review.delete_duplicates(cfg, _gallery(cfg, args), args.delete, args.keep_both)
        except review.ReportError as exc:
            log.error("%s", exc)
            return 1
        print(f"{removed} copies removed from Nextcloud, {kept} pairs kept as different photos"
              + (f", {unchanged} unchanged (no longer in the gallery)" if unchanged else ""))
        return 0
    if args.keep_both:
        log.error("--keep-both only together with --delete")
        return 1
    stamp, pairs = review.duplicates_report(cfg)
    if not pairs:
        print("No duplicates found (run backfill-fingerprints first for older images)")
        return 0
    print(f"{len(pairs)} photos stored twice – the second one would be removed from Nextcloud:")
    for n, keep, extra, distance in pairs:
        print(f"  {n:>3}  keep    {keep['remote_path']}\n       delete  {extra['remote_path']}  ({distance} bits)\n")
    _print_sheet(cfg, review.DUPLICATES, f"find-duplicates --delete {stamp} [--keep-both 3,17]")
    return 0


def _print_sheet(cfg: Config, kind: str, command: str) -> None:
    print(f"Review page: {cfg.data_dir / (kind + '.html')}  (host: data/worker/{kind}.html, open via File Station)")
    print(f"then: {command}")


def cmd_backfill_fingerprints(cfg: Config, args: argparse.Namespace) -> int:
    print(f"{jobs.backfill_fingerprints(cfg)} images fingerprinted")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    try:
        print("Items per state:")
        _print_counts(db.state_counts())
        _print_folders(db)
    finally:
        db.close()
    return 0


def _print_counts(counts: dict[str, int]) -> None:
    for state, count in sorted(counts.items()):
        print(f"  {state:<12} {count:>5}")


def _print_folders(db: Database) -> None:
    folders = db.folder_counts()
    if folders:
        print("\nFolders:")
        for folder, count in sorted(folders.items()):
            print(f"  {count:>5}  {folder}")
