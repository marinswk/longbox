"""Admin / portability routes — back up the whole library to a zip
(JSON + cover files), or restore from a previously-exported file.

Endpoints:

  GET  /admin                — admin landing page.
  GET  /api/backup           — streams a zip containing library.json + covers/.
  GET  /api/export           — streams just the JSON (data-only, no images).
  GET  /api/export/preview   — row-count summary for the admin page.
  POST /admin/import         — accepts either a .json (JSON-only) or .zip
                                (full backup with cover files).

`/admin/import` is destructive: it replaces the current library inside
one transaction. Cover files in the zip are written into `covers_dir()`
and overwrite any existing same-named files.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlalchemy import delete as sa_delete
from sqlmodel import select

from app.db import get_session
from app.models import Comic, ComicTag, Copy, Publisher, Series, Tag
from app.services import covers
from app.services.portability import EXPORT_VERSION, export_all, import_all

router = APIRouter(tags=["admin"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]

LIBRARY_JSON = "library.json"
COVERS_PREFIX = "covers/"


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, flash: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        request, "admin.html",
        {"export_version": EXPORT_VERSION, "flash": flash[:200]},
    )


def _stamp_filename(suffix: str) -> str:
    return f"longbox-backup-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.{suffix}"


@router.get("/api/export")
async def export_json(session: SessionDep) -> Response:
    payload = await export_all(session)
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_stamp_filename("json")}"'},
    )


CSV_COLUMNS = [
    "comic_id", "copy_id", "title", "issue_number", "series", "publisher",
    "cover_date", "format", "fandom", "canon", "era", "isbn_13", "upc",
    "language", "condition", "storage_location", "read_status", "date_read",
    "purchase_date", "notes", "tags",
]


@router.get("/api/export/csv")
async def export_csv(session: SessionDep) -> Response:
    """Flattened CSV: one row per copy (or one row per comic when a comic
    has zero copies). Joins Comic + Series + Publisher and pulls in tag
    names as a semicolon-joined column.

    Designed for spreadsheet workflows — round-tripping back into the app
    isn't supported (use the JSON/ZIP backup for that).
    """
    rows = (
        await session.exec(
            select(Comic, Copy, Series, Publisher)
            .select_from(Comic)
            .join(Copy, Copy.comic_id == Comic.id, isouter=True)
            .join(Series, Series.id == Comic.series_id, isouter=True)
            .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
            .order_by(Comic.id.asc(), Copy.id.asc())
        )
    ).all()

    # Tag names per comic, in one round-trip.
    tag_rows = (
        await session.exec(
            select(ComicTag.comic_id, Tag.name)
            .join(Tag, Tag.id == ComicTag.tag_id)
            .order_by(Tag.name.asc())
        )
    ).all()
    tags_by_comic: dict[int, list[str]] = {}
    for cid, tname in tag_rows:
        tags_by_comic.setdefault(cid, []).append(tname)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for comic, copy, ser, pub in rows:
        writer.writerow({
            "comic_id": comic.id,
            "copy_id": copy.id if copy else "",
            "title": comic.title or "",
            "issue_number": comic.issue_number or "",
            "series": ser.name if ser else "",
            "publisher": pub.name if pub else "",
            "cover_date": comic.cover_date.isoformat() if comic.cover_date else "",
            "format": comic.format or "",
            "fandom": comic.fandom or "",
            "canon": comic.canon or "",
            "era": comic.era or "",
            "isbn_13": comic.isbn_13 or "",
            "upc": comic.upc or "",
            "language": comic.language or "",
            "condition": (copy.condition if copy else "") or "",
            "storage_location": (copy.storage_location if copy else "") or "",
            "read_status": (copy.read_status if copy else "") or "",
            "date_read": (copy.date_read.isoformat() if copy and copy.date_read else ""),
            "purchase_date": (copy.purchase_date.isoformat() if copy and copy.purchase_date else ""),
            "notes": (copy.notes if copy else "") or "",
            "tags": ";".join(tags_by_comic.get(comic.id, [])),
        })

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_stamp_filename("csv")}"'},
    )


@router.get("/api/backup")
async def backup_zip(session: SessionDep) -> Response:
    """Full backup: library data + every file under `covers_dir()`.

    Built in memory — fine at v1 scale (a few MB of JSON + a few MB per
    100 covers). If libraries grow into the tens of thousands of comics,
    swap to a streamed zip backed by a temp file.
    """
    payload = await export_all(session)
    body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(LIBRARY_JSON, body)
        cdir = covers.covers_dir()
        if cdir.exists():
            for path in sorted(cdir.iterdir()):
                if path.is_file():
                    zf.write(path, f"{COVERS_PREFIX}{path.name}")
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{_stamp_filename("zip")}"'},
    )


# Phrase the user must type verbatim to confirm a wipe. Kept on the
# server side rather than client-only JS so the destructive POST can't
# be triggered by a stray page reload or replayed form.
_WIPE_CONFIRMATION_PHRASE = "WIPE EVERYTHING"


@router.post("/admin/wipe", response_class=HTMLResponse)
async def admin_wipe(
    request: Request, session: SessionDep,
    confirm: str = Form(""),
    delete_cover_files: str = Form(""),
) -> HTMLResponse:
    """Factory reset. Truncates every user-data table; optionally also
    deletes cover image files under `covers_dir()`.

    Gated by a typed-confirmation phrase that has to match
    `_WIPE_CONFIRMATION_PHRASE` exactly. The transactional DB wipe runs
    first; cover-file deletion is best-effort *after* the DB succeeds.
    """
    if confirm.strip() != _WIPE_CONFIRMATION_PHRASE:
        return templates.TemplateResponse(
            request, "partials/_wipe_result.html",
            {"error": (f"Confirmation phrase didn't match. Type "
                       f"“{_WIPE_CONFIRMATION_PHRASE}” verbatim to wipe.")},
            status_code=400,
        )

    from app.services.wipe import wipe_everything
    outcome = await wipe_everything(
        session, delete_cover_files=(delete_cover_files == "on"),
    )
    return templates.TemplateResponse(
        request, "partials/_wipe_result.html", {"outcome": outcome},
    )


@router.get("/admin/inconsistencies", response_class=HTMLResponse)
async def admin_inconsistencies(request: Request, session: SessionDep) -> HTMLResponse:
    """Liberal sweep that flags comics whose stored data shape disagrees
    with itself — usually wrong-picks from the import wizard. Renders a
    partial designed to be HTMX-swapped into the admin Cleanup section."""
    from collections import Counter
    from app.services.inconsistencies import find_inconsistencies
    flagged = await find_inconsistencies(session)
    counts = Counter()
    for f in flagged:
        for r in f.reasons:
            counts[r.code] += 1
    return templates.TemplateResponse(
        request, "partials/_inconsistencies.html",
        {"flagged": flagged, "counts": counts.most_common(), "total": len(flagged)},
    )


@router.post("/admin/cleanup-orphans", response_class=HTMLResponse)
async def cleanup_orphan_series(request: Request, session: SessionDep) -> HTMLResponse:
    """One-shot sweep: delete every Series row that no Comic points at.

    Auto-pruning kicks in on comic delete going forward, but legacy DBs
    can have a backlog of zero-comic series from earlier experiments.
    Returns a small partial showing how many were dropped.
    """
    series_with_comics = (
        await session.exec(select(Comic.series_id).where(Comic.series_id.is_not(None)))
    ).all()
    in_use = {sid for sid in series_with_comics if sid is not None}

    all_series = (await session.exec(select(Series))).all()
    orphans = [s for s in all_series if s.id not in in_use]

    if orphans:
        await session.exec(
            sa_delete(Series).where(Series.id.in_([s.id for s in orphans]))
        )
        await session.commit()

    return HTMLResponse(
        f'<div class="rounded-lg border-2 border-crawl bg-crawl-light/30 p-3 text-sm">'
        f'<p class="font-semibold">POW! Pruned {len(orphans)} orphan series.</p>'
        f'</div>'
    )


@router.get("/api/export/preview")
async def export_preview(session: SessionDep) -> JSONResponse:
    """Lightweight row-count summary so the admin page can show what's
    about to be downloaded without paying the full serialization cost."""
    payload = await export_all(session)
    counts = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
    cdir = covers.covers_dir()
    cover_count = sum(1 for p in cdir.iterdir() if p.is_file()) if cdir.exists() else 0
    return JSONResponse({
        "version": payload["version"],
        "counts": counts,
        "cover_files": cover_count,
    })


def _is_safe_cover_name(name: str) -> bool:
    """Reject path-traversal attempts and nested-directory entries inside
    covers/. We only accept flat filenames."""
    if not name.startswith(COVERS_PREFIX):
        return False
    rest = name[len(COVERS_PREFIX):]
    if not rest or rest.endswith("/"):
        return False
    # Disallow '/', '\\', '..' inside the filename portion.
    if "/" in rest or "\\" in rest:
        return False
    if rest in {".", ".."} or rest.startswith(".."):
        return False
    return True


def _restore_covers(zf: zipfile.ZipFile) -> int:
    cdir = covers.covers_dir()
    count = 0
    for member in zf.infolist():
        if member.is_dir():
            continue
        if not _is_safe_cover_name(member.filename):
            continue
        target = cdir / Path(member.filename).name
        with zf.open(member) as src:
            target.write_bytes(src.read())
        count += 1
    return count


@router.post("/admin/import", response_class=HTMLResponse)
async def import_backup(
    request: Request,
    session: SessionDep,
    backup: UploadFile = File(...),
) -> HTMLResponse:
    raw = await backup.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    name = (backup.filename or "").lower()
    is_zip = name.endswith(".zip") or raw[:4] == b"PK\x03\x04"

    if is_zip:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail=f"invalid zip: {exc}") from exc
        with zf:
            if LIBRARY_JSON not in zf.namelist():
                raise HTTPException(
                    status_code=400,
                    detail=f"zip is missing {LIBRARY_JSON}",
                )
            try:
                payload = json.loads(zf.read(LIBRARY_JSON))
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail=f"invalid JSON inside zip: {exc}") from exc
            try:
                summary = await import_all(session, payload)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            cover_count = _restore_covers(zf)
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        try:
            summary = await import_all(session, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cover_count = 0

    return templates.TemplateResponse(
        request,
        "partials/_import_result.html",
        {
            "summary": summary,
            "total": sum(summary.values()),
            "covers_restored": cover_count,
        },
    )
