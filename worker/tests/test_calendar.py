"""Calendar loading: Konzertmeister title cleanup, exclusion, de-duplication."""

from datetime import timedelta
from pathlib import Path

from conftest import TZ, at

from gruppix.config import DEFAULT_SIDE_EVENTS, default_title_strip
from gruppix.events import check_sources, load_calendar, sanitize
import re


def ics(path: Path, *events: tuple[str, str, str]) -> Path:
    body = "".join(
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nDTSTART;TZID=Europe/Berlin:{start}\r\n"
        f"DURATION:PT3H\r\nEND:VEVENT\r\n"
        for uid, title, start in events
    )
    path.write_text(f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:t\r\n{body}END:VCALENDAR\r\n", encoding="utf-8")
    return path


def load(cfg, *sources: Path, exclude: str | None = None):
    return load_calendar(
        [str(s) for s in sources],
        cache_dir=cfg.cache_dir, tz=TZ, start=at(2025, 1, 1), end=at(2026, 12, 31),
        exclude=re.compile(exclude) if exclude else None,
        pre=timedelta(hours=2), post=timedelta(hours=48), late=timedelta(days=7),
        title_strip=re.compile(default_title_strip("Musikverein Beispielstadt")),
    )


EXCLUDE = r"(?i)((musik|gesamt|satz|theater|anspiel)probe|\bprobe\b|beerdigung|sitzung|vorstand)"


def test_konzertmeister_titles_are_cleaned_and_excluded(cfg, tmp_path):
    km = ics(
        tmp_path / "km.ics",
        ("k1", "(+) Kirchenkonzert (Musikverein Beispielstadt)", "20251109T170000"),
        ("k2", "(-) Auftritt Wöschbach (Musikverein Beispielstadt)", "20260509T140000"),
        ("k3", "(?) Maifest (Musikverein Beispielstadt)", "20260501T100000"),
        ("k4", "Theaterprobe (Musikverein Beispielstadt)", "20251014T200000"),
        ("k5", "(+) Beerdigung X (Musikverein Beispielstadt)", "20251024T140000"),
        ("k6", "Generalprobe (Halle)", "20250328T190000"),
    )
    titles = [e.title for e in load(cfg, km, exclude=EXCLUDE).events]
    assert titles == ["Generalprobe (Halle)", "Kirchenkonzert", "Maifest", "Auftritt Wöschbach"]


def test_first_source_wins_on_duplicates(cfg, tmp_path):
    google = ics(tmp_path / "g.ics", ("g1", "Sommerfest", "20260711T140000"))
    km = ics(tmp_path / "k.ics", ("k1", "(+) Auftritt Sommerfest (Musikverein Beispielstadt)", "20260711T150000"))
    assert [e.title for e in load(cfg, google, km).events] == ["Sommerfest"]


def test_html_instead_of_ics_is_reported(tmp_path):
    page = tmp_path / "embed.html"
    page.write_text("<!doctype html><html></html>")
    assert check_sources([str(page)]) == ["Kalenderquelle #1 liefert keinen Kalender (iCal) mehr"]


def test_sanitize_folder_names():
    assert sanitize('Grillabend Jugend / Helfer: "2026"') == "Grillabend Jugend - Helfer- -2026"
    assert sanitize("   ") == "Termin"


# --- all-day entries, build-up/tear-down --------------------------------------------

def raw_ics(path: Path, *vevents: str) -> Path:
    body = "".join(f"BEGIN:VEVENT\r\n{v.strip()}\r\nEND:VEVENT\r\n" for v in vevents)
    path.write_text(f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:t\r\n{body}END:VCALENDAR\r\n", encoding="utf-8")
    return path


def all_day(uid: str, title: str, first: str, after_last: str) -> str:
    return f"UID:{uid}\r\nSUMMARY:{title}\r\nDTSTART;VALUE=DATE:{first}\r\nDTEND;VALUE=DATE:{after_last}"


def timed(uid: str, title: str, start: str, end: str) -> str:
    return f"UID:{uid}\r\nSUMMARY:{title}\r\nDTSTART;TZID=Europe/Berlin:{start}\r\nDTEND;TZID=Europe/Berlin:{end}"


def load_shaped(cfg, *sources: Path):
    return load_calendar(
        [str(s) for s in sources],
        cache_dir=cfg.cache_dir, tz=TZ, start=at(2025, 1, 1), end=at(2026, 12, 31),
        exclude=None, pre=timedelta(hours=2), post=timedelta(hours=48), late=timedelta(days=7),
        title_strip=re.compile(default_title_strip("Musikverein Beispielstadt")), day_cutoff_hour=5, all_day_max_days=4,
        side=re.compile(DEFAULT_SIDE_EVENTS), side_gap=timedelta(hours=24),
    )


def test_build_up_fest_tear_down_is_one_folder(cfg, tmp_path):
    cal = load_shaped(cfg, raw_ics(
        tmp_path / "k.ics",
        timed("a", "Aufbau Heckenfest", "20260710T140000", "20260710T200000"),
        all_day("h", "Heckenfest", "20260711", "20260712"),
        timed("b", "Abbau", "20260712T100000", "20260712T140000"),
    ))
    assert [e.folder_name() for e in cal.events] == ["2026-07-11 Heckenfest"]
    for moment, late in [(at(2026, 7, 10, 16), False), (at(2026, 7, 11, 15), False), (at(2026, 7, 12, 2), False),
                         (at(2026, 7, 12, 19), False), (at(2026, 7, 13, 20), False), (at(2026, 7, 15, 20), True)]:
        assert cal.match(moment, late=late).title == "Heckenfest", moment


def test_side_event_without_neighbour_keeps_its_folder(cfg, tmp_path):
    cal = load_shaped(cfg, raw_ics(tmp_path / "k.ics", timed("a", "Bühnenaufbau", "20260301T090000", "20260301T120000")))
    assert [e.folder_name() for e in cal.events] == ["2026-03-01 Bühnenaufbau"]


def test_all_day_runs_from_cutoff_to_cutoff_and_long_entries_are_ignored(cfg, tmp_path):
    cal = load_shaped(cfg, raw_ics(
        tmp_path / "g.ics",
        all_day("f", "Maifest", "20260501", "20260502"),
        all_day("s", "Sommerferien", "20260730", "20260912"),
    ))
    assert [e.title for e in cal.events] == ["Maifest"]
    fest = cal.events[0]
    assert (fest.start, fest.end) == (at(2026, 5, 1, 5), at(2026, 5, 2, 5))
    assert fest.folder_name() == "2026-05-01 Maifest"
    assert cal.match(at(2026, 4, 30, 23)) is None          # no longer the evening before
    assert cal.match(at(2026, 5, 2, 3)).title == "Maifest"  # party night still counts


def test_timed_entry_beats_all_day_duplicate_but_other_events_survive(cfg, tmp_path):
    google = raw_ics(tmp_path / "g.ics", all_day("g", "Heckenfest", "20260711", "20260712"))
    km = raw_ics(
        tmp_path / "k.ics",
        timed("k1", "(+) Heckenfest (Musikverein Beispielstadt)", "20260711T110000", "20260711T230000"),
        timed("k2", "Ständchen Müller", "20260711T090000", "20260711T100000"),
    )
    events = load_shaped(cfg, google, km).events
    assert [(e.title, e.all_day) for e in events] == [("Ständchen Müller", False), ("Heckenfest", False)]
