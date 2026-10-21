"""Runtime configuration, loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or invalid."""


def _str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _required(name: str) -> str:
    value = _str(name)
    if value is None:
        raise ConfigError(f"Missing required environment variable {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _list(name: str) -> tuple[str, ...]:
    raw = _str(name, "") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _hours(name: str, default: tuple[int, int]) -> tuple[int, int]:
    """'22-9' -> (22, 9); empty or '-' -> no quiet time."""
    raw = (_str(name) or "").strip()
    if not raw:
        return default
    if raw == "-":
        return (0, 0)
    try:
        start, end = (int(x) % 24 for x in raw.split("-"))
    except ValueError as exc:
        raise ConfigError(f"{name} must look like 22-9, got {raw!r}") from exc
    return start, end


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = (_str(name, default) or default).lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


# Konzertmeister prefixes the attendance of the subscribing member — (+) yes,
# (-) no, (?) open — and appends the club name in brackets.
DEFAULT_ANNOUNCE_TEXT = (
    "📸 Neu in der Galerie: „{title}“ mit {count} Bildern\n{link}\n"
    "Habt ihr noch mehr? Einfach hier posten, für volle Qualität als Dokument."
)
DEFAULT_SIDE_EVENTS = r"(?i)(auf|ab)bau"  # also "Bühnenaufbau"
DEFAULT_TITLE_STRIP = r"^\s*\([+\-?]\)\s*"


def default_title_strip(club_name: str | None) -> str:
    """Konzertmeister: "(+) Maifest (<club name>)" -> "Maifest"."""
    if not club_name:
        return DEFAULT_TITLE_STRIP
    return DEFAULT_TITLE_STRIP + r"|\s*\(" + re.escape(club_name) + r"\)\s*$"


def _regex(name: str, default: str | None = None) -> re.Pattern[str] | None:
    raw = os.environ.get(name, default)
    if raw is None or raw == "":
        return None  # set to empty to disable
    try:
        return re.compile(raw)
    except re.error as exc:
        raise ConfigError(f"{name} is not a valid regular expression: {exc}") from exc


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    data_dir: Path
    archive_dir: Path
    sender_salt: str

    # Calendar
    calendar_sources: tuple[str, ...]
    window_pre: timedelta
    window_post: timedelta
    late_grace: timedelta  # photos without capture time, posted after an event
    late_min_batch: int    # late assignment only for batches of this size ...
    late_batch_gap: timedelta  # ... sent by one person with gaps up to this
    event_exclude: re.Pattern[str] | None
    event_title_strip: re.Pattern[str] | None
    event_side: re.Pattern[str] | None       # build-up/tear-down, merged into the neighbouring event
    side_event_gap: timedelta
    all_day_max_days: int
    day_cutoff_hour: int

    # Quarantine and cluster detection for photos outside any event window
    quarantine_days: int
    min_event_items: int        # an event folder is created only from this many items on
    cluster_min_items: int
    cluster_min_senders: int
    cluster_max_gap: timedelta

    # Nextcloud
    nc_webdav_url: str | None
    nc_user: str | None
    nc_app_password: str | None
    nc_gallery_dir: str
    nc_inbox_dir: str
    nc_drop_dir: str          # upload folder for photographers
    drop_quiet: timedelta     # a folder is processed once nothing was added for this long

    # Processing
    img_max_edge: int
    img_max_bytes: int
    img_quality: int
    video_max_edge: int
    video_max_seconds: int        # longer: original archived, converted copy to the admin inbox …
    video_inbox_max_seconds: int  # … up to this length; beyond, archive only
    clip_as_photo_seconds: float  # shorter clips (Live/Motion photos) become a still photo; 0 = off
    video_crf: int

    # Service (built-in scheduler)
    drop_interval: timedelta
    retry_interval: timedelta
    maintenance_hour: int
    http_port: int

    # Notifications
    mqtt_host: str | None
    mqtt_port: int
    mqtt_user: str | None
    mqtt_password: str | None
    mqtt_prefix: str
    smtp_host: str | None
    smtp_port: int
    smtp_security: str          # starttls | ssl | none
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_to: tuple[str, ...]
    healthcheck_url: str | None
    report_weekday: int         # 0 = Monday … 6 = Sunday, -1 = off
    report_hour: int
    min_free_gb_nas: int
    min_free_gb_nextcloud: int

    # WAHA (session check, webhooks)
    waha_url: str | None
    waha_api_key: str | None
    hook_hmac_key: str | None = None
    club_name: str = ""                # shown in Home Assistant, stripped from Konzertmeister titles
    group_chat_id: str | None = None   # the one group whose media is taken over (phase 2)
    webhook_capture: bool = False      # record raw webhooks of group_chat_id (development)
    live_ingest: bool = False          # phase 2: take over media of group_chat_id live
    # One bot message per event when its folder fills up (live only)
    announce: bool = False
    announce_url: str | None = None    # folder link template with {path}, e.g. https://…/s/TOKEN?dir={path}
    announce_min_items: int = 5
    announce_quiet: tuple[int, int] = (22, 9)  # no messages from 22:00 to 09:00, sent at 09:00
    announce_max_age_days: int = 3     # only for events that started within this many days
    announce_text: str = DEFAULT_ANNOUNCE_TEXT

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.sqlite"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def capture_dir(self) -> Path:
        return self.data_dir / "webhook-capture"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            tz=ZoneInfo(_str("TZ", "Europe/Berlin")),
            data_dir=Path(_str("DATA_DIR", "/data")),
            archive_dir=Path(_str("ARCHIVE_DIR", "/archive")),
            sender_salt=_required("SENDER_SALT"),
            calendar_sources=_list("CALENDAR_ICS_URLS"),
            window_pre=timedelta(hours=_int("WINDOW_PRE_HOURS", 2)),
            window_post=timedelta(hours=_int("WINDOW_POST_HOURS", 48)),
            late_grace=timedelta(days=_int("LATE_GRACE_DAYS", 7)),
            late_min_batch=_int("LATE_MIN_BATCH", 3),
            late_batch_gap=timedelta(minutes=_int("LATE_BATCH_GAP_MINUTES", 10)),
            event_exclude=_regex("EVENT_EXCLUDE_REGEX"),
            event_title_strip=_regex("EVENT_TITLE_STRIP_REGEX", default_title_strip(_str("CLUB_NAME"))),
            event_side=_regex("EVENT_SIDE_REGEX", DEFAULT_SIDE_EVENTS),
            side_event_gap=timedelta(hours=_int("SIDE_EVENT_GAP_HOURS", 24)),
            all_day_max_days=_int("ALL_DAY_MAX_DAYS", 4),
            day_cutoff_hour=_int("DAY_CUTOFF_HOUR", 5),
            quarantine_days=_int("QUARANTINE_DAYS", 7),
            min_event_items=_int("MIN_EVENT_ITEMS", 3),
            cluster_min_items=_int("CLUSTER_MIN_ITEMS", 8),
            cluster_min_senders=_int("CLUSTER_MIN_SENDERS", 3),
            cluster_max_gap=timedelta(hours=_int("CLUSTER_MAX_GAP_HOURS", 3)),
            nc_webdav_url=_str("NC_WEBDAV_URL"),
            nc_user=_str("NC_USER"),
            nc_app_password=_str("NC_APP_PASSWORD"),
            nc_gallery_dir=(_str("NC_GALLERY_DIR", "Fotos") or "").strip("/"),
            nc_inbox_dir=(_str("NC_INBOX_DIR", "Fotos-Eingang") or "").strip("/"),
            nc_drop_dir=(_str("NC_DROP_DIR", "Fotos-Upload") or "").strip("/"),
            drop_quiet=timedelta(minutes=_int("DROP_QUIET_MINUTES", 10)),
            img_max_edge=_int("IMG_MAX_EDGE", 2048),
            img_max_bytes=_int("IMG_MAX_BYTES", 1_000_000),
            img_quality=_int("IMG_JPEG_QUALITY", 85),
            video_max_edge=_int("VIDEO_MAX_EDGE", 1280),
            video_max_seconds=_int("VIDEO_MAX_SECONDS", 180),
            video_inbox_max_seconds=_int("VIDEO_INBOX_MAX_SECONDS", 1800),
            clip_as_photo_seconds=float(_str("CLIP_AS_PHOTO_SECONDS", "3") or 0),
            video_crf=_int("VIDEO_CRF", 26),
            drop_interval=timedelta(minutes=max(1, _int("DROP_INTERVAL_MINUTES", 15))),
            retry_interval=timedelta(minutes=max(5, _int("RETRY_INTERVAL_MINUTES", 60))),
            maintenance_hour=_int("MAINTENANCE_HOUR", 4) % 24,
            http_port=_int("HTTP_PORT", 8080),
            mqtt_host=_str("MQTT_HOST"),
            mqtt_port=_int("MQTT_PORT", 1883),
            mqtt_user=_str("MQTT_USER"),
            mqtt_password=_str("MQTT_PASSWORD"),
            mqtt_prefix=(_str("MQTT_TOPIC_PREFIX", "gruppix") or "gruppix").strip("/"),
            smtp_host=_str("SMTP_HOST"),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_security=_choice("SMTP_SECURITY", "starttls", ("starttls", "ssl", "none")),
            smtp_user=_str("SMTP_USER"),
            smtp_password=_str("SMTP_PASSWORD"),
            smtp_from=_str("SMTP_FROM") or _str("SMTP_USER"),
            smtp_to=_list("SMTP_TO"),
            healthcheck_url=_str("HEALTHCHECK_PING_URL"),
            report_weekday=_int("REPORT_WEEKDAY", 6),
            report_hour=_int("REPORT_HOUR", 18) % 24,
            min_free_gb_nas=_int("MIN_FREE_GB_NAS", 5),
            min_free_gb_nextcloud=_int("MIN_FREE_GB_NEXTCLOUD", 2),
            waha_url=_str("WAHA_URL"),
            waha_api_key=_str("WAHA_API_KEY"),
            hook_hmac_key=_str("HOOK_HMAC_KEY"),
            club_name=_str("CLUB_NAME") or "",
            group_chat_id=_str("GROUP_CHAT_ID"),
            webhook_capture=_choice("WEBHOOK_CAPTURE", "off", ("off", "on")) == "on",
            live_ingest=_choice("LIVE_INGEST", "off", ("off", "on")) == "on",
            announce=_choice("ANNOUNCE", "off", ("off", "on")) == "on",
            announce_url=_str("GALLERY_FOLDER_URL"),
            announce_min_items=max(1, _int("ANNOUNCE_MIN_ITEMS", 5)),
            announce_quiet=_hours("ANNOUNCE_QUIET_HOURS", (22, 9)),
            announce_max_age_days=_int("ANNOUNCE_MAX_AGE_DAYS", 3),
            announce_text=(_str("ANNOUNCE_TEXT") or DEFAULT_ANNOUNCE_TEXT).replace("\\n", "\n"),
        )
