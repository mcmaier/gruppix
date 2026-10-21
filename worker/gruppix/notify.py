"""Notifications: Home Assistant via MQTT, e-mail via SMTP, healthchecks.io ping.

Alerts are keyed and sent only on state changes (problem appears / is
resolved), so a failing job does not produce a message every 15 minutes.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import paho.mqtt.publish as mqtt_publish
import requests

from .config import Config

log = logging.getLogger(__name__)

_EXPIRE_AFTER = 900  # s; HA marks the entities unavailable when the worker goes silent


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._active: dict[str, str] = {}  # alert key -> message

    # --- configuration -----------------------------------------------------

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self._cfg.mqtt_host)

    @property
    def mail_enabled(self) -> bool:
        return bool(self._cfg.smtp_host and self._cfg.smtp_from and self._cfg.smtp_to)

    def describe(self) -> str:
        channels = [
            name
            for name, on in (
                ("MQTT", self.mqtt_enabled),
                ("e-mail", self.mail_enabled),
                ("healthchecks.io", bool(self._cfg.healthcheck_url)),
            )
            if on
        ]
        return ", ".join(channels) or "none (log only)"

    # --- alerts ------------------------------------------------------------

    @property
    def active(self) -> dict[str, str]:
        return dict(self._active)

    def alert(self, key: str, title: str, message: str) -> None:
        """Report a problem once; repeated calls with the same key are silent."""
        if key in self._active:
            self._active[key] = message
            return
        self._active[key] = message
        log.warning("ALERT %s: %s", title, message)
        self._send_alert("problem", title, message)

    def resolve(self, key: str, title: str) -> None:
        if self._active.pop(key, None) is None:
            return
        log.info("RESOLVED %s", title)
        self._send_alert("resolved", f"Behoben: {title}", "Das Problem besteht nicht mehr.")

    def _send_alert(self, kind: str, title: str, message: str) -> None:
        self._mqtt([(f"{self._cfg.mqtt_prefix}/alert", {"kind": kind, "title": title, "message": message}, False)])
        self.publish_status()
        self.mail(f"[Gruppix] {title}", message)

    def test(self) -> dict[str, str]:
        """Send a test message on every configured channel; report per channel."""
        title, message = "Testnachricht", "Wenn du das liest, funktioniert dieser Kanal."
        results: dict[str, str] = {}
        if self.mqtt_enabled:
            self.announce()
            ok = self._mqtt(
                [(f"{self._cfg.mqtt_prefix}/alert", {"kind": "test", "title": title, "message": message}, False)]
            )
            self.publish_status()
            results["MQTT"] = "gesendet (Home Assistant prüfen)" if ok else "FEHLER (Broker nicht erreichbar?)"
        if self.mail_enabled:
            results["E-Mail"] = "gesendet" if self.mail(f"[Gruppix] {title}", message) else "FEHLER (siehe Protokoll)"
        if self._cfg.healthcheck_url:
            try:
                requests.get(self._cfg.healthcheck_url, timeout=10).raise_for_status()
                results["healthchecks.io"] = "Ping ok"
            except requests.RequestException as exc:
                results["healthchecks.io"] = f"FEHLER ({exc})"
        return results

    # --- state for Home Assistant -----------------------------------------

    def announce(self) -> None:
        """MQTT discovery: Home Assistant creates the entities by itself."""
        p = self._cfg.mqtt_prefix
        # Identifier follows the topic prefix: renaming the prefix keeps nothing,
        # keeping it keeps the Home Assistant device and its entities.
        device = {"identifiers": [p], "name": "Gruppix", "manufacturer": self._cfg.club_name or "gruppix"}
        configs = [
            (
                f"homeassistant/binary_sensor/{p}/problem/config",
                {
                    "name": "Problem",
                    "unique_id": f"{p}_problem",
                    "device_class": "problem",
                    "state_topic": f"{p}/status",
                    "value_template": "{{ 'ON' if value_json.problems else 'OFF' }}",
                    "json_attributes_topic": f"{p}/status",
                    "expire_after": _EXPIRE_AFTER,
                    "device": device,
                },
                True,
            ),
            (
                f"homeassistant/sensor/{p}/last_problem/config",
                {
                    "name": "Letzte Meldung",
                    "unique_id": f"{p}_last_problem",
                    "state_topic": f"{p}/status",
                    # HA states are limited to 255 characters
                    "value_template": "{{ ((value_json.problems | first)[:250]) if value_json.problems else 'OK' }}",
                    "expire_after": _EXPIRE_AFTER,
                    "icon": "mdi:image-multiple",
                    "device": device,
                },
                True,
            ),
            (
                f"homeassistant/sensor/{p}/week_uploaded/config",
                {
                    "name": "Bilder letzte Woche",
                    "unique_id": f"{p}_week_uploaded",
                    "state_topic": f"{p}/report",
                    "value_template": "{{ value_json.uploaded }}",
                    "unit_of_measurement": "Bilder",
                    "icon": "mdi:camera",
                    "device": device,
                },
                True,
            ),
            (
                f"homeassistant/sensor/{p}/to_review/config",
                {
                    "name": "Zu prüfen",
                    "unique_id": f"{p}_to_review",
                    "state_topic": f"{p}/report",
                    # duplicate pairs + quarantined items on the review pages (weekly)
                    "value_template": "{{ (value_json.review_duplicates | default(0))"
                                      " + (value_json.review_quarantine | default(0)) }}",
                    "icon": "mdi:image-search",
                    "device": device,
                },
                True,
            ),
        ]
        self._mqtt(configs)

    def publish_status(self) -> None:
        self._mqtt([(f"{self._cfg.mqtt_prefix}/status", {"problems": list(self._active.values())}, True)])

    def publish_report(self, data: dict[str, Any]) -> None:
        self._mqtt([(f"{self._cfg.mqtt_prefix}/report", data, True)])

    # --- dead man's switch -------------------------------------------------

    def ping(self, healthy: bool) -> None:
        """healthchecks.io: silence (worker/NAS/internet down) triggers its e-mail."""
        url = self._cfg.healthcheck_url
        if not url:
            return
        try:
            requests.get(url if healthy else url.rstrip("/") + "/fail", timeout=10)
        except requests.RequestException as exc:
            log.debug("healthcheck ping failed: %s", exc)

    # --- transports --------------------------------------------------------

    def mail(self, subject: str, body: str) -> bool:
        if not self.mail_enabled:
            return False
        cfg = self._cfg
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.smtp_from
        msg["To"] = ", ".join(cfg.smtp_to)
        msg.set_content(body + "\n\n-- \nGruppix (automatische Nachricht)\n")
        try:
            context = ssl.create_default_context()
            if cfg.smtp_security == "ssl":
                server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=context)
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            with server:
                if cfg.smtp_security == "starttls":
                    server.starttls(context=context)
                if cfg.smtp_user and cfg.smtp_password:
                    server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg)
            return True
        except (OSError, smtplib.SMTPException) as exc:
            log.error("Sending e-mail failed: %s", exc)
            return False

    def _mqtt(self, messages: list[tuple[str, dict[str, Any], bool]]) -> bool:
        if not self.mqtt_enabled:
            return False
        cfg = self._cfg
        auth = {"username": cfg.mqtt_user, "password": cfg.mqtt_password} if cfg.mqtt_user else None
        try:
            mqtt_publish.multiple(
                [
                    {"topic": topic, "payload": json.dumps(payload, ensure_ascii=False), "qos": 1, "retain": retain}
                    for topic, payload, retain in messages
                ],
                hostname=cfg.mqtt_host,
                port=cfg.mqtt_port,
                auth=auth,
                client_id="gruppix-worker",
                keepalive=30,
            )
        except (OSError, ValueError) as exc:
            log.error("MQTT publish failed: %s", exc)
            return False
        return True
