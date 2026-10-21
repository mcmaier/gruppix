"""Media handling: classification, capture time, image and video conversion."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import piexif
from PIL import ExifTags, Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".3gp", ".m4v"})
OUTPUT_SUFFIX = {"image": ".jpg", "video": ".mp4"}

_EXIF_DATE = "%Y:%m:%d %H:%M:%S"
# A capture time from metadata is trusted only if it is close to the message time.
_PLAUSIBLE_BEFORE = timedelta(days=90)
_PLAUSIBLE_AFTER = timedelta(hours=1)


class MediaRejected(Exception):
    """The file is readable but must not be published (policy limit)."""


class VideoTooLong(MediaRejected):
    def __init__(self, seconds: float, limit: int) -> None:
        super().__init__(f"video longer than {limit}s ({seconds:.0f}s)")
        self.seconds = seconds


def classify(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def fingerprint(path: Path) -> tuple[str, int] | None:
    """Perceptual hash (64-bit dHash, 16 hex chars) and pixel count of an image.

    The same photo from a WhatsApp export and from WAHA are different files
    (the sender's phone keeps its own copy, HD sends two sizes), so SHA-256
    alone misses them; the dHash survives recompression and resizing.
    """
    try:
        with Image.open(path) as im:
            pixels = im.size[0] * im.size[1]
            im.draft("L", (64, 64))  # JPEG: decode at reduced size, much faster
            img = ImageOps.exif_transpose(im).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    except Exception:  # noqa: BLE001 - unreadable images simply get no fingerprint
        return None
    px = img.tobytes()  # mode "L": one byte per pixel, row by row
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}", pixels


def hash_distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def is_plausible(captured: datetime, sent_at: datetime) -> bool:
    return sent_at - _PLAUSIBLE_BEFORE <= captured <= sent_at + _PLAUSIBLE_AFTER


def read_capture_time(path: Path, kind: str, tz: ZoneInfo) -> datetime | None:
    """Best-effort capture time from EXIF / container metadata."""
    try:
        return _image_capture_time(path, tz) if kind == "image" else _video_capture_time(path)
    except Exception:  # noqa: BLE001 - metadata is optional, never fatal
        return None


def _image_capture_time(path: Path, tz: ZoneInfo) -> datetime | None:
    with Image.open(path) as im:
        exif = im.getexif()
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        raw = sub.get(ExifTags.Base.DateTimeOriginal) or exif.get(ExifTags.Base.DateTime)
        offset = sub.get(ExifTags.Base.OffsetTimeOriginal)
    if not raw:
        return None
    naive = datetime.strptime(str(raw).strip("\x00 "), _EXIF_DATE)
    if offset:
        try:
            return datetime.strptime(f"{naive:%Y-%m-%d %H:%M:%S}{str(offset).strip()}", "%Y-%m-%d %H:%M:%S%z")
        except ValueError:
            pass
    return naive.replace(tzinfo=tz)


def _video_capture_time(path: Path) -> datetime | None:
    raw = probe(path).get("format", {}).get("tags", {}).get("creation_time")
    if not raw:
        return None
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.year < 2000:  # QuickTime epoch default (1904) or unset
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- images ----------------------------------------------------------------


def process_image(src: Path, dst: Path, taken_at: datetime, *, max_edge: int, max_bytes: int, quality: int) -> None:
    """Write a gallery copy: GPS removed, capture date set, re-encoded only if needed."""
    stamp = taken_at.strftime(_EXIF_DATE).encode()
    src_size = src.stat().st_size
    with Image.open(src) as im:
        is_jpeg = im.format == "JPEG"
        too_large = max(im.size) > max_edge
        reencode = not is_jpeg or too_large or src_size > max_bytes
        if reencode:
            img = _to_rgb(ImageOps.exif_transpose(im))
            img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            img.save(
                dst,
                "JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                exif=piexif.dump(_fresh_exif(stamp)),
            )
            # A JPEG that only exceeds the byte limit is re-encoded at the same
            # size; keep the original if that saves less than 20 % (avoids
            # generation loss for little gain).
            if is_jpeg and not too_large and dst.stat().st_size > 0.8 * src_size:
                dst.unlink()
            else:
                return
    # Small JPEG (typical WhatsApp standard quality): keep pixels, rewrite EXIF only.
    shutil.copyfile(src, dst)
    piexif.insert(_sanitized_exif(src, stamp), str(dst))


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img if img.mode in ("RGB", "L") else img.convert("RGB")


def _fresh_exif(stamp: bytes) -> dict[str, Any]:
    return {
        "0th": {piexif.ImageIFD.DateTime: stamp},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
        },
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }


def _sanitized_exif(src: Path, stamp: bytes) -> bytes:
    """Existing EXIF minus location, maker notes and thumbnail, with our date."""
    try:
        data = piexif.load(str(src))
        data["GPS"] = {}
        data["1st"] = {}
        data["thumbnail"] = None
        zeroth = data.setdefault("0th", {})
        zeroth.pop(piexif.ImageIFD.GPSTag, None)
        zeroth[piexif.ImageIFD.DateTime] = stamp
        exif = data.setdefault("Exif", {})
        exif.pop(piexif.ExifIFD.MakerNote, None)
        exif[piexif.ExifIFD.DateTimeOriginal] = stamp
        exif[piexif.ExifIFD.DateTimeDigitized] = stamp
        return piexif.dump(data)
    except Exception:  # noqa: BLE001 - malformed EXIF: start from scratch
        return piexif.dump(_fresh_exif(stamp))


# --- videos ----------------------------------------------------------------


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def short_clip_duration(path: Path, max_seconds: float) -> float | None:
    """Duration if ``path`` is a video of at most ``max_seconds`` (Live/Motion photo), else None."""
    try:
        info = probe(path)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None
    if not any(s.get("codec_type") == "video" for s in info.get("streams", [])):
        return None
    duration = float(info.get("format", {}).get("duration") or 0)
    return duration if 0 < duration <= max_seconds else None


def extract_still(src: Path, dst: Path, at_seconds: float) -> None:
    """Write one frame of a video as JPEG (rotation applied by ffmpeg)."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{at_seconds:.2f}", "-i", str(src),
         "-frames:v", "1", "-q:v", "2", "-map_metadata", "-1", str(dst)],
        check=True, capture_output=True,
    )


def process_video(src: Path, dst: Path, taken_at: datetime, *, max_edge: int, max_seconds: int, crf: int) -> None:
    """Write a browser-playable MP4 (H.264/AAC, faststart) without metadata."""
    info = probe(src)
    duration = float(info.get("format", {}).get("duration") or 0)
    if duration > max_seconds:
        raise VideoTooLong(duration, max_seconds)

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaRejected("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)

    stream_copy = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and max(width, height) <= max_edge
        and (audio is None or audio.get("codec_name") == "aac")
    )

    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
    if stream_copy:
        cmd += ["-c", "copy"]
    else:
        # Limit the long edge, keep aspect ratio, force even dimensions.
        scale = (
            f"scale='if(gte(iw,ih),2*trunc(min({max_edge},iw)/2),-2)'"
            f":'if(gte(iw,ih),-2,2*trunc(min({max_edge},ih)/2))'"
        )
        cmd += [
            "-vf", scale,
            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
        ]
    created = taken_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    cmd += [
        "-map_metadata", "-1",           # drops location and device tags
        "-metadata", f"creation_time={created}",
        "-movflags", "+faststart",       # playable while downloading
        "-f", "mp4", str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
