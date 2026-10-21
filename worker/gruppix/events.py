"""Event calendar: load ICS sources, expand recurrences, map timestamps to events."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_DEFAULT_DURATION = timedelta(hours=2)  # timed events without DTEND/DURATION


@dataclass(frozen=True)
class Event:
    key: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    priority: int  # index of the calendar source; lower wins on duplicates
    label: date | None = None  # folder date if start was moved (build-up merged in)

    def folder_name(self) -> str:
        return f"{self.label or self.start.date():%Y-%m-%d} {sanitize(self.title)}"


def sanitize(text: str, max_len: int = 80) -> str:
    """Make a title safe as a single path segment on Nextcloud and Linux/Windows file systems."""
    cleaned = _UNSAFE.sub("-", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return cleaned[:max_len].rstrip(" .-") or "Termin"


MANUAL_PREFIX = "manual:"  # event_key of events created by an admin command


class EventCalendar:
    """Answers: which event does a photo taken at time t belong to?"""

    def __init__(self, events: Iterable[Event], pre: timedelta, post: timedelta, late: timedelta) -> None:
        self._events = sorted(events, key=lambda e: e.start)
        self._pre = pre
        self._post = post
        self._late = max(late, post)

    @property
    def events(self) -> Sequence[Event]:
        return self._events

    def with_events(self, extra: Iterable[Event]) -> EventCalendar:
        """Copy with additional events (keys already present are not added twice)."""
        known = {e.key for e in self._events}
        added = [e for e in extra if e.key not in known]
        return EventCalendar([*self._events, *added], self._pre, self._post, self._late)

    def window(self, event: Event) -> tuple[datetime, datetime]:
        return event.start - self._pre, event.end + self._post

    def match(self, moment: datetime, *, late: bool = False) -> Event | None:
        """Find the event for a timestamp.

        ``late=True`` is used when only the send time is known: photos are
        usually posted after the event, so the post-window is extended to the
        late-grace period.
        """
        post = self._late if late else self._post
        candidates = [e for e in self._events if e.start - self._pre <= moment <= e.end + post]
        if not candidates:
            return None
        # Inside an event's actual duration that event wins; otherwise the
        # later event wins, i.e. B's pre-window beats A's post-window.
        core = [e for e in candidates if e.start <= moment <= e.end]
        return max(core or candidates, key=lambda e: e.start)


    def near_day(self, day: date, title: str, days: int = 1) -> Event | None:
        """Event with a similar name up to ``days`` before or after ``day``
        (photographer folder dated wrong); the closest one wins."""
        if not title.strip():
            return None
        def gap(e: Event) -> int:
            first, last = e.start.date(), max(e.end - timedelta(seconds=1), e.start).date()
            return 0 if first <= day <= last else min(abs((first - day).days), abs((last - day).days))
        hits = [e for e in self._events if gap(e) <= days and similar_titles(e.title, title)]
        return min(hits, key=gap) if hits else None

    def on_day(self, day: date, prefer: datetime | None = None) -> Event | None:
        """Event taking place on a given day (photographer folders named by date).

        With several events that day, the one whose time contains ``prefer``
        (the capture time) wins, otherwise the earliest.
        """
        hits = [e for e in self._events if e.start.date() <= day <= max(e.end - timedelta(seconds=1), e.start).date()]
        if not hits:
            return None
        if prefer is not None:
            for event in hits:
                if event.start - self._pre <= prefer <= event.end:
                    return event
        return hits[0]


def load_calendar(
    sources: Sequence[str],
    *,
    cache_dir: Path,
    tz: ZoneInfo,
    start: datetime,
    end: datetime,
    exclude: re.Pattern[str] | None,
    pre: timedelta,
    post: timedelta,
    late: timedelta,
    title_strip: re.Pattern[str] | None = None,
    day_cutoff_hour: int = 0,
    all_day_max_days: int | None = None,
    side: re.Pattern[str] | None = None,
    side_gap: timedelta = timedelta(hours=24),
) -> EventCalendar:
    events: list[Event] = []
    for priority, source in enumerate(sources):
        calendar = icalendar.Calendar.from_ical(_read_source(source, cache_dir, priority))
        for component in recurring_ical_events.of(calendar).between(start, end):
            event = _to_event(component, priority, tz)
            if event is None:
                continue
            if title_strip is not None:
                # e.g. Konzertmeister: "(+) Maifest (<CLUB_NAME>)" -> "Maifest"
                cleaned = re.sub(r"\s{2,}", " ", title_strip.sub("", event.title)).strip(" -–")
                event = replace(event, title=cleaned or event.title)
            if exclude is not None and exclude.search(event.title):
                continue
            events.append(event)
    kept = _deduplicate(events)
    kept = _shape_all_day(kept, day_cutoff_hour, all_day_max_days)
    kept = _merge_side_events(kept, side, side_gap)
    log.info("Calendar: %d events in range (%d before de-duplication)", len(kept), len(events))
    return EventCalendar(kept, pre, post, late)


def _read_source(source: str, cache_dir: Path, index: int) -> bytes:
    """Fetch an ICS feed; fall back to the last good copy if the feed is down.

    The URL is a secret (private feed), so it is never logged.
    """
    if source.startswith("webcal://"):
        source = "https://" + source.removeprefix("webcal://")
    if not source.startswith(("http://", "https://")):
        content = Path(source).read_bytes()
        _check_ical(content, index, source)
        return content

    cache = cache_dir / f"calendar-{index}.ics"
    try:
        response = requests.get(source, timeout=30)
        response.raise_for_status()
        _check_ical(response.content, index, source)
    except requests.RequestException as exc:
        if cache.exists():
            log.warning("Calendar source #%d unreachable (%s), using cached copy", index, type(exc).__name__)
            return cache.read_bytes()
        raise CalendarSourceError(f"Calendar source #{index + 1} unreachable and no cached copy") from exc
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(response.content)
    return response.content


def check_sources(sources: Sequence[str]) -> list[str]:
    """Fetch every source without cache fallback; return problems (daily check)."""
    problems: list[str] = []
    for index, source in enumerate(sources):
        url = "https://" + source.removeprefix("webcal://") if source.startswith("webcal://") else source
        try:
            if url.startswith(("http://", "https://")):
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                content = response.content
            else:
                content = Path(url).read_bytes()
            _check_ical(content, index, url)
        except (requests.RequestException, OSError) as exc:
            problems.append(f"Kalenderquelle #{index + 1} nicht erreichbar ({type(exc).__name__})")
        except CalendarSourceError:
            problems.append(f"Kalenderquelle #{index + 1} liefert keinen Kalender (iCal) mehr")
    return problems


class CalendarSourceError(RuntimeError):
    """A calendar source is unreachable or does not deliver iCalendar data."""


def _check_ical(content: bytes, index: int, source: str) -> None:
    """Reject HTML pages (e.g. a Google embed link) before parsing."""
    head = content[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"BEGIN:VCALENDAR"):
        return
    hint = ""
    if "calendar.google.com" in source and "/ical/" not in source:
        hint = (
            " This looks like a Google embed/share link. Use the iCal address instead: "
            "Google Calendar settings -> 'Integrate calendar' -> "
            "'Secret address in iCal format' (or the public iCal address), ending in basic.ics."
        )
    elif head[:15].lower().startswith((b"<!doctype", b"<html")):
        hint = " The URL returns a web page, not an iCalendar feed (.ics)."
    raise CalendarSourceError(f"Calendar source #{index + 1} does not deliver iCalendar data.{hint}")


def _to_event(component: icalendar.cal.Component, priority: int, tz: ZoneInfo) -> Event | None:
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return None
    title = str(component.get("SUMMARY", "")).strip() or "Termin"
    start, all_day = _as_datetime(component.decoded("DTSTART"), tz)
    if "DTEND" in component:
        end, _ = _as_datetime(component.decoded("DTEND"), tz)
    elif "DURATION" in component:
        end = start + component.decoded("DURATION")
    else:
        end = start + (timedelta(days=1) if all_day else _DEFAULT_DURATION)
    uid = str(component.get("UID", "")) or hashlib.sha1(f"{title}|{start.isoformat()}".encode()).hexdigest()
    return Event(
        key=f"{uid}@{start.isoformat()}",
        title=title,
        start=start,
        end=max(end, start),
        all_day=all_day,
        priority=priority,
    )


def _as_datetime(value: date | datetime, tz: ZoneInfo) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # floating time: interpret as local
            return value.replace(tzinfo=tz), False
        return value.astimezone(tz), False
    return datetime.combine(value, time.min, tzinfo=tz), True


def _deduplicate(events: list[Event]) -> list[Event]:
    """Drop events that appear in several sources (e.g. Google + Konzertmeister).

    Two events from different sources are the same if they are on the same day
    and overlap; with an all-day entry involved the titles must match too (an
    all-day "Heckenfest" overlaps everything that day). A timed entry beats an
    all-day one, otherwise the source listed first wins.
    """
    kept: list[Event] = []
    for event in sorted(events, key=lambda e: (e.all_day, e.priority, e.start)):
        duplicate = any(
            other.priority != event.priority
            and other.start.date() == event.start.date()
            and other.start < event.end
            and event.start < other.end
            and (not (event.all_day or other.all_day) or similar_titles(event.title, other.title))
            for other in kept
        )
        if not duplicate:
            kept.append(event)
    return kept


def similar_titles(a: str, b: str) -> bool:
    a, b = a.casefold().strip(), b.casefold().strip()
    return bool(a and b) and (a in b or b in a)


def _shape_all_day(events: list[Event], cutoff_hour: int, max_days: int | None) -> list[Event]:
    """All-day entries run from DAY_CUTOFF_HOUR to DAY_CUTOFF_HOUR the next
    morning (the party night belongs to the day); overly long ones (holidays,
    "Sommerferien") are dropped, they would swallow every photo."""
    shaped = []
    shift = timedelta(hours=cutoff_hour)
    for event in events:
        if not event.all_day:
            shaped.append(event)
            continue
        if max_days is not None and event.end - event.start > timedelta(days=max_days):
            log.info("Calendar: all-day entry '%s' longer than %d days ignored", event.title, max_days)
            continue
        shaped.append(replace(event, start=event.start + shift, end=event.end + shift,
                              label=event.label or event.start.date()))
    return shaped


def _merge_side_events(events: list[Event], side: re.Pattern[str] | None, gap: timedelta) -> list[Event]:
    """Build-up/tear-down ("Aufbau", "Abbau") right next to a main event become
    part of it: the main event is stretched over them and keeps its own date
    in the folder name. Side events without a neighbour stay on their own."""
    if side is None:
        return events
    sides = [e for e in events if side.search(e.title)]
    mains = [e for e in events if not side.search(e.title)]
    stretched = {m.key: m for m in mains}
    standalone = []
    for event in sides:
        def distance(main: Event) -> timedelta:
            return max(main.start - event.end, event.start - main.end, timedelta(0))
        near = [m for m in mains if distance(m) <= gap]
        if not near:
            standalone.append(event)
            continue
        main = min(near, key=distance)
        current = stretched[main.key]
        stretched[main.key] = replace(
            current,
            start=min(current.start, event.start),
            end=max(current.end, event.end),
            label=current.label or main.start.date(),
        )
        log.info("Calendar: '%s' merged into '%s'", event.title, main.title)
    return sorted([*stretched.values(), *standalone], key=lambda e: e.start)
