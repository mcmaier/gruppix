# gruppix

**Gruppe + Pics:** Fotos aus der WhatsApp-Gruppe des Vereins landen automatisch, nach Terminen sortiert, in einer Nextcloud-Galerie für Mitglieder. Die Originale werden auf dem Docker-Host (NAS oder Server) archiviert.

Mitglieder posten wie gewohnt in der Gruppe. gruppix ordnet jedes Bild über den Vereinskalender einem Termin zu, sortiert einzelne Bilder und Privates aus, entfernt Standortdaten und legt eine verkleinerte Kopie in die Galerie.

> **Vor dem Einsatz lesen:** Die Live-Übernahme nutzt eine **inoffizielle WhatsApp-Schnittstelle**; die Bot-Nummer kann gesperrt werden ([Live-Funktion](#live-funktion-whatsapp-bot)). Ohne sie arbeitet gruppix mit dem Export-Import. Für den Datenschutz ist der Verein verantwortlich, der gruppix betreibt ([Datenschutz](#datenschutz-und-verantwortung)).

```
WhatsApp-Gruppe ──(Export-ZIP / live über WAHA-Bot) ───────────┐
Kalender (Google, Konzertmeister) ─────────────────────────────┤
Fotografen (Nextcloud-Upload-Ordner) ──────────────────────────┤
                                                               ▼
                                   ┌────────── Worker (Docker-Host) ───────────┐
                                   │ zuordnen · bereinigen · verkleinern       │
                                   └──────┬───────────────────────┬────────────┘
                                          ▼                       ▼
                              Archiv (Host, Originale)  Nextcloud: Fotos/ (Mitglieder)
                                                                   Fotos-Eingang/ (Admins)
```

## Stand

| Phase | Inhalt | Status |
| --- | --- | --- |
| 1 | Export-Import, Kalenderzuordnung, Verarbeitung, Archiv, Upload, Fotografen-Upload, Dienst mit Zeitplan, Benachrichtigungen | fertig |
| 2 | Live-Bot über WAHA: Empfang per Webhook, Reaktionen ❌/📸, Löschungen, Admin-Befehl „Ordner“, Opt-out „Stopp“, Bot-Nachricht pro Termin | fertig (`LIVE_INGEST=on`) |
| – | Übersichtsseiten für Duplikate und Quarantäne, Wochenbericht | fertig |

## Regeln in Kürze

Werte wie in `.env.example` (alle einstellbar).


- **Zeitfenster:** Ein Bild gehört zu einem Termin, wenn es zwischen Beginn − 4 h und Ende + 36 h aufgenommen bzw. gesendet wurde.
- **Nachzügler:** Pakete (≥ 3 Bilder eines Absenders innerhalb von 10 min) bis 4 Tage nach dem Termin zählen noch dazu.
- **Aufbau/Abbau und ganztägige Termine:** Aufbau und Abbau direkt neben einem Fest zählen zum Fest (ein Ordner). Ganztägige Termine gelten von 6 Uhr bis 6 Uhr am Folgetag; Einträge über 4 Tage (Ferien) werden ignoriert.
- **Mindestmenge:** Ein Terminordner entsteht erst ab 3 Bildern; vorher werden sie zurückgehalten. Fotografen-Uploads und mit 📸 markierte Bilder zählen immer.
- **Quarantäne:** Alles andere bleibt 14 Tage auf dem Host und wird dann gelöscht. Häufungen ohne Termin (≥ 6 Bilder, ≥ 2 Absender) kommen zur Prüfung nach `Fotos-Eingang/`.
- **Verarbeitung:** GPS und Kamera-Notizen werden entfernt, das Aufnahmedatum gesetzt; Bilder über 2048 px oder HEIC werden umgewandelt, Videos zu H.264-MP4 (max. 1280 px). Clips bis 3 s (Live-Fotos) werden zum Standbild, WhatsApp-GIFs verworfen. Videos über 3 min kommen zur Prüfung nach `Fotos-Eingang/`, über 30 min nur ins Archiv; das Original bleibt immer erhalten.

## Live-Funktion (WhatsApp-Bot)

Ohne Live-Funktion kommen Bilder aus der Gruppe nur über den Import einer Export-ZIP herein. Fotografen-Upload, Zeitplan und Benachrichtigungen laufen trotzdem. Mit `LIVE_INGEST=on` übernimmt gruppix jedes neue Bild sofort. Erst dann funktionieren 📸, ❌, *Für alle löschen* und der Admin-Befehl „Ordner …“.

**Wie es funktioniert:** Eine eigene Bot-Nummer ist Mitglied der Gruppe. [WAHA](https://waha.devlike.pro/) meldet sich wie WhatsApp Web als *verknüpftes Gerät* dieser Nummer an und reicht neue Nachrichten an den Worker weiter. Der Worker übernimmt nur Fotos und Videos dieser einen Gruppe. Texte speichert er nie, Absender nur als Hash.

**Bevor man das einschaltet:**

- **Keine offizielle Schnittstelle.** WAHA nutzt das Protokoll von WhatsApp Web, nicht die offizielle WhatsApp-Business-API. Das verstößt gegen die Nutzungsbedingungen von WhatsApp. Die Bot-Nummer kann jederzeit gesperrt werden, ohne Vorwarnung und ohne Anspruch auf Entsperrung.
  - Das Risiko ist gering, weil der Bot nur passiv mitliest. Er schreibt nur kurze Antworten an Admins und zeigt sich nie als online.
  - Ausschließen lässt es sich nicht. Deshalb immer eine eigene Nummer verwenden, nie eine private, und den Export-Import als Rückfallweg behalten.
- **Ein Handy im Dauerbetrieb.** Die Bot-Nummer braucht ein eigenes Handy, etwa ein altes Android-Gerät mit WhatsApp Business. Es bleibt das Hauptgerät des Kontos und muss mindestens alle 14 Tage online sein, sonst meldet WhatsApp das verknüpfte Gerät ab. In der Praxis liegt es dauerhaft im WLAN und am Ladegerät, am besten mit Ladebegrenzung und auf nicht brennbarer Unterlage.
- **Gelegentlich neu koppeln.** WhatsApp meldet verknüpfte Geräte ab und zu ab, etwa nach langer Funkstille des Handys, bei Neuinstallation oder Wechsel des Handys, gelegentlich auch ohne erkennbaren Grund. Dann den QR-Code im WAHA-Dashboard neu scannen. gruppix prüft die Verbindung alle 15 Minuten und meldet einen Ausfall per Mail oder MQTT. Was in der Zwischenzeit gepostet wurde, holt ein Export-Import nach.
- **Bot-Nachrichten (optional):** Mit `ANNOUNCE=on` meldet der Bot einmal pro Termin den neuen Ordner samt Link in der Gruppe. Das ist abschaltbar, mit Ruhezeit und ohne Passwort. Jede Nachricht erhöht das Sperrrisiko etwas, deshalb ist es höchstens eine pro Termin.
- **Kein Verlauf.** Der Bot sieht nur Nachrichten ab seinem Beitritt. 📸 und ❌ wirken nur auf Bilder, die er selbst empfangen hat.
- **Transparenz.** Alle Mitglieder sehen, dass „Gruppix“ der Gruppe beitritt. Vorher den Vorstand einbinden und die Mitglieder-Info ([docs/mitglieder-info.md](docs/mitglieder-info.md)) verschicken.

Einrichtung und Live-Start: [docs/setup.md](docs/setup.md), Abschnitt 7.

## Datenschutz und Verantwortung

gruppix verarbeitet Fotos, auf denen Menschen zu sehen sind. Verantwortlich im Sinne der DSGVO ist der Verein, der es betreibt, nicht die Autoren dieses Projekts. Vor dem Einsatz sollte der Verein:

- den **Vorstand** beschließen lassen und die **Mitglieder informieren** (Vorlage: [docs/mitglieder-info.md](docs/mitglieder-info.md)),
- die **Rechtsgrundlage** klären, besonders für die Veröffentlichung einzelner Bilder und für **Kinder und Jugendliche** (Einwilligung der Eltern),
- mit dem Anbieter von Nextcloud bzw. Server einen **Auftragsverarbeitungsvertrag** (Art. 28 DSGVO) schließen; Standort in der EU.

Was gruppix technisch dazu beiträgt:

- Keine Namen oder Telefonnummern, nur ein gesalzener Hash pro Absender. Texte, Bildunterschriften und Sprachnachrichten werden nie gespeichert.
- Standortdaten (GPS) werden vor jedem Upload entfernt.
- Bilder ohne Termin bleiben nur auf dem eigenen Host und werden nach `QUARANTINE_DAYS` gelöscht.
- Mitglieder können Bilder selbst entfernen (❌, *Für alle löschen*) und sich ganz abmelden („Stopp“ an den Bot).
- Die Galerie ist nur mit Passwort erreichbar; veröffentlicht wird nichts automatisch.

Das ist keine Rechtsberatung. Im Zweifel den Datenschutzbeauftragten des Dachverbands oder eine Fachperson fragen.

## Repository

```
docker-compose.yml     WAHA + Worker
.env.example           alle Einstellungen mit Erklärung (Vorlage für .env)
worker/
  Dockerfile
  gruppix/             Python-Paket (Worker)
  tests/               pytest
docs/
  setup.md             Einrichtung NAS, Nextcloud, Kalender, Betrieb
  mitglieder-info.md   Info für die Vereinsmitglieder
CLAUDE.md              Kontext für Claude Code
LICENSE                MIT
```

## Schnellstart

Läuft auf jedem Docker-Host mit Compose v2, z. B. einer Synology mit Container Manager oder einem Linux-Server. Ausführlich in [docs/setup.md](docs/setup.md).

```bash
git clone <Repo-URL> gruppix && cd gruppix
cp .env.example .env && chmod 600 .env       # fill in, incl. ARCHIVE_PATH
mkdir -p import data/waha-sessions data/waha-media data/worker
docker compose build worker
docker compose up -d
docker compose exec -T worker python -m gruppix notify-test
```

## Befehle

Alle im laufenden Dienst als `docker compose exec -T worker python -m gruppix <Befehl>` (auf der Synology mit `sudo`):

| Befehl | Zweck |
| --- | --- |
| `serve` | Dauerdienst (Standard bei `up -d`): Zeitplan, Prüfungen, `/health` |
| `calendar --from JJJJ-MM-TT --to JJJJ-MM-TT` | Termine und ihre Fotofenster anzeigen |
| `import-export "/import/<Export>.zip" [--since JJJJ-MM-TT] [--until JJJJ-MM-TT] [--dry-run /data/dryrun]` | WhatsApp-Export importieren, optional nur ein Zeitraum |
| `import-drop` | Fotografen-Uploads sofort abholen |
| `quarantine [--csv /data/q.csv]` | Quarantäne und zurückgehaltene Bilder auflisten, Übersicht `quarantine.html` mit Vorschau |
| `release --sheet <Stand> 3,7,12` / `release <Datei> …` | Bilder aus der Quarantäne in die Galerie holen (wie 📸) |
| `status` | Anzahl je Zustand und je Ordner |
| `find-duplicates [--delete <Stand> --keep-both 3,17]` | Doppelte Bilder (HD + Standard, Export + Live) mit Übersicht `duplicates.html` auflisten und löschen |
| `backfill-fingerprints` | Fingerabdruck für ältere Bilder nachtragen (macht der Dienst nachts) |
| `notify-test` | Testnachricht über alle Kanäle |
| `retry-uploads`, `purge-quarantine`, `promote-clusters` | Wartung (macht der Dienst selbst) |

## Entwicklung

```bash
cd worker
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt               # ffmpeg muss installiert sein
pytest
```

## Lizenz und Unterstützung

[MIT-Lizenz](LICENSE): Nutzung, Änderung und Weitergabe sind frei, **ohne Gewähr**. gruppix ist für einen Verein entstanden und wird nach dessen Bedarf weiterentwickelt. Fehlerberichte und Verbesserungen sind willkommen; Hilfe bei der Einrichtung kann ich nicht zusagen.
