"""Admin portal: login, CSRF check, pages, actions."""

import base64
import json
import threading
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest
from conftest import at, published
from test_assignment import _item, _photo
from test_review import X, _two_copies

from gruppix import admin, jobs
from gruppix.db import State
from gruppix.service import Service
from gruppix.sinks import LocalSink

PASSWORD = "correct horse battery"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}
POST = {**AUTH, "Host": "nas:8081", "Origin": "http://nas:8081"}


@pytest.fixture
def portal(cfg, gallery):
    cfg = replace(cfg, admin_password=PASSWORD)
    svc = Service(cfg)
    svc.nextcloud = LocalSink(gallery)
    svc.heartbeat = at(2100, 1, 1)
    p = admin.AdminPortal(cfg, svc, admin.LogBuffer())
    p.fail_delay = 0
    return p


def _form(**fields):
    from urllib.parse import urlencode

    return urlencode(fields, doseq=True).encode()


def test_login_required(portal):
    assert portal.dispatch("GET", "/", {}).code == 401
    wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    assert portal.dispatch("GET", "/", wrong).code == 401
    assert portal.dispatch("GET", "/", {"Authorization": "Basic %%%"}).code == 401
    page = portal.dispatch("GET", "/", AUTH)
    assert page.code == 200 and "Übersicht" in page.body.decode()
    assert "nonce-" in page.headers["Content-Security-Policy"]


def test_portal_off_without_password(cfg):
    p = admin.AdminPortal(cfg, Service(cfg))
    assert p.dispatch("GET", "/", AUTH).code == 401


def test_post_needs_same_origin(portal):
    body = _form(id="waha:none")
    assert portal.dispatch("POST", "/quarantine", AUTH, body).code == 403                       # no Origin
    evil = {**AUTH, "Host": "nas:8081", "Origin": "http://evil.example"}
    assert portal.dispatch("POST", "/quarantine", evil, body).code == 403
    proxied = {**AUTH, "Host": "localhost:8081", "X-Forwarded-Host": "gruppix.lan", "Referer": "https://gruppix.lan/quarantine"}
    assert portal.dispatch("POST", "/quarantine", proxied, body).code == 303


def test_quarantine_page_thumbnail_and_release(portal, db, make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(X)
    for n in range(2):
        pipe.ingest(_item("waha", f"Q{n}", at(2026, 7, 20, 12, n), _photo(tmp_path, f"q{n}.jpg", 20 + n)))

    page = portal.dispatch("GET", "/quarantine", AUTH).body.decode()
    assert "Montag, 20.07.2026" in page and page.count('name="id"') == 2
    thumb = portal.dispatch("GET", "/thumb?id=waha%3AQ0", AUTH)
    assert thumb.code == 200 and thumb.content_type == "image/jpeg" and thumb.body[:2] == b"\xff\xd8"
    assert portal.dispatch("GET", "/thumb?id=waha%3Anope", AUTH).code == 404

    answer = portal.dispatch("POST", "/quarantine", POST, _form(id=["waha:Q1", "waha:gone"]))
    assert answer.code == 303 and answer.headers["Location"] == "/quarantine"     # returns at once
    assert portal.queue.join()
    assert db.get("waha:Q1")["state"] == State.UPLOADED and db.get("waha:Q0")["state"] == State.QUARANTINED
    assert len(published(gallery)) == 1
    shown = portal.dispatch("GET", "/quarantine", AUTH).body.decode()
    assert "Erledigt: 1 ohne Termin nach Fotos-Eingang" in shown and "1 übersprungen" in shown
    assert shown.count('name="id"') == 1 and "ohne Termin → Fotos-Eingang" in shown
    status = json.loads(portal.dispatch("GET", "/queue", AUTH).body)
    assert status["active"] is False and status["done"] == status["total"] == 2


def test_duplicates_delete_keep_and_forged(portal, db, make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(X)
    hd1, sd1 = _two_copies(pipe, db, tmp_path, 9, 1)
    hd2, sd2 = _two_copies(pipe, db, tmp_path, 11, 2)
    assert "Prüfung starten" in portal.dispatch("GET", "/duplicates", AUTH).body.decode()
    _scan(portal)
    page = portal.dispatch("GET", "/duplicates", AUTH).body.decode()
    assert page.count('type="radio"') == 8 and "Erneut prüfen" in page

    form = _form(a1="drop", k1=hd1, e1=sd1, a2="keep", k2=hd2, e2=sd2,
                 a3="drop", k3=hd2, e3=hd1)                       # pair 3 is forged
    answer = portal.dispatch("POST", "/duplicates", POST, form)
    assert answer.headers["Location"] == "/duplicates" and portal.queue.join()
    assert portal.queue.status()["summary"] == "1 Kopien gelöscht, 1 Paare als verschieden gemerkt, 1 übersprungen (inzwischen geändert)"
    assert db.get(sd1)["state"] == State.DUPLICATE and db.get(sd2)["state"] == State.UPLOADED
    assert db.get(hd1)["state"] == State.UPLOADED
    assert "Keine doppelten Bilder (mehr) offen" in portal.dispatch("GET", "/duplicates", AUTH).body.decode()


def test_duplicates_keep_the_smaller_copy(portal, db, make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(X)
    hd, sd = _two_copies(pipe, db, tmp_path, 9, 1)
    _scan(portal)
    portal.dispatch("POST", "/duplicates", POST, _form(a1="swap", k1=hd, e1=sd))
    assert portal.queue.join()
    assert db.get(hd)["state"] == State.DUPLICATE and db.get(sd)["state"] == State.UPLOADED
    assert db.get(hd)["archive_path"]                        # the original stays in the archive
    assert len(published(gallery)) == 1


def _scan(portal):
    answer = portal.dispatch("POST", "/duplicates/scan", POST, b"")
    assert answer.code == 303
    for thread in threading.enumerate():
        if thread.name == "duplicate-scan":
            thread.join(timeout=30)
    assert not portal.scan.running and portal.scan.error is None


def test_scan_fingerprints_old_images_and_shows_progress(portal, db, make_pipeline, tmp_path):
    pipe = make_pipeline(X)
    hd, sd = _two_copies(pipe, db, tmp_path, 9, 1)
    db.update(sd, phash=None, pixels=None)                   # imported before fingerprints existed

    running = admin.DuplicateScan(running=True, started=at(2026, 10, 21, 12))
    running.begin(1, 300)
    running.done = 120
    portal.scan = running
    page = portal.dispatch("GET", "/duplicates", AUTH).body.decode()
    assert 'http-equiv="refresh"' in page and "Schritt 1 von 3" in page and "120 von 300" in page
    assert "noch unter einer Minute" in page

    portal.scan = admin.DuplicateScan()
    _scan(portal)
    assert db.get(sd)["phash"] is not None
    assert portal.scan.pairs == [(hd, sd, 0)]
    assert "Duplikate (letzte Prüfung)" in portal.dispatch("GET", "/", AUTH).body.decode()


def test_queue_waits_for_a_running_job_and_ignores_double_clicks(portal, db, make_pipeline, tmp_path):
    import time

    pipe = make_pipeline(X)
    pipe.ingest(_item("waha", "Q9", at(2026, 7, 20, 12), _photo(tmp_path, "q9.jpg", 30)))
    portal.service.running = ("import-drop", at(2026, 10, 21, 10))
    portal.queue.retry_delay = 0.05
    with jobs.pipeline_lock(portal.cfg):                       # a long import holds the lock
        assert portal.dispatch("POST", "/quarantine", POST, _form(id="waha:Q9")).code == 303
        assert portal.queue.submit([admin.Task("release", "waha:Q9")]) == 0     # second click
        time.sleep(0.3)
        assert db.get("waha:Q9")["state"] == State.QUARANTINED
        page = portal.dispatch("GET", "/quarantine", AUTH).body.decode()
        assert "wartet auf „Fotografen-Uploads abholen“" in page and "wird übernommen" in page
        assert 'data-active="1"' in page and 'name="id"' not in page
    assert portal.queue.join()
    assert db.get("waha:Q9")["state"] == State.UPLOADED


def test_overview_shows_counts_report_and_log(portal, make_pipeline, tmp_path):
    import logging

    pipe = make_pipeline(X)
    pipe.ingest(_item("waha", "Q9", at(2026, 7, 20, 12), _photo(tmp_path, "q9.jpg", 30)))
    (portal.cfg.data_dir / "weekly-report.txt").write_text("Wochenbericht Gruppix bis heute", encoding="utf-8")
    portal.logs.emit(logging.LogRecord("gruppix.x", logging.INFO, __file__, 1, "hello <b>", None, None))
    page = portal.dispatch("GET", "/", AUTH).body.decode()
    assert "Wochenbericht Gruppix bis heute" in page and "hello &lt;b&gt;" in page
    assert '<span class="num">1</span><span class="muted">Quarantäne</span>' in page


def test_served_over_http(portal):
    server = ThreadingHTTPServer(("127.0.0.1", 0), admin.handler(portal))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        with pytest.raises(urllib.request.HTTPError) as err:
            urllib.request.urlopen(url)
        assert err.value.code == 401
        with urllib.request.urlopen(urllib.request.Request(url, headers=AUTH)) as answer:
            assert answer.status == 200 and answer.headers["X-Frame-Options"] == "DENY"
    finally:
        server.shutdown()


def test_short_admin_password_is_rejected(monkeypatch, cfg):
    from gruppix.config import Config, ConfigError

    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_release_into_chosen_event_or_yearly_collection(portal, db, make_pipeline, tmp_path, gallery):
    pipe = make_pipeline(X)
    pipe.ingest(_item("export", "chat/IMG-1.jpg", at(2026, 7, 11, 16), _photo(tmp_path, "c1.jpg", 12)))
    pipe.pin("export:chat/IMG-1.jpg")                         # the event folder exists now
    for n in range(3):
        pipe.ingest(_item("waha", f"Q{n}", at(2026, 7, 20, 12, n), _photo(tmp_path, f"q{n}.jpg", 40 + n)))

    page = portal.dispatch("GET", "/quarantine", AUTH).body.decode()
    assert '<option value="event:x">11.07.2026 · Probe-Konzert</option>' in page and "„Allgemein“" in page

    portal.dispatch("POST", "/quarantine", POST, _form(id="waha:Q0", target="event:x"))
    portal.dispatch("POST", "/quarantine", POST, _form(id="waha:Q1", target="general"))
    portal.dispatch("POST", "/quarantine", POST, _form(id="waha:Q2", target="event:forged"))
    assert portal.queue.join()
    assert db.get("waha:Q0")["remote_path"].startswith("Fotos/2026/2026-07-11 Probe-Konzert/")
    assert db.get("waha:Q0")["event_key"] == "x"
    assert db.get("waha:Q1")["remote_path"].startswith("Fotos/2026/2026 Allgemein/")
    assert db.get("waha:Q2")["state"] == State.QUARANTINED   # unknown target: nothing done
    assert "1 in den Jahres-Sammelordner übernommen" in portal.queue.status()["summary"]
