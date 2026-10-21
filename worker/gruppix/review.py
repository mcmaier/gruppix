"""Review sheets for admins: look-alike photos and the quarantine.

Each sheet is a self-contained HTML page in the data directory (thumbnails
embedded, nothing leaves the host) plus a JSON report with the numbered list.
The page builds the command that acts on the ticked numbers; the command
carries the report's stamp, so it never acts on a newer list than the one
the admin looked at.

- ``duplicates``: pairs from ``Pipeline.similar_pairs``. ``--delete`` removes
  the second copy from Nextcloud (archive untouched); pairs marked
  "keep both" are remembered and never listed again.
- ``quarantine``: quarantined and held items. ``release --sheet`` puts the
  ticked ones into the gallery like a 📸 reaction.

The service refreshes both before the weekly report, which names the counts.
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageOps

from . import jobs, media
from .config import Config
from .db import Database, State
from .pipeline import Pipeline
from .sinks import LocalSink, Sink

log = logging.getLogger(__name__)

DUPLICATES = "duplicates"
QUARANTINE = "quarantine"
SOURCE_LABEL = {"export": "Export", "waha": "Live", "drop": "Fotograf"}
_DUP_EDGE = 480
_QUARANTINE_EDGE = 320


class ReportError(RuntimeError):
    """The command does not fit the current report (missing, newer, unknown number)."""


@dataclass(frozen=True)
class Summary:
    duplicates: int
    quarantine: int


def _paths(cfg: Config, kind: str) -> tuple[Path, Path]:
    return cfg.data_dir / f"{kind}.json", cfg.data_dir / f"{kind}.html"


def _stamp(cfg: Config) -> str:
    return datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")


def _write(cfg: Config, kind: str, stamp: str, items: list[dict], page: str | None) -> None:
    report, sheet = _paths(cfg, kind)
    if not items:
        report.unlink(missing_ok=True)
        sheet.unlink(missing_ok=True)
        return
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"stamp": stamp, "items": items}, indent=1), encoding="utf-8")
    sheet.write_text(page or "", encoding="utf-8")


def _load(cfg: Config, kind: str, stamp: str, numbers: set[int]) -> list[dict]:
    report, _ = _paths(cfg, kind)
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportError(f"no {kind} list – create it first, check it, then run the command from the page") from exc
    if data["stamp"] != stamp:
        raise ReportError(f"the {kind} list was renewed ({data['stamp']}) – open the current page and copy its command")
    unknown = sorted(numbers - {item["n"] for item in data["items"]})
    if unknown:
        raise ReportError(f"no number {', '.join(map(str, unknown))} in the {kind} list")
    return data["items"]


def _done(cfg: Config, kind: str) -> None:
    for path in _paths(cfg, kind):
        path.unlink(missing_ok=True)


def _pipeline(cfg: Config, db: Database, gallery: Sink | None = None) -> Pipeline:
    return Pipeline(cfg, db, jobs.empty_calendar(cfg), gallery or LocalSink(cfg.outbox_dir))


def refresh(cfg: Config) -> Summary:
    """Renew both lists (weekly, before the report)."""
    return Summary(len(duplicates_report(cfg)[1]), len(quarantine_report(cfg)[1]))


# --- duplicates ----------------------------------------------------------------------


def duplicates_report(cfg: Config) -> tuple[str, list[tuple[int, object, object, int]]]:
    """Numbered (n, keep, extra, distance) pairs; writes duplicates.json/.html."""
    db = Database(cfg.db_path)
    try:
        found = _pipeline(cfg, db).similar_pairs()
    finally:
        db.close()
    pairs = [(n, keep, extra, d) for n, (keep, extra, d) in enumerate(found, start=1)]
    stamp = _stamp(cfg)
    items = [{"n": n, "keep": k["id"], "extra": e["id"], "distance": d} for n, k, e, d in pairs]
    _write(cfg, DUPLICATES, stamp, items, _duplicates_page(cfg, stamp, pairs) if pairs else None)
    return stamp, pairs


def delete_duplicates(cfg: Config, gallery: Sink, stamp: str, keep_both: set[int]) -> tuple[int, int, int]:
    """Remove second copies of the listed pairs. Returns (removed, kept both, unchanged)."""
    items = _load(cfg, DUPLICATES, stamp, keep_both)
    removed = kept = unchanged = 0
    with jobs.pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            pipeline = _pipeline(cfg, db, gallery)
            for item in items:
                if item["n"] in keep_both:
                    db.add_distinct_pair(item["keep"], item["extra"])
                    kept += 1
                elif db.get(item["keep"])["state"] not in State.PUBLISHED:
                    unchanged += 1  # never leave the gallery without any copy
                elif pipeline.drop_duplicate(item["extra"], item["keep"]) == State.DUPLICATE:
                    removed += 1
                else:
                    unchanged += 1
        finally:
            db.close()
    _done(cfg, DUPLICATES)
    return removed, kept, unchanged


def _duplicates_page(cfg: Config, stamp: str, pairs) -> str:
    cards = []
    for n, keep, extra, distance in pairs:
        folder = (keep["remote_path"] or "").rsplit("/", 1)[0]
        figures = "".join(
            _figure(cfg, row, role, label, _DUP_EDGE)
            for row, role, label in ((keep, "keep", "Behalten"), (extra, "drop", "Löschen"))
        )
        cards.append(
            f'<section class="card" data-n="{n}"><header><span class="n">{n}</span>'
            f'<span class="grow">{html.escape(folder)}</span>'
            f'<span class="muted" title="Unterschied im Bildvergleich, 0 = gleich">{distance} von 64 Bit</span>'
            f'</header><div class="pair">{figures}</div>'
            f'<label class="tick"><input type="checkbox" value="{n}"> Kein Duplikat – beide behalten</label>'
            f'</section>'
        )
    lead = (
        f"{len(pairs)} Paare. Links die Datei, die bleibt (die größere), rechts die Kopie, die aus der "
        "Nextcloud gelöscht wird. Ist es kein Duplikat, das Häkchen setzen – das Paar wird dann nicht "
        "mehr gemeldet. Die Originale im Archiv bleiben in jedem Fall erhalten."
    )
    command = {"base": f"gx find-duplicates --delete {stamp}", "flag": " --keep-both ", "empty": ""}
    return _page(cfg, "Duplikate prüfen", lead, "\n".join(cards), command, "both")


# --- quarantine ----------------------------------------------------------------------


def quarantine_report(cfg: Config) -> tuple[str, list[tuple[int, object]]]:
    """Numbered (n, row) items in quarantine or held; writes quarantine.json/.html."""
    db = Database(cfg.db_path)
    try:
        rows = _pipeline(cfg, db).quarantined()
    finally:
        db.close()
    items = [(n, row) for n, row in enumerate((r for r in rows if r["staged_path"]), start=1)]
    stamp = _stamp(cfg)
    _write(cfg, QUARANTINE, stamp, [{"n": n, "id": r["id"]} for n, r in items],
           _quarantine_page(cfg, stamp, items) if items else None)
    return stamp, items


def release_from_sheet(cfg: Config, gallery: Sink, stamp: str, numbers: set[int]) -> dict[int, str]:
    """Put the ticked items into the gallery. Returns number -> new state."""
    if not numbers:
        raise ReportError("no numbers given")
    items = {item["n"]: item["id"] for item in _load(cfg, QUARANTINE, stamp, numbers)}
    results: dict[int, str] = {}
    with jobs.pipeline_lock(cfg):
        db = Database(cfg.db_path)
        try:
            pipeline = _pipeline(cfg, db, gallery)
            for n in sorted(numbers):
                results[n] = pipeline.pin(items[n])
        finally:
            db.close()
    return results  # the list stays valid: more numbers may follow


def _quarantine_page(cfg: Config, stamp: str, items) -> str:
    days: dict[str, list[str]] = {}
    for n, row in items:
        sent = datetime.fromtimestamp(row["msg_ts"], cfg.tz)
        if row["state"] == State.HELD:
            note = "zurückgehalten: " + (row["pending_dir"] or "").rsplit("/", 1)[-1]
        else:
            purge = datetime.fromtimestamp(row["updated_at"], cfg.tz) + timedelta(days=cfg.quarantine_days)
            note = f"wird gelöscht am {purge:%d.%m.}"
        caption = (
            f'<b>{n}</b> · {sent:%H:%M} · {SOURCE_LABEL.get(row["source"], row["source"])} · '
            f'Absender {row["sender_hash"][:6]}<br>{html.escape(note)}'
        )
        thumb = _thumb_tag(row["staged_path"], row["kind"], _QUARANTINE_EDGE)
        days.setdefault(f"{sent:%A, %d.%m.%Y}", []).append(
            f'<label class="tile card" data-n="{n}">{thumb}<span class="cap">{caption}</span>'
            f'<span class="tick"><input type="checkbox" value="{n}"> In die Galerie</span></label>'
        )
    body = "\n".join(
        f'<h2>{html.escape(_german_day(day))}</h2><div class="grid">{"".join(tiles)}</div>'
        for day, tiles in days.items()
    )
    lead = (
        f"{len(items)} Bilder und Videos ohne passenden Termin oder mit zu wenigen Bildern für ihren Termin. "
        "Angehakte kommen wie bei 📸 in die Galerie: zurückgehaltene in ihren Terminordner, die übrigen "
        f"nach Fotos-Eingang (Ordner „Markiert“). Alles andere wird nach {cfg.quarantine_days} Tagen gelöscht. "
        "Gleicher Absender-Code = gleiche Person."
    )
    command = {"base": f"gx release --sheet {stamp}", "flag": " ", "empty": " <Nummern>"}
    return _page(cfg, "Quarantäne prüfen", lead, body, command, "pick")


_DAYS = {"Monday": "Montag", "Tuesday": "Dienstag", "Wednesday": "Mittwoch", "Thursday": "Donnerstag",
         "Friday": "Freitag", "Saturday": "Samstag", "Sunday": "Sonntag"}


def _german_day(text: str) -> str:
    name, rest = text.split(",", 1)
    return _DAYS.get(name, name) + "," + rest


# --- shared page parts ---------------------------------------------------------------


def _thumbnail(path: str | None, kind: str, edge: int) -> tuple[str | None, str]:
    """Data URI of a small JPEG and a size label."""
    if not path or not Path(path).exists():
        return None, "Datei nicht gefunden"
    source = Path(path)
    frame = None
    try:
        if kind == "video":
            frame = Path(tempfile.mkstemp(suffix=".jpg")[1])
            media.extract_still(source, frame, 0.5)
            source = frame
        with Image.open(source) as im:
            width, height = im.size
            im.draft("RGB", (edge, edge))
            img = ImageOps.exif_transpose(im).convert("RGB")
            img.thumbnail((edge, edge))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
    except Exception as exc:  # noqa: BLE001 - show the entry without picture
        log.debug("no thumbnail for %s: %s", path, exc)
        return None, "Vorschau nicht möglich"
    finally:
        if frame is not None:
            frame.unlink(missing_ok=True)
    kib = Path(path).stat().st_size / 1024
    weight = f"{kib / 1024:.1f} MB" if kib >= 1024 else f"{kib:.0f} KB"
    label = ("Video · " if kind == "video" else "") + f"{width} × {height} px · {weight}"
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), label


def _thumb_tag(path: str | None, kind: str, edge: int) -> str:
    uri, label = _thumbnail(path, kind, edge)
    img = f'<img src="{uri}" alt="" loading="lazy">' if uri else '<span class="noimg">kein Bild</span>'
    badge = '<span class="badge">Video</span>' if kind == "video" else ""
    return f'<span class="frame">{img}{badge}</span><span class="muted size">{html.escape(label)}</span>'


def _figure(cfg: Config, row, role: str, label: str, edge: int) -> str:
    sent = datetime.fromtimestamp(row["msg_ts"], cfg.tz).strftime("%d.%m.%Y %H:%M")
    name = (row["remote_path"] or "").rsplit("/", 1)[-1]
    return (
        f'<figure class="{role}">{_thumb_tag(row["archive_path"], row["kind"], edge)}<figcaption>'
        f'<b>{label}</b> · {SOURCE_LABEL.get(row["source"], row["source"])} · gesendet {sent}'
        f'<br><code>{html.escape(name)}</code></figcaption></figure>'
    )


def _page(cfg: Config, title: str, lead: str, body: str, command: dict[str, str], mode: str) -> str:
    created = datetime.now(cfg.tz).strftime("%d.%m.%Y %H:%M")
    return (
        _PAGE.replace("{{title}}", html.escape(title))
        .replace("{{lead}}", html.escape(lead))
        .replace("{{created}}", created)
        .replace("{{body}}", body)
        .replace("{{command}}", html.escape(json.dumps(command)))
        .replace("{{mode}}", mode)
    )


_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{title}}</title>
<style>
:root { --bg:#f6f6f4; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e2e2dd;
        --keep:#2f7d4f; --drop:#b4462f; --accent:#24527a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#171716; --card:#222220; --text:#ececea; --muted:#a3a39e; --line:#363633;
          --keep:#5fbf86; --drop:#e0785f; --accent:#7fb2e0; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px 16px 150px; }
h1 { font-size:22px; margin:8px 0 4px; }
h2 { font-size:16px; margin:28px 0 10px; }
.lead { color:var(--muted); margin:0 0 20px; max-width:75ch; }
.muted { color:var(--muted); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
section.card { margin:0 0 16px; }
.card header { display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; margin-bottom:10px; }
.n { font-weight:700; font-size:18px; }
.grow { flex:1; color:var(--muted); overflow-wrap:anywhere; }
.pair { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
@media (max-width:600px) { .pair { grid-template-columns:1fr; } }
figure { margin:0; border-top:4px solid var(--keep); padding-top:8px; }
figure.drop { border-color:var(--drop); }
.card.on figure.drop { border-color:var(--keep); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; }
.tile { display:flex; flex-direction:column; gap:6px; cursor:pointer; }
.tile.on { outline:3px solid var(--keep); outline-offset:-1px; }
.frame { position:relative; display:block; }
.frame img, .noimg { display:grid; place-items:center; width:100%; aspect-ratio:4/3; object-fit:contain;
                     background:var(--bg); border-radius:6px; color:var(--muted); }
.badge { position:absolute; left:6px; top:6px; background:rgba(0,0,0,.6); color:#fff; font-size:12px;
         padding:1px 6px; border-radius:4px; }
.size, figcaption, .cap { font-size:13px; }
figcaption, .cap { color:var(--muted); }
figcaption b, .cap b { color:var(--text); }
code { font:12px ui-monospace, Consolas, monospace; overflow-wrap:anywhere; }
.tick { display:inline-flex; gap:8px; align-items:center; margin-top:4px; cursor:pointer; }
section .tick { margin-top:12px; }
.card.both { opacity:.55; }
.bar { position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--line); }
.bar div { max-width:1100px; margin:0 auto; padding:12px 16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.bar #cmd { flex:1; min-width:240px; font-size:14px; padding:8px 10px; border:1px solid var(--line);
            border-radius:6px; background:var(--bg); }
.bar button { font:inherit; padding:8px 14px; border:0; border-radius:6px; background:var(--accent); color:#fff; cursor:pointer; }
.bar small { width:100%; color:var(--muted); }
</style></head>
<body data-command="{{command}}" data-mode="{{mode}}"><main>
<h1>{{title}}</h1>
<p class="lead">{{lead}} <span class="muted">Stand {{created}}.</span></p>
{{body}}
</main>
<div class="bar"><div>
<code id="cmd"></code><button id="copy" type="button">Kopieren</button>
<small>Per SSH ausführen; <code>gx</code> = <code>sudo docker compose -f …/docker-compose.yml exec -T worker python -m gruppix</code>.
Gilt nur für diese Liste; nach einer neuen Liste die neue Seite öffnen.</small>
</div></div>
<script>
const command = JSON.parse(document.body.dataset.command);
const mode = document.body.dataset.mode;
const boxes = [...document.querySelectorAll('input[type=checkbox]')];
const cmd = document.getElementById('cmd');
function update() {
  const picked = boxes.filter(b => b.checked).map(b => b.value);
  boxes.forEach(b => b.closest('.card').classList.toggle(mode === 'both' ? 'both' : 'on', b.checked));
  cmd.textContent = command.base + (picked.length ? command.flag + picked.join(',') : command.empty);
}
boxes.forEach(b => b.addEventListener('change', update));
document.getElementById('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(cmd.textContent); }
  catch (e) { const r = document.createRange(); r.selectNodeContents(cmd);
              const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
});
update();
</script>
</body></html>
"""
