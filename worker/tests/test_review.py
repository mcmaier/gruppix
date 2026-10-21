"""Review pages: duplicate pairs and the quarantine, and the commands they hand out."""

import json

import pytest
from conftest import at, event, published
from test_assignment import _item, _photo

from gruppix import review
from gruppix.db import State
from gruppix.sinks import LocalSink

X = event("x", "Probe-Konzert", at(2026, 7, 11, 14), at(2026, 7, 11, 23))


def _two_copies(pipe, db, tmp_path, seed, sid):
    """An HD and a standard copy of one photo, both published (as by an older import)."""
    hd, sd = f"export:chat/HD{sid}.jpg", f"export:chat/SD{sid}.jpg"
    pipe.ingest(_item("export", f"chat/HD{sid}.jpg", at(2026, 7, 11, 16), _photo(tmp_path, f"h{sid}.jpg", seed, size=(4000, 3000))))
    db.update(hd, phash="0" * 16)                      # hide it from the check during ingest …
    pipe.ingest(_item("export", f"chat/SD{sid}.jpg", at(2026, 7, 11, 16), _photo(tmp_path, f"s{sid}.jpg", seed)))
    pipe.pin(hd)
    pipe.pin(sd)
    db.update(hd, phash=db.get(sd)["phash"])           # … then restore
    return hd, sd


def test_duplicates_page_and_delete(cfg, db, make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(X)
    hd, sd = _two_copies(pipe, db, tmp_path, 9, 1)
    assert len(published(gallery)) == 2

    stamp, pairs = review.duplicates_report(cfg)
    assert [(n, k["id"], e["id"]) for n, k, e, _ in pairs] == [(1, hd, sd)]
    page = (cfg.data_dir / "duplicates.html").read_text(encoding="utf-8")
    assert page.count("data:image/jpeg;base64,") == 2 and stamp in page

    with pytest.raises(review.ReportError):
        review.delete_duplicates(cfg, LocalSink(gallery), "20000101-000000", set())   # other list
    removed, kept, unchanged = review.delete_duplicates(cfg, LocalSink(gallery), stamp, set())
    assert (removed, kept, unchanged) == (1, 0, 0)
    assert db.get(sd)["state"] == State.DUPLICATE and db.get(sd)["archive_path"]   # original stays
    assert len(published(gallery)) == 1
    assert not (cfg.data_dir / "duplicates.json").exists()


def test_keep_both_is_remembered(cfg, db, make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(X)
    _two_copies(pipe, db, tmp_path, 9, 1)
    stamp, _ = review.duplicates_report(cfg)
    with pytest.raises(review.ReportError):
        review.delete_duplicates(cfg, LocalSink(gallery), stamp, {2})             # no pair 2
    assert review.delete_duplicates(cfg, LocalSink(gallery), stamp, {1}) == (0, 1, 0)
    assert review.duplicates_report(cfg)[1] == []
    assert len(published(gallery)) == 2


def test_quarantine_page_and_release(cfg, db, make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(X)
    for n in range(2):                                    # outside every event
        pipe.ingest(_item("waha", f"Q{n}", at(2026, 7, 20, 12, n), _photo(tmp_path, f"q{n}.jpg", 20 + n)))
    stamp, items = review.quarantine_report(cfg)
    assert [n for n, _ in items] == [1, 2]
    page = (cfg.data_dir / "quarantine.html").read_text(encoding="utf-8")
    assert "Montag, 20.07.2026" in page and page.count("data:image/jpeg;base64,") == 2

    assert review.release_from_sheet(cfg, LocalSink(gallery), stamp, {2}) == {2: State.UPLOADED}
    assert db.get("waha:Q1")["state"] == State.UPLOADED
    assert db.get("waha:Q0")["state"] == State.QUARANTINED
    with pytest.raises(review.ReportError):
        review.release_from_sheet(cfg, LocalSink(gallery), stamp, {3})


def test_refresh_counts_both(cfg, db, make_pipeline, tmp_path):
    pipe = make_pipeline(X)
    _two_copies(pipe, db, tmp_path, 9, 1)
    pipe.ingest(_item("waha", "Q9", at(2026, 7, 20, 12), _photo(tmp_path, "q9.jpg", 30)))
    assert review.refresh(cfg) == review.Summary(duplicates=1, quarantine=1)


def test_cli_commands(monkeypatch, cfg, db, make_pipeline, tmp_path, gallery, capsys):
    from gruppix import cli

    monkeypatch.setattr(cli, "_gallery", lambda _cfg, _args: LocalSink(gallery))
    pipe = make_pipeline(X)
    _two_copies(pipe, db, tmp_path, 9, 1)
    pipe.ingest(_item("waha", "Q1", at(2026, 7, 20, 12), _photo(tmp_path, "q1.jpg", 30)))

    assert cli.main(["find-duplicates"]) == 0
    stamp = json.loads((cfg.data_dir / "duplicates.json").read_text())["stamp"]
    assert f"find-duplicates --delete {stamp}" in capsys.readouterr().out
    assert cli.main(["find-duplicates", "--keep-both", "1"]) == 1          # only with --delete
    assert cli.main(["find-duplicates", "--delete", stamp]) == 0
    assert "1 copies removed" in capsys.readouterr().out

    assert cli.main(["quarantine"]) == 0
    stamp = json.loads((cfg.data_dir / "quarantine.json").read_text())["stamp"]
    assert cli.main(["release", "--sheet", stamp, "1"]) == 0
    assert db.get("waha:Q1")["state"] == State.UPLOADED
    assert cli.main(["release", "--sheet", "19990101-000000", "1"]) == 1   # outdated page


def test_weekly_report_names_review_pages(cfg):
    from gruppix import checks

    text, data = checks.weekly_report(cfg, [], (50, 3))
    assert "50  mögliche Duplikate" in text and "data/worker/quarantine.html" in text
    assert data["review_duplicates"] == 50 and data["review_quarantine"] == 3
    assert "Zum Durchsehen" not in checks.weekly_report(cfg, [])[0]
