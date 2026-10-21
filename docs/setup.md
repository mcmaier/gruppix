# gruppix – Einrichtung

gruppix übernimmt Fotos und kurze Videos aus der WhatsApp-Gruppe eines Vereins automatisch in eine Nextcloud-Galerie, sortiert nach den Terminen im Vereinskalender. Die Originale werden auf dem Docker-Host archiviert. Diese Anleitung beschreibt eine Neuinstallation von Anfang bis Live-Betrieb.

| Komponente | Ort | Aufgabe |
| --- | --- | --- |
| WAHA | Docker-Host, Container `gruppix-waha` | WhatsApp-Gateway, als verknüpftes Gerät der Bot-Nummer |
| Worker | Docker-Host, Container `gruppix-worker` | Zuordnung, Verarbeitung, Archiv, Upload, Zeitplan, Prüfungen |
| Archiv | Docker-Host, `ARCHIVE_PATH` | Originale, wie sie aus WhatsApp oder der Kamera kommen |
| Galerie | Nextcloud, `Fotos/` | Bereinigte Kopien (ohne GPS), Passwort-Link für Mitglieder |
| Eingang | Nextcloud, `Fotos-Eingang/` | Häufungen ohne Termin, unklare Uploads; nur Admins |
| Upload | Nextcloud, `Fotos-Upload/` | Kamerafotos der Fotografen, vom Worker abgeholt |
| Kalender | iCal-Adressen (z. B. Google, Konzertmeister) | Bestimmen, welcher Termin ein Bild bekommt |

**Voraussetzungen**

- **Docker-Host** mit Docker Engine und Compose v2, dauerhaft online:
  - eine Synology mit DSM 7.2+ und Container Manager, oder
  - ein Linux-Server bzw. vServer bei einem Dienstleister (Abschnitt 2B).
- **Nextcloud** mit WebDAV, Admin-Rechte zum Anlegen eines Benutzers. Getestet mit Nextcloud 34.
- **Bot-Nummer:** ein Zweithandy (Android) mit WhatsApp Business und eigener Nummer, z. B. einer Festnetz-Zusatznummer.
- **Vereinskalender** als iCal-Adresse.

## 1. Nextcloud vorbereiten

Ein eigener Benutzer `fotobot` besitzt alle Fotoordner. So hängt die Galerie an keinem persönlichen Konto, und das App-Passwort auf dem Host kann nichts anderes anfassen.

1. **Benutzer anlegen** (als Admin): Benutzername `fotobot`, Anzeigename `Gruppix`, Kontingent nach Bedarf (Richtwert 20–40 GB).
2. **App-Passwort** (als `fotobot`): *Persönliche Einstellungen → Sicherheit → Geräte & Sitzungen*, Name `gruppix` → *Neues App-Passwort erstellen*. Das wird `NC_APP_PASSWORD`.
   - Die WebDAV-Adresse steht unter *Dateien → Dateien-Einstellungen → WebDAV* und wird `NC_WEBDAV_URL` (`https://<host>/remote.php/dav/files/fotobot/`).
3. **Ordner** (als `fotobot`), nur diese von Hand, den Rest legt der Worker an:

```
Fotos/                         Galerie für Mitglieder (Passwort-Link)
├── 2026/
│   └── 2026-07-11 Sommerfest/
│       └── Videos/
└── Öffentlich/                manuelle Auswahl, Links ohne Passwort
Fotos-Eingang/                 nur Admins
Fotos-Upload/                  Fotografen, wird vom Worker geleert
```

4. **Freigaben:**

| Ordner | Freigabe an | Rechte | Zweck |
| --- | --- | --- | --- |
| `Fotos` | Öffentlicher Link mit Passwort | Nur lesen, Download | Mitglieder |
| `Fotos`, `Fotos-Eingang` | Admin-Konten (intern) | Bearbeiten, Löschen | Aussortieren, verschieben |
| `Fotos-Upload` | Öffentlicher Link, eigenes Passwort | Lesen, Erstellen, Bearbeiten; **kein** Löschen | Fotografen. Datei-Drop reicht nicht, weil man dort keine Ordner anlegen kann. |
| `Fotos/Öffentlich/<Termin>` | Öffentlicher Link ohne Passwort | Nur lesen | z. B. Galerie-Link in einem Website-Beitrag |

Bilder für die Öffentlichkeit nach `Fotos/Öffentlich/<Termin>` **kopieren**, nicht verschieben. Vorher prüfen: keine Nahaufnahmen ohne Einwilligung, keine Minderjährigen ohne Einwilligung der Eltern, der Fotograf hat nicht widersprochen.

## 2. Docker-Host vorbereiten

### 2A. Synology

1. **Archiv:** einen freigegebenen Ordner anlegen, z. B. `Verein`, darin `Fotoarchiv`. Rechte nur für Admins. Pfad merken, etwa `/volume1/Verein/Fotoarchiv`.
2. **Projektordner** `/volume1/docker/gruppix` mit dem Inhalt dieses Repos füllen. Am einfachsten per `git clone <Repo-URL> /volume1/docker/gruppix` (Paket *Git Server* liefert `git`). Alternativ die Dateien über die Freigabe `docker` kopieren, ohne `.git`, `.env`, `data/` und `import/`.
3. Per SSH weiter mit **Abschnitt 3**. Auf der Synology brauchen alle `docker`-Befehle `sudo`.

Im Container Manager braucht es kein eigenes Projekt, die Container erscheinen auch so in der Liste. Wer das Projekt in der Oberfläche sehen will: *Projekt → Erstellen*, Name **exakt** `gruppix`, Pfad `/volume1/docker/gruppix`, *Vorhandene docker-compose.yml verwenden*.

### 2B. Linux-Server bei einem Dienstleister

Ein Server bei einem Dienstleister geht genauso, gruppix braucht nichts Synology-Spezifisches. Für einen Linux-Server gilt:

- **Größe:** 2 GB RAM und 2 vCPU reichen. WAHA mit der Engine GOWS braucht wenig. Videos kurzzeitig auch viel CPU, weil ffmpeg sie umwandelt. Den Speicherplatz bestimmt das Archiv: Pro Jahr fallen je nach Gruppe einige GB an.
- **Netz:** Es muss **kein Port** offen sein. Der Worker baut alle Verbindungen selbst nach außen auf (Nextcloud, Kalender, WhatsApp), der Webhook läuft nur im internen Docker-Netz. Das WAHA-Dashboard wird nur einmal zum Koppeln gebraucht und bleibt mit `WAHA_BIND=127.0.0.1` von außen unerreichbar. Erreichbar ist es dann über einen SSH-Tunnel: `ssh -L 3000:localhost:3000 <user>@<server>`, danach im Browser `http://localhost:3000/dashboard`.
- **Datenschutz:** Die Originale und die Quarantäne liegen dann beim Dienstleister. Nötig sind ein Standort in der EU und ein Auftragsverarbeitungsvertrag (Art. 28 DSGVO), den seriöse Hoster bereitstellen. Den Vorstand vorher informieren.
- **Backup:** Der Server ist kein Backup. Archiv und Datenbank zusätzlich sichern (Abschnitt 8).

Einrichtung:

```bash
# Docker Engine + Compose plugin, e.g. per docs.docker.com; on Ubuntu 24.04 also:
sudo apt install docker.io docker-compose-v2 git
sudo usermod -aG docker $USER                             # danach neu anmelden, dann ohne sudo
git clone <Repo-URL> /srv/gruppix
mkdir -p /srv/gruppix-archive                             # ARCHIVE_PATH
```

Die folgenden Abschnitte zeigen die Befehle mit `sudo` wie auf der Synology. Auf dem Server mit Docker-Gruppe lässt man es weg.

## 3. Konfigurieren

Im Projektordner:

```bash
mkdir -p import data/waha-sessions data/waha-media data/worker    # bind mounts must exist
cp .env.example .env && chmod 600 .env
openssl rand -hex 32    # dreimal: WAHA_API_KEY, HOOK_HMAC_KEY, SENDER_SALT
```

Die `.env` enthält Geheimnisse und gehört nie ins Repo und nie in eine Mail. Speichern immer als UTF-8, damit Umlaute in Werten erhalten bleiben. Alle Einstellungen sind in `.env.example` kommentiert. Diese musst du setzen:

| Einstellung | Inhalt |
| --- | --- |
| `ARCHIVE_PATH` | Archivordner auf dem Host, z. B. `/volume1/Verein/Fotoarchiv` oder `/srv/gruppix-archive`. Ohne Wert startet Compose nicht. |
| `WAHA_BIND` | `127.0.0.1` auf einem Server (Zugriff per SSH-Tunnel), `0.0.0.0` auf einer NAS im LAN |
| `CLUB_NAME` | Vereinsname, wie Konzertmeister ihn an Titel hängt, z. B. `Musikverein Beispielstadt` |
| `WAHA_API_KEY`, `HOOK_HMAC_KEY`, `WAHA_DASHBOARD_PASSWORD` | Zufallswerte und Dashboard-Passwort |
| `SENDER_SALT` | Zufallswert. **Nach dem ersten echten Import nie mehr ändern.** |
| `CALENDAR_ICS_URLS` | iCal-Adressen, durch Komma getrennt (Abschnitt 5) |
| `NC_WEBDAV_URL`, `NC_USER`, `NC_APP_PASSWORD` | aus Abschnitt 1 |
| `SMTP_*` oder `MQTT_*`, `HEALTHCHECK_PING_URL` | Benachrichtigungen (Abschnitt 8), dringend empfohlen |

Die Zuordnungsregeln haben Vorgaben, die sich im Betrieb bewährt haben:

| Einstellung | Wirkung |
| --- | --- |
| `WINDOW_PRE_HOURS`, `WINDOW_POST_HOURS` | Bilder von Beginn − x h bis Ende + y h gehören zum Termin |
| `LATE_GRACE_DAYS`, `LATE_MIN_BATCH`, `LATE_BATCH_GAP_MINUTES` | Nachgereichte Pakete (≥ 3 Bilder eines Absenders am Stück) zählen noch bis x Tage danach |
| `MIN_EVENT_ITEMS` | Ein Terminordner entsteht erst ab so vielen Bildern, einzelne Memes bleiben draußen |
| `DAY_CUTOFF_HOUR` | Tagesgrenze. Bis zu dieser Uhrzeit zählt die Nacht zum Vortag, das gilt für ganztägige Termine, Admin-Ordner und Häufungen. |
| `EVENT_EXCLUDE_REGEX` | Termine ohne Fotos (Proben, Sitzungen, Beerdigungen …) |
| `EVENT_SIDE_REGEX`, `SIDE_EVENT_GAP_HOURS` | Aufbau und Abbau direkt neben einem Fest zählen zum Fest |
| `ALL_DAY_MAX_DAYS` | Längere ganztägige Einträge (Ferien) werden ignoriert |
| `QUARANTINE_DAYS` | Bilder ohne Termin werden danach gelöscht |
| `CLUSTER_*` | Ab wann Bilder ohne Termin als Häufung nach `Fotos-Eingang/` kommen |

Wer die Werte ändert, passt die Zahlen in `docs/mitglieder-info.md` an, denn die Mitglieder lesen sie dort.

## 4. Starten und Befehle

```bash
cd <Projektordner>
sudo docker compose build worker
sudo docker compose up -d                          # WAHA and worker as services
sudo docker compose ps                             # both "Up", worker "(healthy)" after ~1 min
sudo docker compose logs --tail 20 worker          # "Service started: …"
```

Nach jeder Änderung an `.env` oder `docker-compose.yml` brauchen die Container ein `sudo docker compose up -d --force-recreate worker` (bzw. `waha`).

**Einzelbefehle immer per `exec` im laufenden Worker.** `docker compose run` würde für jeden Befehl einen kurzlebigen Container starten, und die Synology meldet bei jedem davon „Container unerwartet beendet“. `exec` läuft im bestehenden Container, ist schneller und nutzt dieselbe Sperre wie der Dienst. Am bequemsten mit einem Alias in `~/.profile`:

```bash
alias gx='sudo docker compose -f <Projektordner>/docker-compose.yml exec -T worker python -m gruppix'
gx status
gx notify-test
```

Lange Läufe wie ein großer Import starten im Hintergrund und schreiben in eine Logdatei. Die SSH-Sitzung darf danach geschlossen werden:

```bash
sudo docker compose exec -d worker sh -c 'python -m gruppix import-export "/import/<Export>.zip" > /data/import.log 2>&1'
tail -f data/worker/import.log                     # Ctrl+C only stops watching
```

Ein `up -d`, `restart` oder Update bricht einen laufenden Hintergrundbefehl ab. Ein erneuter Start ist unschädlich, weil bekannte Bilder übersprungen werden.

| Befehl (`gx …`) | Zweck |
| --- | --- |
| `status` | Anzahl je Zustand und je Ordner |
| `notify-test` | Testnachricht über alle Kanäle |
| `calendar --from JJJJ-MM-TT --to JJJJ-MM-TT` | Termine und ihre Fotofenster nach allen Regeln |
| `import-export "/import/<Export>.zip" [--since …] [--until …] [--dry-run /data/dryrun]` | WhatsApp-Export importieren |
| `import-drop` | Fotografen-Uploads sofort abholen |
| `quarantine [--csv /data/q.csv]` | Quarantäne und zurückgehaltene Bilder, schreibt die Übersicht `quarantine.html` |
| `release --sheet <Stand> 3,7,12` | Angehakte Bilder der Übersicht in die Galerie (wie 📸); die Seite zeigt den Befehl |
| `release <Datei> …` | Einzelne Bilder per Dateiname in die Galerie |
| `find-duplicates` | Bilder, die doppelt in der Galerie stehen, schreibt die Übersicht `duplicates.html` |
| `find-duplicates --delete <Stand> [--keep-both 3,17]` | Doppelte Kopien aus der Nextcloud löschen; die Seite zeigt den Befehl |
| `backfill-fingerprints`, `retry-uploads`, `purge-quarantine`, `promote-clusters` | Wartung, macht der Dienst selbst |

Den Zustand des Dienstes zeigt `/health`:

```bash
sudo docker compose exec -T worker python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/health').read().decode())"
```

## 5. Kalenderquellen

`CALENDAR_ICS_URLS` nimmt mehrere iCal-Adressen. Bei Dubletten gewinnt ein Eintrag mit Uhrzeit vor einem ganztägigen, sonst die zuerst genannte Quelle.

- **Google:** *Einstellungen → <Kalender> → Kalender integrieren → Privatadresse im iCal-Format*. Nicht der Einbettungslink (`…/embed?src=…`), denn der liefert eine Webseite. Die Adresse ist ein Geheimnis.
- **Konzertmeister:** das Kalender-Abo eines Kontos, das zu allen Terminen eingeladen ist. Titel wie `(+) Maifest (<CLUB_NAME>)` werden zu `Maifest` bereinigt.
- **Ausschluss:** `EVENT_EXCLUDE_REGEX` verhindert Fotofenster für Proben, Sitzungen und Ähnliches.
- **Aufbau/Abbau** neben einem Fest werden eingerechnet. Es entsteht ein Ordner mit dem Datum des Festes.
- **Ganztägige Termine** gelten von `DAY_CUTOFF_HOUR` bis `DAY_CUTOFF_HOUR` am Folgetag.

Kontrolle mit `gx calendar --from 2026-01-01 --to 2026-12-31`.

## 6. Bestand importieren (WhatsApp-Export)

1. **Export:** In der Gruppe *Chat exportieren → Medien einbeziehen*, die ZIP-Datei nach `import/` kopieren.
   - Am besten exportiert ein Mitglied, das viele Bilder **empfangen** hat. Beim Absender liegen eigene Bilder in anderer Form vor, etwa HD in zwei Größen. Das erkennt der Worker aber als Duplikat.
2. **Trockenlauf.** Er lädt nichts hoch und schreibt alles nach `data/worker/dryrun/`:
   ```bash
   sudo docker compose exec -d worker sh -c 'python -m gruppix import-export "/import/<Export>.zip" --dry-run /data/dryrun > /data/import.log 2>&1'
   ```
   - In `dryrun/nextcloud/` steht, was hochgeladen würde, in `dryrun/data/staging/` die Quarantäne.
   - Einstellungen anpassen, `data/worker/dryrun/` löschen und wiederholen.
3. **Probe-Import:** echt, aber nur ein Termin samt den Tagen danach:
   ```bash
   gx import-export "/import/<Export>.zip" --since 2026-07-11 --until 2026-07-15
   ```
4. **Vollimport:** wie in Abschnitt 4 im Hintergrund, ohne `--since`/`--until`. Danach die ZIP-Datei löschen, denn sie enthält den ganzen Chat.

Ein wiederholter Import schadet nicht. Bekannte Bilder werden übersprungen, auch wenn sie als andere Datei vorliegen (über Fingerabdruck und Sendezeit). Per ❌ gelöschte Bilder kommen nicht zurück. Der Dienst selbst schaut nie in `import/`, Importe startet man bewusst. Bei US-Datumsformat im Export `--date-order mdy` angeben.

## 7. WhatsApp-Bot

Vorher lesen: Risiken und Betriebsaufwand der Live-Funktion stehen im [README](../README.md#live-funktion-whatsapp-bot). Es ist keine offizielle Schnittstelle, die Nummer kann gesperrt werden, und das Handy läuft im Dauerbetrieb.

### Bot-Handy

- WhatsApp Business mit der Bot-Nummer einrichten und eine Zwei-Schritt-PIN setzen. Kein Root und kein privates Google-Konto nötig.
- Das Handy muss mindestens alle 14 Tage online sein, sonst meldet WhatsApp das verknüpfte Gerät ab. Am besten bleibt es im WLAN und am Ladegerät, mit Ladebegrenzung oder einer schaltbaren Steckdose, auf nicht brennbarer Unterlage.
- Die Akku-Optimierung für WhatsApp ausschalten.

### Koppeln und testen

1. Dashboard öffnen: `http://<Host>:3000/dashboard`, auf einem Server über den SSH-Tunnel `http://localhost:3000/dashboard`.
   - Anmeldung mit `admin` und dem `WAHA_DASHBOARD_PASSWORD`.
   - Server-URL `http://<Host>:3000`, API-Key `WAHA_API_KEY`.
   - Dann die Sitzung `default` starten.
2. Auf dem Bot-Handy: *⋮ → Verknüpfte Geräte → Gerät hinzufügen*, den QR-Code scannen. Der Status wird `WORKING`.
3. **Testgruppe:** vom eigenen Handy anlegen, die Bot-Nummer als normales Mitglied hinzufügen und eine Nachricht schicken.
4. Die Gruppen-ID aus `/health` ablesen (`webhooks.groups`, endet auf `@g.us`). Unter `webhooks.signatures` darf nur `ok` stehen, sonst passt `HOOK_HMAC_KEY` nicht.
5. In der `.env` setzen: `GROUP_CHAT_ID=<Testgruppe>` und `LIVE_INGEST=on`. Dann `sudo docker compose up -d --force-recreate worker`. Im Protokoll erscheint `Live intake started for …@g.us`.
6. Alles aus der Tabelle unten in der Testgruppe ausprobieren.

### Was die Gruppe kann

| In der Gruppe | Wirkung |
| --- | --- |
| Foto oder Video posten | Zuordnung zum Termin, mit Mindestmenge, sonst Quarantäne |
| Foto **als Dokument** | Volle Qualität mit Aufnahmezeit. HD bringt nichts, WhatsApp liefert davon nur die Standardgröße. |
| 📸 auf ein Bild (jeder) | Kommt in die Galerie, auch aus der Quarantäne |
| ❌ auf ein Bild (Absender oder Gruppen-Admin) | Wird überall gelöscht: Nextcloud, Archiv, Quarantäne |
| *Für alle löschen* | Wird überall gelöscht |
| Reaktion wieder entfernen | Keine Wirkung, gelöscht bleibt gelöscht |

📸 und ❌ wirken nur auf Bilder, die der Bot selbst empfangen hat. Ältere Bilder aus Exporten bearbeitet man mit `release` oder direkt in der Nextcloud.

**Ordner ohne Kalendereintrag (nur Gruppen-Admins):** dem Bot eine Direktnachricht schicken.

| Nachricht | Wirkung |
| --- | --- |
| `Ordner Grillabend` | Ordner `Fotos/<Jahr>/<heute> Grillabend`. Bis `DAY_CUTOFF_HOUR` früh zählt noch der Vortag. |
| `Ordner gestern Grillabend` | dasselbe für gestern |
| `Ordner 2026-07-11 Grillabend` oder `Ordner 11.07.2026 Grillabend` | für dieses Datum, bis 30 Tage zurück und 90 voraus |

**Opt-out (alle Mitglieder):** Eine Direktnachricht `Stopp` an den Bot sorgt dafür, dass neue Bilder dieser Person nicht mehr übernommen werden, mit `Start` gilt das wieder. Der Bot bestätigt beides. Das wirkt nur auf die Live-Übernahme, nicht auf Export-Importe, weil Exporte Absender nur als Namen kennen. Bereits übernommene Bilder entfernt ein Admin von Hand.

Der Tag wird wie ein Termin behandelt, aber ohne Mindestmenge. Passende Bilder aus der Quarantäne kommen sofort dazu. Der Bot antwortet dem Admin mit Ordnername und Anzahl. Nachrichten anderer an den Bot werden ignoriert und nicht gespeichert.

### Bot-Nachricht bei neuen Ordnern (optional)

Mit `ANNOUNCE=on` schreibt der Bot einmal pro Termin in die Gruppe, sobald dessen Ordner `ANNOUNCE_MIN_ITEMS` Bilder hat. Er nennt Titel, Anzahl und Link und ruft zum Nachreichen auf.
- Das gilt nur für die Live-Übernahme, nie für Importe, Fotografen-Uploads oder Termine, die älter als `ANNOUNCE_MAX_AGE_DAYS` sind.
- In der Ruhezeit (`ANNOUNCE_QUIET_HOURS`, Standard 22 bis 9 Uhr) wartet die Nachricht bis zum Morgen.
- Der Link zeigt auf den Terminordner in der Mitglieder-Freigabe. Das Passwort steht nicht darin.

`GALLERY_FOLDER_URL` ist der Freigabe-Link mit Platzhalter, z. B. `https://<cloud>/s/<Token>?dir={path}` (mit Nextcloud getestet). Falls der Link nicht im Unterordner landet: im Browser einen Unterordner der Freigabe öffnen und den Parameternamen aus der Adresszeile übernehmen. In der Testgruppe ausprobieren, zum Beispiel mit `Ordner heute Test` und drei Bildern bei `ANNOUNCE_MIN_ITEMS=3`.

Jede Nachricht des Bots erhöht das Sperrrisiko etwas, siehe README. Deshalb ist es eine einzige Nachricht pro Termin.

### Live-Start in der echten Gruppe

1. Ein Admin der Gruppe nimmt die Bot-Nummer auf. Alle sehen „… hat Gruppix hinzugefügt“, deshalb die Mitglieder-Info (`docs/mitglieder-info.md`) gleich danach verschicken.
2. Nach der ersten Nachricht die Gruppen-ID aus `/health` übernehmen: `GROUP_CHAT_ID=<Gruppe>`, dann `up -d --force-recreate worker`. Ab jetzt läuft die Übernahme live. Ältere Nachrichten sieht der Bot nicht.
3. Die Lücke seit dem letzten Export schließen. Dazu neu exportieren und importieren, mit `--since <Datum des letzten Exports>`. Doppelte werden übersprungen.

Texte und Bildunterschriften werden nie gespeichert, Absender nur als Hash. Die Testgruppe wird danach ignoriert, ihre Bilder in der Quarantäne verfallen.

## 8. Betrieb

**Eingebauter Zeitplan.** Aufgabenplaner, Cron oder Ähnliches sind nicht nötig.

| Aufgabe | Takt | Einstellung |
| --- | --- | --- |
| Fotografen-Uploads abholen, wenn ein Ordner `DROP_QUIET_MINUTES` ruhig war; zurückgehaltene Bilder auflösen | alle 15 min | `DROP_INTERVAL_MINUTES` |
| Fehlgeschlagene Uploads wiederholen | stündlich | `RETRY_INTERVAL_MINUTES` |
| Häufungen in den Eingang, Quarantäne löschen, Fingerabdrücke nachtragen, Kalender und Speicher prüfen | täglich 04:00 | `MAINTENANCE_HOUR` |
| WhatsApp-Sitzung prüfen | alle 15 min | – |
| Übersichten Duplikate/Quarantäne erneuern, Wochenbericht | sonntags 18:00 | `REPORT_WEEKDAY`, `REPORT_HOUR` |

**Fotografen-Upload.** Die Fotografen bekommen den Upload-Link mit eigenem Passwort. Pro Termin legen sie einen Ordner an, der mit dem Datum beginnt (`2026-07-11 Sommerfest` oder `11.07.2026 …`).

Der Worker sucht den Termin in dieser Reihenfolge:
1. Termin am Ordnerdatum. Ein abweichender Name ist dann egal.
2. Termin zur Kamerazeit.
3. Termin mit ähnlichem Namen einen Tag davor oder danach.
4. Sonst bekommt der Ordner einen eigenen Galerieordner mit dem Ordnernamen.

Unterordner werden flach übernommen. Upload-Ordner ohne Datum landen in `Fotos-Eingang/`.

**Benachrichtigungen**, alle optional. Jedes Problem wird einmal beim Auftreten gemeldet und einmal, wenn es behoben ist.

- *E-Mail:* über einen SMTP-Zugang, am besten ein Vereinspostfach. In `SMTP_TO` einen zweiten Admin eintragen. Die eingebaute Mail-Funktion einer NAS lässt sich aus dem Container nicht nutzen.
- *Home Assistant:* `MQTT_HOST` ist die LAN-Adresse des Brokers. Das Gerät „Gruppix“ erscheint automatisch. Push-Automation:
  ```yaml
  alias: gruppix – Meldung aufs Handy
  triggers:
    - trigger: mqtt
      topic: gruppix/alert          # prefix = MQTT_TOPIC_PREFIX
  actions:
    - action: notify.mobile_app_DEIN_HANDY
      data:
        title: "{{ trigger.payload_json.title }}"
        message: "{{ trigger.payload_json.message }}"
  ```
- *healthchecks.io:* Totmann-Schalter mit Periode 15 min und Karenz 30 min, die URL kommt in `HEALTHCHECK_PING_URL`. Der Worker pingt alle 5 min, solange er läuft.

**Container-Status:** „unhealthy“ nur bei Fehlern des Workers selbst, also wenn eine Aufgabe dreimal fehlschlägt oder der Dienst hängt. Äußere Störungen (WAHA, Speicher, Kalender) sind Warnungen und kommen per Mail oder MQTT.

**Kurze Clips und GIFs:** Clips bis `CLIP_AS_PHOTO_SECONDS` (3 s), also Live- und Bewegungsfotos, erscheinen als Standbild zwischen den Fotos. Der Clip selbst liegt im Archiv daneben. WhatsApp-GIFs aus der GIF-Suche verwirft die Live-Übernahme, weil das fast immer Memes sind. Im Export lassen sie sich nicht erkennen und werden dort wie kurze Clips behandelt.

**Lange Videos:** Videos über `VIDEO_MAX_SECONDS` (3 min) kommen nicht in die Galerie, sondern umgewandelt nach `Fotos-Eingang/` in denselben Terminordner. Ein Admin verschiebt sie bei Bedarf nach `Fotos/`. Über `VIDEO_INBOX_MAX_SECONDS` (30 min) wird nichts umgewandelt. Das Original liegt in jedem Fall im Archiv unter `_Eingang/`.

**Quarantäne:** Sie liegt nur auf dem Host unter `data/worker/staging/<Monat>/`, mit lesbaren Dateinamen, und wird nach `QUARANTINE_DAYS` gelöscht. Einzelne Bilder holt `gx release <Dateiname>` in die Galerie.

**Übersichten zum Durchsehen:** `gx quarantine` und `gx find-duplicates` schreiben je eine HTML-Seite nach `data/worker/` (`quarantine.html`, `duplicates.html`). Der Dienst erneuert beide vor dem Wochenbericht; der Bericht (Mail und Home-Assistant-Sensor „Zu prüfen“) nennt die Anzahl.
- Seite in File Station herunterladen und im Browser öffnen. Die Vorschaubilder stecken in der Datei, sie verlässt den Host nur, wenn man sie selbst weitergibt.
- Ankreuzen: in der Quarantäne „In die Galerie“, bei Duplikaten „Kein Duplikat – beide behalten“. Unten steht der fertige Befehl, z. B. `gx find-duplicates --delete 20261015-171111 --keep-both 3,17`.
- Der Befehl gilt nur für genau diese Liste (Stand im Befehl). Wurde sie inzwischen erneuert, lehnt der Worker ab und man öffnet die neue Seite.
- Duplikate: gelöscht wird nur die kleinere Kopie in der Nextcloud, beide Originale bleiben im Archiv. Als „beide behalten“ markierte Paare werden nicht mehr gemeldet.

**Aufräumen in der Nextcloud:**
- Falsch einsortierte Bilder **verschieben**, nicht löschen. Der Worker fasst sie danach nicht mehr an.
- Terminordner nicht umbenennen, sonst legt der Worker den alten Namen für nachkommende Bilder wieder an.
- Ein in der Nextcloud gelöschtes Bild bleibt im Archiv und kommt nicht zurück.

**Backup:**
- `ARCHIVE_PATH` mit den Originalen
- `data/worker/state.sqlite*` mit Zustand und Ordnernamen
- `data/waha-sessions`, die WhatsApp-Kopplung
- `.env`, nur verschlüsselt sichern

Auf der Synology z. B. mit Hyper Backup, auf einem Server z. B. mit restic oder borg auf einen externen Speicher.

**Updates:**
```bash
cd <Projektordner>
git pull                                   # or copy the new files
sudo docker compose pull waha
sudo docker compose build --pull worker
sudo docker compose up -d
```
Neue Einstellungen stehen in `.env.example`. Schlüssel, die in der eigenen `.env` noch fehlen, zeigt:
```bash
sudo diff <(sudo grep -o '^[A-Z0-9_]*=' .env | sort) <(grep -o '^[A-Z0-9_]*=' .env.example | sort)
```

**Notfall-Abschaltung:**
1. `sudo docker compose down`
2. In der Nextcloud das App-Passwort von `fotobot` widerrufen und gegebenenfalls den Mitglieder-Link löschen.
3. Auf dem Bot-Handy unter *Verknüpfte Geräte* „Gruppix“ abmelden.

## 9. Fehlerbehebung

| Meldung | Ursache | Lösung |
| --- | --- | --- |
| `set ARCHIVE_PATH in .env` | Archivpfad fehlt | `ARCHIVE_PATH` setzen (Abschnitt 3) |
| `bind source path does not exist` | Ordner fehlt | `mkdir -p import data/…` bzw. Archivordner anlegen |
| `Missing required environment variable SENDER_SALT` | `.env` fehlt oder ist leer | `.env` im Projektordner prüfen |
| Synology: „Container unerwartet beendet“ | Befehl mit `run` statt `exec` gestartet | Einzelbefehle per `exec` (Abschnitt 4) |
| `does not deliver iCalendar data` | Einbettungslink statt iCal | iCal-Adresse eintragen (Abschnitt 5) |
| Ordnernamen enden auf `(Vereinsname)` | `CLUB_NAME` fehlt oder weicht ab | Namen exakt wie in Konzertmeister eintragen |
| `MKCOL …: HTTP 401` | App-Passwort oder Benutzer falsch | neues App-Passwort, `NC_USER` prüfen |
| `MKCOL …: HTTP 404` | WebDAV-Adresse falsch | aus den *Dateien-Einstellungen* kopieren |
| `PUT …: HTTP 507` | Kontingent voll | Kontingent erhöhen oder aufräumen |
| `DELETE …: HTTP 423` (Warnung „Upload-Ordner“) | Nextcloud hält eine Dateisperre, meist nach abgebrochenem Upload | Bild ist schon in der Galerie; Sperre läuft nach etwa einer Stunde ab, der Worker löscht dann selbst. Hält sie an: Hoster bitten, hängende Sperren zu löschen |
| `WAHA nicht erreichbar (ReadTimeout)` | Host ausgelastet | wird erst nach 3 Fehlversuchen gemeldet, meist harmlos |
| `WAHA antwortet mit HTTP 401` | API-Schlüssel ungleich | `sudo docker compose up -d --force-recreate waha` |
| `webhooks.signatures` zeigt `invalid` | `HOOK_HMAC_KEY` geändert, aber nur ein Container neu erstellt | beide neu erstellen: `up -d --force-recreate` |
| `another job holds the pipeline lock` | Import oder Dienst arbeitet gerade | warten, wird automatisch wiederholt |
| Dashboard nicht erreichbar | `WAHA_BIND=127.0.0.1` | SSH-Tunnel (Abschnitt 2B) oder im LAN `0.0.0.0` |
| Alles in der Quarantäne | Kalender leer oder Ausschluss zu breit | `gx calendar …` prüfen |
