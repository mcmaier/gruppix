"""Image processing, export parsing, photographer folders, staging names, health."""

from pathlib import Path

import piexif
from conftest import TZ, at
from PIL import Image

from gruppix import media
from gruppix.ingest_export import ExportContent, parse_chat, select_period
from gruppix.pipeline import parse_drop_folder


def scan_data(path: Path) -> bytes:
    raw = path.read_bytes()
    return raw[raw.index(b"\xff\xda"):]  # compressed pixel data after SOS


def test_small_jpeg_is_not_recompressed_but_gps_removed(make_jpeg, tmp_path):
    src = make_jpeg(size=(1600, 1200), gps=True, exif_date="2026:07:11 20:00:00")
    dst = tmp_path / "out.jpg"
    media.process_image(src, dst, at(2026, 7, 11, 20), max_edge=2048, max_bytes=1_000_000, quality=85)
    assert scan_data(src) == scan_data(dst)
    assert piexif.load(str(dst))["GPS"] == {}


def test_large_image_is_resized(make_jpeg, tmp_path):
    src = make_jpeg(size=(4000, 3000))
    dst = tmp_path / "out.jpg"
    media.process_image(src, dst, at(2026, 7, 11, 20), max_edge=2048, max_bytes=1_000_000, quality=85)
    assert max(Image.open(dst).size) == 2048


def test_capture_time_is_read_from_exif(make_jpeg):
    src = make_jpeg(exif_date="2026:07:11 21:30:00")
    assert media.read_capture_time(src, "image", TZ) == at(2026, 7, 11, 21, 30)


def test_export_parser_android_ios_and_noise():
    files = {"IMG-20260711-WA0001.jpg": "IMG-20260711-WA0001.jpg",
             "00000012-PHOTO-2026-07-11-18-43-11.jpg": "00000012-PHOTO-2026-07-11-18-43-11.jpg"}
    lines = [
        "11.07.26, 09:00 - Nachrichten und Anrufe sind Ende-zu-Ende-verschlüsselt.",
        "11.07.26, 09:05 - Anna: Wer bringt Bier mit?",
        "11.07.26, 18:43 - Anna: IMG-20260711-WA0001.jpg (Datei angehängt)",
        "[11.07.26, 18:43:11] Ben: ‎<Anhang: 00000012-PHOTO-2026-07-11-18-43-11.jpg>",
        "12.07.26, 10:00 - Carla: <Medien ausgeschlossen>",
    ]
    messages = list(parse_chat(lines, files, TZ))
    assert [(m.sender, m.sent_at) for m in messages] == [
        ("Anna", at(2026, 7, 11, 18, 43)),
        ("Ben", at(2026, 7, 11, 18, 43, 11)),
    ]


def test_export_period_selection_is_inclusive_and_keeps_sequence():
    from datetime import date

    from gruppix.ingest_export import ExportMessage

    msgs = [ExportMessage(at(2026, 7, d, 23, 59), "A", f"f{d}.jpg", seq) for seq, d in enumerate((10, 11, 12, 13))]
    content = ExportContent("export:x", msgs, [])
    picked = select_period(content, date(2026, 7, 11), date(2026, 7, 12))
    assert [m.seq for m in picked.messages] == [1, 2]
    assert [m.seq for m in select_period(content, None, date(2026, 7, 10)).messages] == [0]
    assert select_period(content, None, None) is content


def test_photographer_folder_names():
    from datetime import date

    assert parse_drop_folder("2026-07-11 Sommerfest") == (date(2026, 7, 11), "2026-07-11 Sommerfest")
    assert parse_drop_folder("11.07.2026 Sommerfest") == (date(2026, 7, 11), "2026-07-11 Sommerfest")
    assert parse_drop_folder("Konzert Fotos") == (None, "Konzert Fotos")
    assert parse_drop_folder(None) == (None, None)


def test_staging_names_are_readable(make_pipeline, send, db):
    pipe = make_pipeline()
    send(pipe, at(2026, 7, 15, 12))
    name = Path(pipe.quarantined()[0]["staged_path"])
    assert name.parent.name == "2026-07"
    assert name.name.startswith("2026-07-15_120000_")


def test_health_separates_worker_faults_from_warnings(cfg):
    from gruppix.service import Service

    svc = Service(cfg)
    svc.heartbeat = at(2100, 1, 1)  # loop "alive"
    svc.notifier.alert("waha", "WhatsApp-Verbindung", "WAHA nicht erreichbar")
    code, body = svc.health()
    assert code == 200 and body["status"] == "warning"

    svc.notifier.alert("job:retry-uploads", "Upload", "HTTP 401")
    code, body = svc.health()
    assert code == 503 and body["problems"] == ["HTTP 401"]


def test_cli_import_export_applies_period(monkeypatch, cfg, tmp_path):
    """Runs the CLI path up to the calendar, which is stubbed out."""
    import argparse
    from datetime import date

    from gruppix import cli
    from gruppix.ingest_export import ExportMessage

    msgs = [ExportMessage(at(2026, 7, d, 12), "A", f"f{d}.jpg", d) for d in (10, 11, 12)]
    monkeypatch.setattr(cli, "read_export", lambda *a: ExportContent("export:x", msgs, []))
    seen = {}

    class Stop(Exception):
        pass

    def fake_calendar(_cfg, start, end):
        seen["range"] = (start, end)
        raise Stop

    monkeypatch.setattr(cli.jobs, "build_calendar", fake_calendar)
    args = argparse.Namespace(zip=tmp_path / "x.zip", date_order="dmy", since=date(2026, 7, 11),
                              until=date(2026, 7, 11), no_clusters=True, dry_run=None)
    try:
        cli.cmd_import_export(cfg, args)
    except Stop:
        pass
    start, end = seen["range"]
    assert start < at(2026, 7, 11, 12) < end < at(2026, 7, 13)

    args.since, args.until = date(2026, 8, 1), None
    assert cli.cmd_import_export(cfg, args) == 1          # nothing in period
