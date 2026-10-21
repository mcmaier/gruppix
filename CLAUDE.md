# CLAUDE.md – gruppix

Kontext für Claude Code. gruppix übernimmt Fotos aus der WhatsApp-Gruppe eines Vereins automatisch in eine Nextcloud-Galerie (nur Mitglieder) und archiviert die Originale auf dem Docker-Host (NAS oder Server). Nutzerdoku: `README.md`, `docs/setup.md`.

## Konventionen

- Sprache: Code, Bezeichner und Kommentare **Englisch**; alles, was Menschen lesen (Mails, Wochenbericht, HA-Texte, Doku), **Deutsch**.
- Python 3.13, Standardbibliothek bevorzugt; Abhängigkeiten nur in `worker/requirements.txt` (Pillow, pillow-heif, piexif, icalendar, recurring-ical-events, requests, paho-mqtt). ffmpeg/ffprobe kommen aus dem Docker-Image.
- Konfiguration ausschließlich über Umgebungsvariablen (`gruppix/config.py`, dokumentiert in `.env.example`). Neue Einstellung = Feld in `Config` + `from_env` + Eintrag mit Kommentar in `.env.example`.
- Datenschutz ist Designvorgabe: keine Telefonnummern/Namen speichern (nur gesalzener Hash), GPS vor jedem Upload entfernen, Texte nie speichern, Quarantäne nur auf der NAS und zeitlich begrenzt.
- Tests: `cd worker && pyflakes gruppix tests && pytest` (pyflakes findet undefinierte Namen, die pytest ohne Testabdeckung übersieht). Jede Regeländerung (Zuordnung, Filter, Kalender) braucht einen Test in `worker/tests/`.
- Läuft auf jedem Docker-Host mit Compose v2 (Synology, Linux-Server); Host-Pfade nur über `.env` (`ARCHIVE_PATH`), nie im Code. Repo hat `eol=lf` (`.gitattributes`); keine Windows-Pfade im Code.

## Architektur (`worker/gruppix/`)

| Modul | Aufgabe |
| --- | --- |
| `cli.py` | Einstieg `python -m gruppix <befehl>`; Befehle rufen `jobs.py` |
| `service.py` | Dauerdienst `serve`: Zeitplan (Jobs, laufen nacheinander in der Hauptschleife), Prüfungen, Alarme, `/health` (laufender Auftrag ≤ 3 h gilt nicht als Hänger), `/webhook`, startet `LiveIntake` |
| `jobs.py` | Arbeitseinheiten für CLI und Dienst; `pipeline_lock` (fcntl) serialisiert alles, was Zustände ändert |
| `pipeline.py` | Kernlogik: ingest → Zuordnung → Zurückhalten/Quarantäne → Verarbeitung → Archiv → Upload |
| `events.py` | iCal laden, Wiederholungen, Titelbereinigung, Ausschluss, De-Duplizierung, Zeitfenster |
| `media.py` | Aufnahmezeit, Bild (Resize, EXIF, GPS weg, HEIC), Video (ffmpeg) |
| `db.py` | SQLite, Zustandsmaschine `State`, leichte Migrationen (`_MIGRATIONS`) |
| `sinks.py` | Nextcloud-WebDAV (PUT/MKCOL/PROPFIND/DELETE, Quota) bzw. `LocalSink` für `--dry-run` |
| `ingest_export.py` | WhatsApp-Export-ZIP (Android/iOS, DE/EN) |
| `ingest_drop.py` | Fotografen-Upload-Ordner in Nextcloud |
| `notify.py` | MQTT (HA-Discovery), SMTP, healthchecks.io; Alarme nur bei Zustandswechsel |
| `webhook.py` | WAHA-Webhooks: HMAC-Prüfung (live: 401), Zähler je Ereignis/Gruppe, Mitschnitt (`WEBHOOK_CAPTURE`) |
| `live.py` | Phase 2: Webhook → Aufgaben → Warteschlange → Thread (Download, ingest, 📸, ❌, Löschen, Befehle, Opt-out) |
| `announce.py` | Bot-Nachricht pro Termin (abschaltbar, Ruhezeit) |
| `review.py` | Übersichtsseiten (HTML mit eingebetteten Vorschaubildern + JSON-Liste mit Stand) für Duplikate und Quarantäne; `--delete`/`release --sheet` handeln nur auf die Liste mit passendem Stand |
| `checks.py` | WAHA-Sitzung, Kalenderquellen, Speicher, Wochenbericht |
| `healthcheck.py` | Docker-HEALTHCHECK, eine lesbare Zeile |

**Zustände** (`db.State`): `staged → held | quarantined | ready → uploaded`; außerdem `duplicate`, `skipped` (Opt-out), `rejected` (z. B. Video zu lang), `failed`, `purged`, `removed`.

**Zuordnungsregeln** (`pipeline.Pipeline`):
1. Aufnahmezeit aus EXIF, wenn plausibel (≤ 90 Tage vor Sendung), sonst Sendezeit.
2. Fenster: Termin-Beginn − `WINDOW_PRE_HOURS` bis Ende + `WINDOW_POST_HOURS`; innerhalb der Termindauer gewinnt der Termin, sonst der spätere.
3. Nur Sendezeit bekannt und außerhalb des Fensters: Nachzügler-Paket (≥ `LATE_MIN_BATCH` eines Absenders, Abstand ≤ `LATE_BATCH_GAP_MINUTES`) bis `LATE_GRACE_DAYS` nach dem Termin.
4. Mindestmenge `MIN_EVENT_ITEMS`: zurückhalten (`held`), mit Erreichen alle veröffentlichen; nach Fristende → Quarantäne. Fotografen-Uploads (`force=True`) und gepinnte Elemente (📸, `pin()`) umgehen das.
5. Häufungen ohne Termin → `Fotos-Eingang/<Datum> Unbenannt`.
5a. Kalender-Aufbereitung (`events.load_calendar`): Dubletten über Quellen – mit Uhrzeit vor ganztägig, sonst erste Quelle (ganztägig nur bei ähnlichem Titel); ganztägige Einträge `DAY_CUTOFF_HOUR`–`DAY_CUTOFF_HOUR`, länger als `ALL_DAY_MAX_DAYS` → verworfen; Nebentermine (`EVENT_SIDE_REGEX`, Aufbau/Abbau) innerhalb `SIDE_EVENT_GAP_HOURS` werden in den Haupttermin eingerechnet (Ordnerdatum bleibt das des Haupttermins, `Event.label`).
5b. Fotografen-Ordner (`Pipeline._ingest_drop`): Termin am Ordnerdatum (`on_day`); sonst Termin zur plausiblen Kamerazeit (`match`); sonst Termin mit ähnlichem Namen ±1 Tag (`near_day`); sonst eigener Ordner mit dem Ordnernamen. Unterordner werden flach übernommen. Lässt sich eine verarbeitete Datei nicht löschen (HTTP 423, Nextcloud-Sperre), bleibt sie liegen (`UNDELETED`), blockiert die übrigen nicht und wird im nächsten Lauf ohne erneuten Download nur gelöscht; der Dienst meldet das als Warnung `drop-cleanup`, nicht als Fehler.
5c. Duplikate: SHA-256 über alle Quellen; zusätzlich für Chat-Bilder (export, waha) Wahrnehmungs-Hash (`media.fingerprint`, dHash ≤ `SIMILAR_MAX_BITS`) innerhalb ±15 min Sendezeit – über die Quellen hinweg jede Größe, innerhalb einer Quelle nur andere Pixelzahl (HD + Standard; Serienbilder bleiben). Export-Dateien sind NICHT bytegleich mit WAHA-Dateien (getestet). Auf Wunsch gelöschte Bilder (`removed`) kommen so auch über einen späteren Export nicht zurück. Ältere Bilder: `backfill-fingerprints` (nachts automatisch), Altlasten: `find-duplicates` → `review.duplicates_report` → `--delete <Stand>` → `Pipeline.drop_duplicate` (Nextcloud-Kopie weg, Zustand `duplicate`, Archiv bleibt); `--keep-both` → Tabelle `distinct_pairs`, nie wieder gemeldet.
5d. Videos > `VIDEO_MAX_SECONDS` → `media.VideoTooLong` → umgewandelt nach `Fotos-Eingang/<Terminpfad>/Videos`; > `VIDEO_INBOX_MAX_SECONDS` oder sonst abgelehnt → `rejected`, Original trotzdem im Archiv (`_Eingang/`), ❌ löscht auch das.
5e. Clips ≤ `CLIP_AS_PHOTO_SECONDS` → `Pipeline._still_from_short_clip` (Bild aus der Mitte) → `kind=image`, Clip als `companion_path` (erst Staging, dann Archiv neben dem Standbild); `sha256` bleibt die des Clips. Live: `_data.Message.videoMessage.gifPlayback` → verworfen.
6. Dateiname `JJJJMMTT-HHMMSS-<seq>_<hash6>` bestimmt die Reihenfolge in der Freigabe; geschätzte EXIF-Zeiten werden auf das Termin-Ende begrenzt.

## Phase 2 (Live-Betrieb, `LIVE_INGEST=on`)

`webhook.WebhookInbox` → `live.tasks_from_webhook` (rein, ohne I/O) → Warteschlange `data/live/queue/*.json` (Dateiname = Ankunftsreihenfolge) → Thread `live.LiveIntake`: erst Medien von WAHA laden (ohne Sperre, WAHA löscht nach `WHATSAPP_FILES_LIFETIME`), dann unter `pipeline_lock` streng der Reihe nach `ingest` / `pin` / `remove`. Nach 5 Fehlversuchen → `data/live/failed/` + Alarm.

Payload-Fakten (WAHA 2026.9.2, GOWS; `tests/fixtures/waha_gows_2026.9.2.json`, anonymisiert):
- Nachrichten-ID `<fromMe>_<chat>_<ID>_<participant>`; Reaktionen verweisen mit falschem `fromMe`/Teilnehmer → nur `<ID>` als Schlüssel (`message_key`), `media.id = "waha:<ID>"`.
- Absender sind LIDs (`…@lid`); nur `hash_sender` wird gespeichert (auch in der Warteschlange).
- Album: Kopfnachricht ohne Medium (`albumMessage`), dann je Bild eine Nachricht.
- HD-Foto kommt nur in Standardauflösung (1600 px) an; volle Qualität nur „als Dokument“ (`media.filename` gesetzt, Originaldatei).
- Reaktion entfernt: `reaction.text == ""` → ignoriert. „Für alle löschen“: `message.revoked` mit `revokedMessageId`.
- Bildunterschriften stehen in `payload.body` und werden nie übernommen.
- Admins: `GET /api/<session>/groups/<id>/participants` liefert je Admin LID, `@c.us` und `@s.whatsapp.net` (verifiziert); Reaktionen kommen mit LID.

Admin-Befehl `Ordner [heute|gestern|JJJJ-MM-TT|TT.MM.JJJJ] <Name>` per Direktnachricht an den Bot (`live.parse_folder_command`): legt ein Ereignis `manual:<datum>:<name>` in `manual_events` an (Tag ab `DAY_CUTOFF_HOUR` bis zum nächsten Morgen), `jobs.build_calendar` mischt diese Ereignisse in jeden Kalender, `Pipeline.add_event` übernimmt passende Quarantäne-Bilder; manuelle Ereignisse haben keine Mindestmenge. Antwort über `POST /api/sendText`. Opt-out: Direktnachricht `Stopp`/`Start` von jedem (`live.parse_optout`), gespeichert werden die Hashes aller Absender-IDs der DM (`live.sender_ids`: from, `_data.Info.Sender/SenderAlt`, `@s.whatsapp.net`→`@c.us`), damit die LID aus Gruppennachrichten passt. Gilt nicht für Export-Importe (Absender dort = Anzeigename).

Die Form der Direktnachricht im Test ist aus den Gruppen-Mitschnitten abgeleitet, nicht mitgeschnitten.

Bot-Nachricht (`announce.Announcer`, `ANNOUNCE=on`): nach jedem live veröffentlichten Element `consider()` → einmal pro Termin (Tabelle `announcements`) ab `ANNOUNCE_MIN_ITEMS`, nicht für Termine älter als `ANNOUNCE_MAX_AGE_DAYS`, Fälligkeit nach Ruhezeit; `LiveIntake.announce_due()` sendet über `POST /api/sendText` an `GROUP_CHAT_ID`, Link aus `GALLERY_FOLDER_URL` mit `{path}`.

CI: `.github/workflows/tests.yml` (pyflakes, pytest mit ffmpeg, `docker compose config`).

Offen:
- Nachholen nach längerem Ausfall (> Webhook-Retries / Datei-Lebensdauer) über die WAHA-API.

## Betrieb

`docker compose up -d` startet `gruppix-waha` und `gruppix-worker` (Befehl `serve`). Einzelbefehle im laufenden Dienst: `docker compose exec -T worker python -m gruppix <befehl>` (kein `run`: jeder kurzlebige Container löst auf Synology „unerwartet beendet“ aus). Dry-Run-Daten unter `/data/dryrun` mit eigener DB und eigener Sperre.
