"""Nextcloud access via WebDAV, or a local directory for dry runs.

A *sink* receives gallery files; a *drop source* is the photographers' upload
folder the worker collects from. Both are served by the same classes.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote, urlparse

import requests

from .config import Config, ConfigError


@dataclass(frozen=True)
class RemoteFile:
    path: str             # relative to the Nextcloud user root, no leading slash
    uploaded: datetime    # upload time if known, else last modification
    size: int


class Sink(Protocol):
    def put(self, local: Path, remote_path: str) -> None: ...

    def delete(self, remote_path: str) -> None: ...


class DropSource(Protocol):
    def list_tree(self, directory: str) -> list[RemoteFile]: ...

    def download(self, remote_path: str, local: Path) -> None: ...

    def delete(self, remote_path: str) -> None: ...


class LocalSink:
    """Mirrors the Nextcloud layout into a local directory (dry run)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, local: Path, remote_path: str) -> None:
        target = self._root / remote_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, target)

    def delete(self, remote_path: str) -> None:
        target = self._root / remote_path
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    def list_tree(self, directory: str) -> list[RemoteFile]:
        base = self._root / directory
        if not base.is_dir():
            return []
        return [
            RemoteFile(
                path=p.relative_to(self._root).as_posix(),
                uploaded=datetime.fromtimestamp(p.stat().st_mtime, timezone.utc),
                size=p.stat().st_size,
            )
            for p in sorted(base.rglob("*"))
            if p.is_file()
        ]

    def download(self, remote_path: str, local: Path) -> None:
        shutil.copyfile(self._root / remote_path, local)

    def quota_available(self) -> int | None:
        return None  # dry run: no quota


_PROPFIND = (
    b'<?xml version="1.0"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns"><d:prop>'
    b"<d:resourcetype/><d:getlastmodified/><d:getcontentlength/><nc:upload_time/>"
    b"</d:prop></d:propfind>"
)
_DAV = "{DAV:}"
_NC = "{http://nextcloud.org/ns}"


class WebDavSink:
    """Minimal WebDAV client for Nextcloud (MKCOL, PUT, GET, PROPFIND, DELETE)."""

    def __init__(self, base_url: str, user: str, password: str, timeout: float = 300) -> None:
        self._base = base_url.rstrip("/")
        self._base_path = unquote(urlparse(self._base).path).rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers["User-Agent"] = "gruppix"
        self._known_dirs: set[str] = set()

    @classmethod
    def from_config(cls, cfg: Config) -> WebDavSink:
        if not (cfg.nc_webdav_url and cfg.nc_user and cfg.nc_app_password):
            raise ConfigError("NC_WEBDAV_URL, NC_USER and NC_APP_PASSWORD are required (or use --dry-run)")
        return cls(cfg.nc_webdav_url, cfg.nc_user, cfg.nc_app_password)

    # --- sink --------------------------------------------------------------

    def put(self, local: Path, remote_path: str) -> None:
        directory = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
        self._ensure_dir(directory)
        with local.open("rb") as fh:
            response = self._session.put(self._url(remote_path), data=fh, timeout=self._timeout)
        if response.status_code not in (200, 201, 204):
            raise requests.HTTPError(f"PUT {remote_path}: HTTP {response.status_code}", response=response)

    def delete(self, remote_path: str) -> None:
        response = self._session.delete(self._url(remote_path), timeout=self._timeout)
        if response.status_code not in (200, 204, 404):
            raise requests.HTTPError(f"DELETE {remote_path}: HTTP {response.status_code}", response=response)

    # --- drop source -------------------------------------------------------

    def list_tree(self, directory: str) -> list[RemoteFile]:
        """All files below ``directory`` (Depth: 1 per level; Nextcloud refuses infinity)."""
        files: list[RemoteFile] = []
        pending = [directory.strip("/")]
        while pending:
            current = pending.pop()
            response = self._session.request(
                "PROPFIND",
                self._url(current) + "/",
                data=_PROPFIND,
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                timeout=self._timeout,
            )
            if response.status_code == 404:
                continue
            if response.status_code != 207:
                raise requests.HTTPError(f"PROPFIND {current}: HTTP {response.status_code}", response=response)
            for entry in ET.fromstring(response.content).iter(f"{_DAV}response"):
                rel = self._relative(entry.findtext(f"{_DAV}href", ""))
                if rel == current:
                    continue
                props = _props(entry)
                if props.get("collection"):
                    pending.append(rel)
                    continue
                files.append(RemoteFile(path=rel, uploaded=props["uploaded"], size=props["size"]))
        return sorted(files, key=lambda f: f.path)

    def quota_available(self) -> int | None:
        """Free bytes for this Nextcloud user, None if unlimited or unknown."""
        body = (
            b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
            b"<d:quota-available-bytes/></d:prop></d:propfind>"
        )
        response = self._session.request(
            "PROPFIND", self._base + "/", data=body,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"}, timeout=self._timeout,
        )
        if response.status_code != 207:
            raise requests.HTTPError(f"PROPFIND quota: HTTP {response.status_code}", response=response)
        value = ET.fromstring(response.content).findtext(f".//{_DAV}quota-available-bytes")
        try:
            available = int(value) if value else -1
        except ValueError:
            return None
        return available if available >= 0 else None  # -1/-2/-3: not computed / unknown / unlimited

    def download(self, remote_path: str, local: Path) -> None:
        with self._session.get(self._url(remote_path), stream=True, timeout=self._timeout) as response:
            if response.status_code != 200:
                raise requests.HTTPError(f"GET {remote_path}: HTTP {response.status_code}", response=response)
            with local.open("wb") as fh:
                for chunk in response.iter_content(1 << 20):
                    fh.write(chunk)

    # --- helpers -----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base}/{quote(path.strip('/'))}"

    def _relative(self, href: str) -> str:
        path = unquote(urlparse(href).path)
        if path.startswith(self._base_path):
            path = path[len(self._base_path):]
        return path.strip("/")

    def _ensure_dir(self, directory: str) -> None:
        parts = [p for p in directory.split("/") if p]
        for depth in range(1, len(parts) + 1):
            current = "/".join(parts[:depth])
            if current in self._known_dirs:
                continue
            response = self._session.request("MKCOL", self._url(current), timeout=self._timeout)
            if response.status_code not in (201, 405):  # 405: collection already exists
                raise requests.HTTPError(f"MKCOL {current}: HTTP {response.status_code}", response=response)
            self._known_dirs.add(current)


def _props(entry: ET.Element) -> dict:
    """Merge the found properties of all <propstat> blocks of one response."""
    result: dict = {"collection": False, "size": 0, "uploaded": datetime.now(timezone.utc)}
    modified: datetime | None = None
    upload_time = 0
    for propstat in entry.iter(f"{_DAV}propstat"):
        if " 200 " not in (propstat.findtext(f"{_DAV}status") or ""):
            continue
        prop = propstat.find(f"{_DAV}prop")
        if prop is None:
            continue
        resourcetype = prop.find(f"{_DAV}resourcetype")
        if resourcetype is not None and resourcetype.find(f"{_DAV}collection") is not None:
            result["collection"] = True
        if (length := prop.findtext(f"{_DAV}getcontentlength")):
            result["size"] = int(length)
        if (lastmod := prop.findtext(f"{_DAV}getlastmodified")):
            modified = parsedate_to_datetime(lastmod)
        if (uploaded := prop.findtext(f"{_NC}upload_time")) and uploaded.isdigit():
            upload_time = int(uploaded)
    # Web uploads keep the file's own mtime (often the camera date), so the
    # Nextcloud upload time is preferred when the server provides it.
    if upload_time > 0:
        result["uploaded"] = datetime.fromtimestamp(upload_time, timezone.utc)
    elif modified is not None:
        result["uploaded"] = modified
    return result
