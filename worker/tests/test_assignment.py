"""Event assignment: windows, late batches, minimum items per event, pinning."""

from datetime import datetime
from pathlib import Path

from conftest import at, event, published

from gruppix.db import State

FEST = event("fest", "Sommerfest", at(2026, 7, 11, 14), at(2026, 7, 11, 23))
GRILL = event("grill", "Grillabend", at(2026, 7, 25, 18), at(2026, 7, 25, 22))


def test_three_images_publish_together(make_pipeline, send, db, gallery):
    pipe = make_pipeline(FEST)
    assert send(pipe, at(2026, 7, 11, 15)) == State.HELD
    assert send(pipe, at(2026, 7, 11, 16), "B") == State.HELD
    assert published(gallery) == []

    assert send(pipe, at(2026, 7, 11, 17), "C") == State.UPLOADED
    assert db.count_for_event("fest", State.PUBLISHED) == 3
    # From now on every further image goes straight through.
    assert send(pipe, at(2026, 7, 11, 18)) == State.UPLOADED
    assert all(p.startswith("Fotos/2026/2026-07-11 Sommerfest/") for p in published(gallery))


def test_single_image_goes_to_quarantine_after_late_grace(make_pipeline, send, db):
    pipe = make_pipeline(GRILL)
    assert send(pipe, at(2026, 7, 25, 19)) == State.HELD
    assert pipe.settle_held(at(2026, 7, 27)) == 0       # still within late grace
    assert pipe.settle_held(at(2026, 8, 3)) == 1        # grace over
    assert db.count_for_event("grill", (State.QUARANTINED,)) == 1


def test_pin_publishes_single_image_also_from_quarantine(make_pipeline, send, db, gallery):
    pipe = make_pipeline(GRILL)
    send(pipe, at(2026, 7, 25, 19))
    pipe.settle_held(at(2026, 8, 3))
    row = pipe.quarantined()[0]
    assert pipe.pin(row["id"]) == State.UPLOADED
    files = published(gallery)
    assert len(files) == 1 and files[0].startswith("Fotos/2026/2026-07-25 Grillabend/")


def test_outside_any_event_is_quarantined(make_pipeline, send):
    pipe = make_pipeline(FEST)
    assert send(pipe, at(2026, 7, 20, 12)) == State.QUARANTINED


def test_late_batch_joins_previous_event_single_late_image_does_not(make_pipeline, send, db):
    pipe = make_pipeline(FEST)
    for _ in range(3):  # three images by one sender, same minute, three days later
        send(pipe, at(2026, 7, 14, 20), "B")
    assert db.count_for_event("fest", State.PUBLISHED) == 3

    assert send(pipe, at(2026, 7, 15, 12), "C") == State.QUARANTINED  # lone meme


def test_late_image_gets_exif_date_inside_event(make_pipeline, send, db):
    pipe = make_pipeline(FEST)
    for _ in range(3):
        send(pipe, at(2026, 7, 14, 20), "B")
    row = next(r for r in db.by_state(State.UPLOADED))
    assert row["exif_ts"] < FEST.end.timestamp() <= row["msg_ts"]


def test_capture_time_from_exif_wins_over_send_time(make_pipeline, send, db):
    pipe = make_pipeline(FEST)
    # Sent next morning as a document, taken during the event:
    for _ in range(3):
        send(pipe, at(2026, 7, 12, 10), exif_date="2026:07:11 21:30:00")
    row = db.by_state(State.UPLOADED)[0]
    assert row["ts_source"] == "metadata"
    assert row["ts"] == int(at(2026, 7, 11, 21, 30).timestamp())


def test_duplicate_is_detected(make_pipeline, db, make_jpeg):
    from gruppix.pipeline import IncomingMedia

    pipe = make_pipeline(FEST)
    path = make_jpeg()
    first = IncomingMedia("waha", "m1", "g", "A", at(2026, 7, 11, 15), path)
    again = IncomingMedia("waha", "m2", "g", "B", at(2026, 7, 11, 16), path)
    pipe.ingest(first)
    assert pipe.ingest(again) == State.DUPLICATE
    assert pipe.ingest(first) == "known"


# --- photographer upload folder ------------------------------------------------------

TRIP = event("trip", "Konzertreise", at(2026, 10, 10, 8), at(2026, 10, 10, 23))


def drop(pipe, make_jpeg, folder, exif_date=None):
    from gruppix.pipeline import IncomingMedia

    drop.n = getattr(drop, "n", 0) + 1
    item = IncomingMedia("drop", f"f{drop.n}", "drop", "drop", at(2026, 10, 15, 9), make_jpeg(exif_date=exif_date),
                         seq=drop.n, folder_hint=folder)
    return pipe.ingest(item)


def test_drop_folder_with_wrong_date_follows_camera_time(make_pipeline, make_jpeg, db, gallery):
    pipe = make_pipeline(TRIP)
    assert drop(pipe, make_jpeg, "2026-10-11 Konzertreise", "2026:10:10 15:00:00") == State.UPLOADED
    assert published(gallery)[0].startswith("Fotos/2026/2026-10-10 Konzertreise/")


def test_drop_folder_with_wrong_date_finds_similar_name_nearby(make_pipeline, make_jpeg, db, gallery):
    pipe = make_pipeline(TRIP)
    drop(pipe, make_jpeg, "11.10.2026 Konzertreise")          # no camera time
    assert published(gallery)[0].startswith("Fotos/2026/2026-10-10 Konzertreise/")
    row = db.by_state(State.UPLOADED)[0]
    assert TRIP.start.timestamp() <= row["ts"] < TRIP.end.timestamp()   # placed inside the trip


def test_drop_folder_other_name_same_day_joins_event(make_pipeline, make_jpeg, gallery):
    pipe = make_pipeline(TRIP)
    drop(pipe, make_jpeg, "2026-10-10 Musikerausflug")
    assert published(gallery)[0].startswith("Fotos/2026/2026-10-10 Konzertreise/")


def test_drop_folder_unrelated_name_next_day_gets_own_folder(make_pipeline, make_jpeg, gallery):
    pipe = make_pipeline(TRIP)
    drop(pipe, make_jpeg, "2026-10-11 Grillen")
    drop(pipe, make_jpeg, "2026-10-13 Konzertreise")          # similar name, but 3 days off
    assert sorted(p.rsplit("/", 1)[0] for p in published(gallery)) == [
        "Fotos/2026/2026-10-11 Grillen", "Fotos/2026/2026-10-13 Konzertreise"]


def test_removed_image_does_not_come_back_with_an_export(make_pipeline, db, make_jpeg, gallery):
    from gruppix.pipeline import IncomingMedia

    pipe = make_pipeline(FEST)
    path = make_jpeg()
    live = IncomingMedia("waha", "AC1", "g", "A", at(2026, 7, 11, 15), path)
    pipe.ingest(live)
    pipe.pin("waha:AC1")
    assert pipe.remove("waha:AC1") == State.REMOVED          # ❌ by the sender
    exported = IncomingMedia("export", "chat/IMG-1.jpg", "export:chat", "Anna", at(2026, 7, 11, 15), path)
    assert pipe.ingest(exported) == State.REMOVED
    assert published(gallery) == []


# --- same photo as a different file (export vs. live, HD + standard) -----------------

def _photo(tmp_path, name, seed, size=(1600, 1200), quality=85, noise=0):
    """A textured test photo; the same seed gives the same motif."""
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    img = Image.new("RGB", (400, 300), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = rnd.randrange(400), rnd.randrange(300)
        draw.ellipse((x, y, x + rnd.randrange(20, 120), y + rnd.randrange(20, 120)),
                     fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    if noise:
        px = img.load()
        nr = random.Random(noise)
        for _ in range(300):
            x, y = nr.randrange(400), nr.randrange(300)
            px[x, y] = (nr.randrange(256),) * 3
    path = tmp_path / name
    img.resize(size).save(path, "JPEG", quality=quality)
    return path


def _item(source, sid, when, path):
    from gruppix.pipeline import IncomingMedia

    return IncomingMedia(source, sid, "g", "A", when, path)


def test_export_copy_of_a_live_photo_is_a_duplicate(make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(FEST)
    live = _photo(tmp_path, "live.jpg", 1, quality=80)
    export = _photo(tmp_path, "export.jpg", 1, quality=92)          # same motif, other bytes
    pipe.ingest(_item("waha", "AC1", at(2026, 7, 11, 15, 0, 20), live))
    assert pipe.ingest(_item("export", "chat/IMG-1.jpg", at(2026, 7, 11, 15, 0), export)) == State.DUPLICATE
    # a different photo at the same time is not
    other = _photo(tmp_path, "other.jpg", 2)
    assert pipe.ingest(_item("export", "chat/IMG-2.jpg", at(2026, 7, 11, 15, 0), other)) != State.DUPLICATE


def test_hd_and_standard_copy_in_one_export_count_once(make_pipeline, tmp_path):
    pipe = make_pipeline(FEST)
    big = _photo(tmp_path, "hd.jpg", 3, size=(4000, 3000))
    small = _photo(tmp_path, "sd.jpg", 3, size=(1600, 1200))
    pipe.ingest(_item("export", "chat/IMG-HD.jpg", at(2026, 7, 11, 16), big))
    assert pipe.ingest(_item("export", "chat/IMG-SD.jpg", at(2026, 7, 11, 16), small)) == State.DUPLICATE


def test_burst_shots_of_one_source_both_stay(make_pipeline, tmp_path):
    pipe = make_pipeline(FEST)
    a = _photo(tmp_path, "a.jpg", 4, noise=1)
    b = _photo(tmp_path, "b.jpg", 4, noise=2)                         # nearly identical, same size
    pipe.ingest(_item("waha", "AC4", at(2026, 7, 11, 17), a))
    assert pipe.ingest(_item("waha", "AC5", at(2026, 7, 11, 17), b)) != State.DUPLICATE


def test_removed_photo_stays_removed_even_as_other_file(make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(FEST)
    pipe.ingest(_item("waha", "AC6", at(2026, 7, 11, 18), _photo(tmp_path, "l.jpg", 6, quality=80)))
    pipe.pin("waha:AC6")
    pipe.remove("waha:AC6")
    export = _photo(tmp_path, "e.jpg", 6, quality=95)
    assert pipe.ingest(_item("export", "chat/IMG-6.jpg", at(2026, 7, 11, 18), export)) == State.REMOVED
    assert published(gallery) == []


def test_far_apart_in_time_is_not_a_duplicate(make_pipeline, tmp_path):
    pipe = make_pipeline(FEST)
    pipe.ingest(_item("waha", "AC7", at(2026, 7, 11, 15), _photo(tmp_path, "x.jpg", 7)))
    later = _photo(tmp_path, "y.jpg", 7, quality=95)                  # reposted an hour later
    assert pipe.ingest(_item("export", "chat/IMG-7.jpg", at(2026, 7, 11, 16), later)) != State.DUPLICATE


def test_backfill_fingerprints(make_pipeline, tmp_path, db):
    pipe = make_pipeline(FEST)
    pipe.ingest(_item("waha", "AC8", at(2026, 7, 11, 15), _photo(tmp_path, "z.jpg", 8)))
    db.update("waha:AC8", phash=None, pixels=None)                  # as before this release
    assert pipe.backfill_fingerprints() == 1
    assert db.get("waha:AC8")["phash"] is not None


def test_find_existing_duplicates(make_pipeline, tmp_path, db):
    pipe = make_pipeline(event("x", "X", at(2026, 7, 11, 14), at(2026, 7, 11, 23)))
    pipe.ingest(_item("export", "chat/HD.jpg", at(2026, 7, 11, 16), _photo(tmp_path, "h.jpg", 9, size=(4000, 3000))))
    # simulate an older import that let the standard copy through
    db.update("export:chat/HD.jpg", phash="0" * 16)                  # hide it from the check …
    pipe.ingest(_item("export", "chat/SD.jpg", at(2026, 7, 11, 16), _photo(tmp_path, "s.jpg", 9)))
    pipe.pin("export:chat/HD.jpg")
    pipe.pin("export:chat/SD.jpg")                                    # both in the gallery
    db.update("export:chat/HD.jpg", phash=db.get("export:chat/SD.jpg")["phash"])  # … then restore
    pairs = pipe.similar_pairs()
    assert [(k["id"], e["id"]) for k, e, _ in pairs] == [("export:chat/HD.jpg", "export:chat/SD.jpg")]


# --- long videos -------------------------------------------------------------------

def _video(tmp_path, name, seconds):
    import subprocess

    path = tmp_path / name
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=red:s=320x240:d={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_long_video_goes_to_inbox_and_too_long_is_archived_only(cfg, db, gallery, tmp_path):
    from dataclasses import replace

    from gruppix.events import EventCalendar
    from gruppix.pipeline import Pipeline
    from gruppix.sinks import LocalSink

    cfg = replace(cfg, video_max_seconds=2, video_inbox_max_seconds=5, min_event_items=1)
    pipe = Pipeline(cfg, db, EventCalendar([FEST], cfg.window_pre, cfg.window_post, cfg.late_grace), LocalSink(gallery))

    assert pipe.ingest(_item("waha", "V1", at(2026, 7, 11, 15), _video(tmp_path, "a.mp4", 4))) == State.UPLOADED
    files = published(gallery)
    assert len(files) == 1 and files[0].startswith("Fotos-Eingang/2026/2026-07-11 Sommerfest/Videos/")
    assert "/_Eingang/2026/2026-07-11 Sommerfest/Videos/" in db.get("waha:V1")["archive_path"]

    assert pipe.ingest(_item("waha", "V2", at(2026, 7, 11, 16), _video(tmp_path, "b.mp4", 8))) == State.REJECTED
    row = db.get("waha:V2")
    assert len(published(gallery)) == 1                       # nothing new uploaded
    assert row["archive_path"] and Path(row["archive_path"]).exists()   # but the original is kept


def test_short_clip_becomes_a_still_photo_and_clip_is_archived(make_pipeline, db, gallery, tmp_path):
    pipe = make_pipeline(FEST)
    clip = _video(tmp_path, "live.mp4", 2)
    pipe.ingest(_item("waha", "LP1", at(2026, 7, 11, 15), clip))
    pipe.pin("waha:LP1")
    files = published(gallery)
    assert len(files) == 1 and files[0].endswith(".jpg") and "/Videos/" not in files[0]
    row = db.get("waha:LP1")
    assert row["kind"] == "image"
    assert Path(row["archive_path"]).suffix == ".jpg" and Path(row["companion_path"]).suffix == ".mp4"
    assert Path(row["companion_path"]).exists()
    pipe.remove("waha:LP1")                                   # ❌ removes still and clip
    assert not Path(row["archive_path"]).exists() and not Path(row["companion_path"]).exists()


def test_longer_clip_stays_a_video(make_pipeline, db, tmp_path):
    pipe = make_pipeline(FEST)
    pipe.ingest(_item("waha", "V5", at(2026, 7, 11, 15), _video(tmp_path, "v.mp4", 5)))
    assert db.get("waha:V5")["kind"] == "video"


# --- photographer upload folder: locked files ------------------------------------------

class _LockedDrop:
    """Upload folder whose first file Nextcloud refuses to delete (HTTP 423) until unlocked."""

    def __init__(self, files):
        self.files = dict(files)  # remote path -> local source file
        self.locked = {sorted(self.files)[0]}
        self.downloads = 0

    def list_tree(self, directory):
        from datetime import timezone

        from gruppix.sinks import RemoteFile

        return [RemoteFile(path=p, uploaded=datetime(2026, 10, 15, 7, tzinfo=timezone.utc), size=1)
                for p in sorted(self.files) if p.startswith(directory + "/")]

    def download(self, remote_path, local):
        import shutil

        self.downloads += 1
        shutil.copyfile(self.files[remote_path], local)

    def delete(self, remote_path):
        import requests

        if remote_path in self.locked:
            raise requests.HTTPError(f"DELETE {remote_path}: HTTP 423")
        self.files.pop(remote_path, None)


def test_locked_drop_file_does_not_block_the_others(make_pipeline, make_jpeg, cfg, gallery):
    from gruppix.ingest_drop import UNDELETED, ingest_drop

    pipe = make_pipeline(event("k", "Konzert", at(2026, 10, 14, 18), at(2026, 10, 14, 22)))
    folder = "Fotos-Upload/2026-10-14 Konzert"
    source = _LockedDrop({f"{folder}/DSC_{n}.jpg": make_jpeg() for n in range(3)})
    groups = {"2026-10-14 Konzert": source.list_tree("Fotos-Upload")}

    results = ingest_drop(source, groups, pipe, cfg)
    assert results[State.UPLOADED] == 3 and results[UNDELETED] == 1
    assert len(published(gallery)) == 3 and len(source.files) == 1

    source.locked.clear()                                    # Nextcloud released the lock
    results = ingest_drop(source, {"2026-10-14 Konzert": source.list_tree("Fotos-Upload")}, pipe, cfg)
    assert results[UNDELETED] == 0 and source.files == {}
    assert source.downloads == 3                             # the known file was not fetched again
