"""Network failures: readable messages, no tracebacks, waiting at start, outage follow-up.

Based on a real incident: WAHA was updated in Synology Container Manager, ended up
outside the compose network ("waha" not resolvable) for three days; after recreating
both containers the first jobs failed because Docker's DNS was not ready yet.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import replace
from datetime import timedelta

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError

from gruppix import checks, live, net
from gruppix.live import LiveIntake
from gruppix.service import Job, Service
from gruppix.sinks import LocalSink

from conftest import at


def _request(url: str) -> requests.PreparedRequest:
    return requests.Request("GET", url).prepare()


def dns_error(url: str) -> requests.ConnectionError:
    """Same nesting as requests produces: ConnectionError → MaxRetryError → NameResolutionError → gaierror."""
    host = requests.utils.urlparse(url).hostname
    gai = socket.gaierror(-3, "Temporary failure in name resolution")
    try:
        try:
            raise gai
        except socket.gaierror as cause:
            raise NameResolutionError(host, None, cause) from cause
    except NameResolutionError as nre:
        retry = MaxRetryError(None, url, reason=nre)
    return requests.ConnectionError(retry, request=_request(url))


def refused_error(url: str) -> requests.ConnectionError:
    try:
        try:
            raise ConnectionRefusedError(111, "Connection refused")
        except ConnectionRefusedError as cause:
            raise NewConnectionError(None, "Failed to establish a new connection") from cause
    except NewConnectionError as nce:
        retry = MaxRetryError(None, url, reason=nce)
    return requests.ConnectionError(retry, request=_request(url))


# --- classification -----------------------------------------------------------


def test_dns_failure_of_a_docker_service_names_the_network_fix():
    exc = dns_error("http://waha:3000/api/sessions")
    assert net.is_network_error(exc) and net.kind(exc) == net.DNS and net.host(exc) == "waha"
    text = net.describe(exc, "WAHA")
    assert text.startswith("WAHA nicht erreichbar: Name 'waha' nicht auflösbar (DNS)")
    assert "docker compose up -d --force-recreate" in text


def test_dns_failure_of_an_internet_host_points_to_dns_of_the_host():
    exc = dns_error("https://daten.example.de/remote.php/dav/files/fotobot/Fotos-Upload/")
    text = net.describe(exc, "Nextcloud")
    assert text.startswith("Nextcloud nicht erreichbar: Name 'daten.example.de' nicht auflösbar")
    assert net.describe(exc).startswith("Server nicht erreichbar")
    assert "Docker-Hosts prüfen" in text and "compose" not in text


def test_refused_and_timeout_are_told_apart():
    assert net.kind(refused_error("http://waha:3000/x")) == net.REFUSED
    assert "startet gerade" in net.describe(refused_error("http://waha:3000/x"), "WAHA")
    timeout = requests.ReadTimeout("read timed out", request=_request("http://waha:3000/x"))
    assert net.kind(timeout) == net.TIMEOUT
    assert net.describe(timeout, "WAHA").startswith("WAHA antwortet nicht (Zeitüberschreitung")
    assert not net.is_network_error(requests.HTTPError("500"))


def test_waha_check_reports_the_cause(monkeypatch, cfg):
    cfg = replace(cfg, waha_url="http://waha:3000", waha_api_key="k")

    def unreachable(url, **_):
        raise dns_error(url)

    monkeypatch.setattr(checks.requests, "get", unreachable)
    with pytest.raises(checks.WahaUnreachable, match="Name 'waha' nicht auflösbar"):
        checks.waha_problems(cfg)


# --- service ------------------------------------------------------------------


def test_network_failure_of_a_job_is_one_line_and_alerts_after_three(cfg, caplog):
    svc = Service(cfg)
    sent = []
    svc.notifier._send_alert = lambda kind, title, message: sent.append((kind, title, message))

    def drop():
        raise dns_error("https://daten.example.de/remote.php/dav/files/fotobot/Fotos-Upload/")

    job = Job("import-drop", drop, at(2026, 11, 7, 17), every=timedelta(minutes=15))
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        svc._run_job(job)
    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.WARNING]
    assert all(r.exc_info is None for r in caplog.records)          # no traceback
    assert "Nextcloud nicht erreichbar: Name 'daten.example.de'" in caplog.records[0].getMessage()
    assert sent == []

    svc._run_job(job)
    mine = [r for r in caplog.records if r.name == "gruppix.service"]
    assert mine[-1].levelno == logging.ERROR and mine[-1].exc_info is None
    assert [kind for kind, *_ in sent] == ["problem"]

    job.run = lambda: None
    svc._run_job(job)
    assert [kind for kind, *_ in sent] == ["problem", "resolved"] and job.failures == 0


def test_waha_outage_recovery_names_the_period_to_catch_up(monkeypatch, cfg):
    svc = Service(replace(cfg, waha_url="http://waha:3000", waha_api_key="k", live_ingest=True))
    sent = []
    svc.notifier._send_alert = lambda kind, title, message: sent.append((kind, title, message))

    def down(_cfg):
        raise checks.WahaUnreachable("WAHA nicht erreichbar: Name 'waha' nicht auflösbar (DNS)")

    monkeypatch.setattr(checks, "waha_problems", down)
    for _ in range(3):
        svc._waha_check()
    assert [kind for kind, *_ in sent] == ["problem"] and svc.waha_down_since is not None

    monkeypatch.setattr(checks, "waha_problems", lambda _cfg: [])
    svc._waha_check()
    kind, title, message = sent[-1]
    assert (kind, title) == ("resolved", "Behoben: WhatsApp-Verbindung")
    assert "nicht erreichbar" in message and "Import" in message
    assert svc.waha_misses == 0 and svc.waha_down_since is None

    svc._waha_check()                       # nothing new while it stays fine
    assert len(sent) == 2


def test_short_waha_hiccup_is_neither_alerted_nor_resolved(monkeypatch, cfg):
    svc = Service(replace(cfg, waha_url="http://waha:3000", waha_api_key="k"))
    sent = []
    svc.notifier._send_alert = lambda *a: sent.append(a)

    def down(_cfg):
        raise checks.WahaUnreachable("x")

    monkeypatch.setattr(checks, "waha_problems", down)
    svc._waha_check()
    monkeypatch.setattr(checks, "waha_problems", lambda _cfg: [])
    svc._waha_check()
    assert sent == [] and svc.waha_down_since is None


def test_open_problem_is_repeated_once_a_day(cfg):
    svc = Service(cfg)
    sent = []
    svc.notifier._send_alert = lambda kind, title, message: sent.append(title)
    now = {"t": at(2026, 11, 4, 18)}
    svc.notifier._now = lambda: now["t"]

    svc.notifier.alert("waha", "WhatsApp-Verbindung", "WAHA nicht erreichbar")
    now["t"] += timedelta(hours=23)
    assert svc.notifier.remind() == []
    now["t"] += timedelta(hours=1)
    assert svc.notifier.remind() == ["waha"]
    assert svc.notifier.remind() == []                 # not again right away
    now["t"] += timedelta(hours=24)
    assert svc.notifier.remind() == ["waha"]
    assert sent == ["WhatsApp-Verbindung", "Weiterhin offen: WhatsApp-Verbindung",
                    "Weiterhin offen: WhatsApp-Verbindung"]

    svc.notifier.resolve("waha", "WhatsApp-Verbindung")
    now["t"] += timedelta(days=2)
    assert svc.notifier.remind() == []


def test_start_waits_until_the_hosts_resolve(monkeypatch, cfg):
    svc = Service(replace(cfg, waha_url="http://waha:3000",
                          nc_webdav_url="https://daten.example.de/remote.php/dav/files/fotobot"))
    calls = {"n": 0}

    def resolves(name):
        calls["n"] += 1
        return calls["n"] > 4                      # DNS answers after a few polls

    monkeypatch.setattr(net, "resolves", resolves)
    assert svc.await_network(limit=timedelta(seconds=5), poll=0) is True
    assert svc.heartbeat is not None                # waiting does not look like a stalled loop

    monkeypatch.setattr(net, "resolves", lambda name: False)
    started = time.monotonic()
    assert svc.await_network(limit=timedelta(seconds=0.2), poll=0.05) is False
    assert time.monotonic() - started < 2


# --- live intake --------------------------------------------------------------


def _queued_media(intake: LiveIntake, age: timedelta) -> os.PathLike:
    intake.enqueue([{"kind": "media", "msg_id": "ABC", "suffix": ".jpg",
                     "url": "http://waha:3000/api/files/default/ABC.jpg", "session": "default"}])
    path = intake._queued()[0]
    stamp = int((time.time() - age.total_seconds()) * 1_000_000)
    aged = path.with_name(f"{stamp:017d}_{path.name.split('_', 1)[1]}")
    path.rename(aged)
    return aged


def test_live_download_waits_out_a_waha_outage(monkeypatch, cfg, tmp_path):
    cfg = replace(cfg, waha_url="http://waha:3000", waha_api_key="k")
    intake = LiveIntake(cfg, LocalSink(tmp_path / "nc"))

    def unreachable(url, **_):
        raise dns_error(url)

    monkeypatch.setattr(live.requests, "get", unreachable)
    path = _queued_media(intake, timedelta(minutes=5))
    for _ in range(live.MAX_ATTEMPTS + 2):          # WAHA restarting: no attempt used up
        intake._download(path)
    assert path.exists() and "attempts" not in live._read_json(path)

    path.unlink()
    path = _queued_media(intake, timedelta(minutes=55))   # WAHA is about to drop the file
    intake._download(path)
    task = live._read_json(path)
    assert task["attempts"] == 1 and "nicht auflösbar" in task["error"]
