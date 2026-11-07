"""Network failures as short, actionable messages instead of tracebacks.

requests hides the real cause several levels deep (ConnectionError → MaxRetryError
→ NameResolutionError → socket.gaierror). Jobs, checks and the live intake use
this module to tell "name not resolvable" from "refused" or "timeout", and to add
the fix for the typical Docker case: a container that is not in the gruppix
network any more (e.g. after an update in Synology Container Manager).
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Iterator
from urllib.parse import urlparse

import requests

DNS = "dns"
REFUSED = "refused"
TIMEOUT = "timeout"
TLS = "tls"
OTHER = "other"

# Shown for Docker service names (no dot) that do not resolve
DOCKER_NETWORK_HINT = (
    "Container nicht im gruppix-Docker-Netz (z. B. nach einem Update über den Container Manager). "
    "Abhilfe im Projektordner: sudo docker compose up -d --force-recreate"
)


def is_network_error(exc: BaseException) -> bool:
    """True for failures to reach a host at all (DNS, refused, timeout, TLS)."""
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception and everything it wraps (cause, context, urllib3 .reason, args)."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack += [current.__cause__, current.__context__, getattr(current, "reason", None)]
        stack += [arg for arg in current.args if isinstance(arg, BaseException)]


def kind(exc: BaseException) -> str:
    chain = list(_chain(exc))
    if any(isinstance(e, socket.gaierror) or type(e).__name__ == "NameResolutionError" for e in chain):
        return DNS
    if any(isinstance(e, (requests.exceptions.SSLError, ssl.SSLError)) for e in chain):
        return TLS
    if any(isinstance(e, (requests.Timeout, TimeoutError, socket.timeout)) for e in chain):
        return TIMEOUT
    if any(isinstance(e, ConnectionRefusedError) for e in chain):
        return REFUSED
    return OTHER


def host(exc: BaseException) -> str | None:
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None)
    return urlparse(url).hostname if url else None


def is_docker_name(name: str | None) -> bool:
    """Compose service names like 'waha' or 'worker' have no dot."""
    return bool(name) and "." not in name and name != "localhost"


def describe(exc: BaseException, what: str | None = None) -> str:
    """One line in German, e.g. "WAHA nicht erreichbar: Name 'waha' nicht auflösbar (DNS) – …"."""
    name = host(exc)
    target = what or "Server"
    at = f"'{name}'" if name else "die Adresse"
    problem = kind(exc)
    if problem == DNS:
        text = f"{target} nicht erreichbar: Name {at} nicht auflösbar (DNS)"
        if is_docker_name(name):
            text += f" – {DOCKER_NETWORK_HINT}"
        elif name:
            text += " – DNS oder Internetverbindung des Docker-Hosts prüfen"
        return text
    if problem == REFUSED:
        text = f"{target} nicht erreichbar: Verbindung zu {at} abgelehnt"
        if is_docker_name(name):
            text += " – Container gestoppt oder startet gerade"
        return text
    if problem == TIMEOUT:
        return f"{target} antwortet nicht (Zeitüberschreitung bei {at}) – Host ausgelastet?"
    if problem == TLS:
        return f"{target}: TLS-/Zertifikatsfehler bei {at}"
    return f"{target} nicht erreichbar ({type(exc).__name__})"


def resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, None)
    except OSError:
        return False
    return True
