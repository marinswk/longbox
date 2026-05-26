"""CSV import wizard routes (lives under /admin/import).

The wizard is a 5-step flow keyed by an opaque `token` so users can leave
and come back. Routes are nested under /admin so the import surface is a
sub-section of the admin hub:

  GET  /admin/import/csv                          step 1 — upload form
  POST /admin/import/csv                          step 1 — accept file → parse → redirect
  GET  /admin/import/csv/{token}/map              step 2 — column mapping
  POST /admin/import/csv/{token}/map              step 2 — save mapping
  GET  /admin/import/csv/{token}/config           step 3 — sources picker
  POST /admin/import/csv/{token}/config           step 3 — save config
  GET  /admin/import/csv/{token}/resolve          step 4 — per-row search/picker
  POST /admin/import/csv/{token}/rows/{row_id}/*  step 4 — per-row state transitions
  GET  /admin/import/csv/{token}/commit           step 5 — pre-flight summary
  POST /admin/import/csv/{token}/commit           step 5 — apply (run commit pipeline)
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import ImportRow, ImportSession
from app.services.csv_import import (
    OUR_FIELDS, canonical_csv_headers, parse_csv, suggest_mapping,
)

router = APIRouter(tags=["import"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Cap upload size so a typo'd 1 GB CSV doesn't OOM the container.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _new_token() -> str:
    return secrets.token_urlsafe(16)


@router.get("/admin/import/csv", response_class=HTMLResponse)
async def import_upload_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "import_upload.html", {})


@router.post("/admin/import/csv", response_model=None)
async def import_upload_submit(
    request: Request,
    session: SessionDep,
    file: UploadFile = File(...),
):
    blob = await file.read()
    if len(blob) > _MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(
            request, "import_upload.html",
            {"error": f"File is larger than {_MAX_UPLOAD_BYTES // (1024*1024)} MB. "
                      "Trim it and try again."},
            status_code=400,
        )

    parsed = parse_csv(blob)
    if not parsed.headers or not parsed.rows:
        return templates.TemplateResponse(
            request, "import_upload.html",
            {"error": "Couldn't find any data rows in that file. "
                      "Check that the first line is a header and there's at "
                      "least one comic below it."},
            status_code=400,
        )

    token = _new_token()
    sess = ImportSession(
        token=token,
        filename=file.filename or None,
        state="map",
    )
    session.add(sess)
    await session.flush()  # need sess.id for child rows

    for i, row in enumerate(parsed.rows):
        session.add(ImportRow(
            session_id=sess.id,
            row_index=i,
            raw=json.dumps(row, ensure_ascii=False),
            status="pending",
        ))
    await session.commit()

    # Redirect to step 2. Until that route lands the response is a 404, but
    # we keep the wizard data so the user can resume once it ships.
    return RedirectResponse(url=f"/admin/import/csv/{token}/map", status_code=303)


# ---------------------------------------------------------------------------
# Step 2 — column mapping
# ---------------------------------------------------------------------------


_SAMPLE_ROWS = 5


async def _load_session(session: AsyncSession, token: str) -> ImportSession:
    sess = (await session.exec(
        select(ImportSession).where(ImportSession.token == token)
    )).first()
    if sess is None:
        raise HTTPException(status_code=404, detail="import session not found")
    return sess


async def _sample_rows(session: AsyncSession, session_id: int, n: int) -> list[dict]:
    rows = (await session.exec(
        select(ImportRow)
        .where(ImportRow.session_id == session_id)
        .order_by(ImportRow.row_index.asc())
        .limit(n)
    )).all()
    return [json.loads(r.raw) for r in rows]


async def _row_count(session: AsyncSession, session_id: int) -> int:
    rows = (await session.exec(
        select(ImportRow.id).where(ImportRow.session_id == session_id)
    )).all()
    return len(rows)


@router.get("/admin/import/csv/{token}/map", response_class=HTMLResponse)
async def import_map(token: str, request: Request, session: SessionDep) -> HTMLResponse:
    sess = await _load_session(session, token)
    samples = await _sample_rows(session, sess.id, _SAMPLE_ROWS)
    if not samples:
        # Edge case: parser persisted no rows. Punt back to upload.
        return RedirectResponse(url="/admin/import/csv", status_code=303)

    headers = list(samples[0].keys())  # source-of-truth header order

    # Pre-fill: the user's saved mapping if they're revisiting; otherwise
    # the autosuggest based on header text similarity.
    if sess.column_map:
        try:
            current_map = json.loads(sess.column_map)
            if not isinstance(current_map, dict):
                current_map = {}
        except json.JSONDecodeError:
            current_map = {}
    else:
        current_map = suggest_mapping(headers)

    total = await _row_count(session, sess.id)
    return templates.TemplateResponse(
        request, "import_map.html",
        {
            "session": sess,
            "headers": headers,
            "our_fields": OUR_FIELDS,
            "current_map": current_map,
            "samples": samples,
            "total_rows": total,
        },
    )


@router.post("/admin/import/csv/{token}/map", response_model=None)
async def import_map_save(
    token: str,
    request: Request,
    session: SessionDep,
):
    """Persist the column_map JSON. Form fields are named `map[<our_key>]`,
    each value is the chosen CSV header (or empty for "don't map")."""
    sess = await _load_session(session, token)
    form = await request.form()

    column_map: dict[str, str] = {}
    for tf in OUR_FIELDS:
        chosen = (form.get(f"map[{tf.key}]") or "").strip()
        if chosen:
            column_map[tf.key] = chosen

    sess.column_map = json.dumps(column_map, ensure_ascii=False)
    if sess.state == "upload":
        sess.state = "config"
    elif sess.state == "map":
        sess.state = "config"
    # If the user is revisiting from a later step, leave their state alone
    # — they may be tweaking the map without restarting the wizard.
    session.add(sess)
    await session.commit()

    return RedirectResponse(
        url=f"/admin/import/csv/{token}/config", status_code=303,
    )


# ---------------------------------------------------------------------------
# Step 3 — sources picker + config knobs
# ---------------------------------------------------------------------------


# Defaults applied when a session has never been past step 3.
_DEFAULT_CONFIG = {
    "year_tolerance": 1,
    "auto_tag_fandom": True,
    "auto_tag_publisher": False,
}


async def _all_rows_raw(session: AsyncSession, session_id: int) -> list[dict]:
    rows = (await session.exec(
        select(ImportRow)
        .where(ImportRow.session_id == session_id)
        .order_by(ImportRow.row_index.asc())
    )).all()
    return [json.loads(r.raw) for r in rows]


@router.get("/admin/import/csv/{token}/config", response_class=HTMLResponse)
async def import_config(token: str, request: Request, session: SessionDep) -> HTMLResponse:
    sess = await _load_session(session, token)
    if not sess.column_map:
        # Can't pick sources without a mapping — punt back to step 2.
        return RedirectResponse(
            url=f"/admin/import/csv/{token}/map", status_code=303,
        )
    column_map = json.loads(sess.column_map)

    raw_rows = await _all_rows_raw(session, sess.id)

    # Re-use the user's saved selection if revisiting; else compute defaults.
    chosen = json.loads(sess.sources) if sess.sources else None
    config = json.loads(sess.config) if sess.config else dict(_DEFAULT_CONFIG)

    from app.services.import_sources import build_source_tiles
    tiles = build_source_tiles(raw_rows, column_map, chosen_sources=chosen)

    return templates.TemplateResponse(
        request, "import_config.html",
        {
            "session": sess,
            "tiles": tiles,
            "config": config,
            "total_rows": len(raw_rows),
            "column_map": column_map,
        },
    )


@router.post("/admin/import/csv/{token}/config", response_model=None)
async def import_config_save(
    token: str,
    request: Request,
    session: SessionDep,
):
    sess = await _load_session(session, token)
    form = await request.form()

    # Sources: each tile submits `source[<key>] = on` if checked.
    selected: list[str] = []
    for key in ("wookieepedia", "comicvine", "metron", "openlibrary"):
        if form.get(f"source[{key}]") == "on":
            selected.append(key)
    sess.sources = json.dumps(selected)

    # Numeric / boolean knobs.
    try:
        year_tol = int(form.get("year_tolerance") or _DEFAULT_CONFIG["year_tolerance"])
    except ValueError:
        year_tol = _DEFAULT_CONFIG["year_tolerance"]
    year_tol = max(0, min(year_tol, 10))

    sess.config = json.dumps({
        "year_tolerance": year_tol,
        "auto_tag_fandom": form.get("auto_tag_fandom") == "on",
        "auto_tag_publisher": form.get("auto_tag_publisher") == "on",
    })

    if sess.state in ("upload", "map", "config"):
        sess.state = "resolve"
    session.add(sess)
    await session.commit()

    return RedirectResponse(
        url=f"/admin/import/csv/{token}/resolve", status_code=303,
    )


# ---------------------------------------------------------------------------
# Step 4 — resolve (per-row search + multi-hit picker)
# ---------------------------------------------------------------------------


def _apply_column_map(raw: dict, column_map: dict) -> dict:
    """Project a raw CSV row dict through the column_map into our internal
    field names. Empty values become absent keys, never empty strings, so
    downstream code can do plain `mapped.get(key)` truthiness checks."""
    out: dict = {}
    for our_key, csv_header in column_map.items():
        v = (raw.get(csv_header) or "").strip()
        if v:
            out[our_key] = v
    return out


def _summarize_row(raw: dict, column_map: dict) -> dict:
    """Prepare the human-readable summary block at the top of each card."""
    m = _apply_column_map(raw, column_map)
    return {
        "series": m.get("series", ""),
        "title": m.get("title", ""),
        "issue_number": m.get("issue_number", ""),
        "year": m.get("year", ""),
        "publisher": m.get("publisher", ""),
        "format": m.get("format", ""),
        "fandom": m.get("fandom", ""),
        "isbn_13": m.get("isbn_13", ""),
        "upc": m.get("upc", ""),
    }


async def _row_progress(session: AsyncSession, session_id: int) -> dict[str, int]:
    """Counts by status for the sticky footer."""
    rows = (await session.exec(
        select(ImportRow.status).where(ImportRow.session_id == session_id)
    )).all()
    counts: dict[str, int] = {
        "pending": 0, "matched": 0, "multi": 0,
        "not_found": 0, "skipped": 0, "errored": 0, "as_is": 0,
    }
    for s in rows:
        counts[s] = counts.get(s, 0) + 1
    counts["total"] = len(rows)
    # Multi-hit rows count as ready: the search endpoint pre-picks the
    # top-ranked candidate, the picker UI still lets the user change to
    # any alternative, and the commit treats them identically to matched.
    counts["ready"] = (
        counts.get("matched", 0) + counts.get("multi", 0)
        + counts.get("skipped", 0) + counts.get("as_is", 0)
    )
    return counts


@router.get("/admin/import/csv/{token}/resolve", response_class=HTMLResponse)
async def import_resolve(token: str, request: Request, session: SessionDep) -> HTMLResponse:
    sess = await _load_session(session, token)
    if not sess.column_map or sess.sources is None:
        return RedirectResponse(
            url=f"/admin/import/csv/{token}/config", status_code=303,
        )

    column_map = json.loads(sess.column_map)
    rows = (await session.exec(
        select(ImportRow)
        .where(ImportRow.session_id == sess.id)
        .order_by(ImportRow.row_index.asc())
    )).all()

    summaries = []
    for r in rows:
        raw = json.loads(r.raw)
        # Hydrate `candidates` and `error` so a reload of /resolve renders
        # picker rows + warning chips for already-resolved entries — not
        # just the next-action buttons. Without this, matched/multi rows
        # came back with empty pickers because the template iterates
        # `row.candidates`.
        candidates = json.loads(r.candidates) if r.candidates else []
        summaries.append({
            "id": r.id,
            "row_index": r.row_index,
            "status": r.status,
            "fields": _summarize_row(raw, column_map),
            "chosen_source": r.chosen_source,
            "chosen_source_id": r.chosen_source_id,
            "candidates": candidates,
            "error": r.error,
        })
    progress = await _row_progress(session, sess.id)

    return templates.TemplateResponse(
        request, "import_resolve.html",
        {
            "session": sess,
            "rows": summaries,
            "progress": progress,
        },
    )


def _candidate_to_dict(c) -> dict:
    """Whitelist of fields the resolve UI cares about, JSON-serializable."""
    return {
        "source": c.source,
        "source_id": c.source_id,
        "title": c.title,
        "series": c.series,
        "issue_number": c.issue_number,
        "publisher": c.publisher,
        "cover_date": c.cover_date,
        "cover_url": c.cover_url,
    }


async def _render_row_card(
    request: Request, session: AsyncSession, sess: ImportSession, row: ImportRow,
) -> HTMLResponse:
    column_map = json.loads(sess.column_map or "{}")
    raw = json.loads(row.raw)
    candidates = json.loads(row.candidates) if row.candidates else []
    progress = await _row_progress(session, sess.id)

    return templates.TemplateResponse(
        request, "partials/_import_row.html",
        {
            "session": sess,
            "row": {
                "id": row.id,
                "row_index": row.row_index,
                "status": row.status,
                "fields": _summarize_row(raw, column_map),
                "chosen_source": row.chosen_source,
                "chosen_source_id": row.chosen_source_id,
                "candidates": candidates,
                "error": row.error,
            },
            "progress": progress,
            # Only HTMX swap responses emit the OOB progress block. The
            # initial /resolve render shows the sticky footer once at the
            # bottom of the page — emitting it per-row would create N
            # duplicate elements with the same id.
            "oob_progress": True,
        },
    )


@router.post("/admin/import/csv/{token}/rows/{row_id}/search", response_class=HTMLResponse)
async def import_row_search(
    token: str, row_id: int,
    request: Request, session: SessionDep,
) -> HTMLResponse:
    """Run the upstream search for a single row. Called lazily by the
    resolve page's HTMX `hx-trigger="revealed"` so we don't fan out 250
    parallel API calls when the page first renders."""
    sess = await _load_session(session, token)
    row = await session.get(ImportRow, row_id)
    if row is None or row.session_id != sess.id:
        raise HTTPException(status_code=404, detail="row not found")

    column_map = json.loads(sess.column_map or "{}")
    sources = json.loads(sess.sources or "[]")
    config = json.loads(sess.config or "{}")
    year_tol = int(config.get("year_tolerance", 1))

    raw = json.loads(row.raw)
    mapped = _apply_column_map(raw, column_map)

    # Coerce mapped fields. `format` is left alone here — only relevant at
    # commit time. `year` may be like "2015 (2nd print)"; pull the leading int.
    year_val: int | None = None
    if y := mapped.get("year"):
        try:
            year_val = int("".join(ch for ch in y if ch.isdigit())[:4])
        except (TypeError, ValueError):
            year_val = None

    try:
        from app.services.aggregator import find_candidates_multi
        result = await find_candidates_multi(
            series=mapped.get("series"),
            title=mapped.get("title"),
            year=year_val,
            issue_number=mapped.get("issue_number"),
            isbn=mapped.get("isbn_13"),
            upc=mapped.get("upc"),
            sources=sources or None,
            year_tolerance=year_tol,
            # Bumped from 5 → 50 so the per-row search box can offer real
            # alternatives via horizontal pagination. The card UI pages
            # them client-side.
            limit=50,
        )
        cand_dicts = [_candidate_to_dict(c) for c in result.candidates]
        row.candidates = json.dumps(cand_dicts, ensure_ascii=False)

        # Surface throttled sources so the user can see why a row might
        # have come up "not_found" or with a thin candidate list.
        rate_limited = list(result.rate_limited or [])
        if rate_limited and not cand_dicts:
            # All sources rejected us — treat as a soft error so the user
            # can hit "retry" once the quota refreshes.
            row.status = "errored"
            row.chosen_source = None
            row.chosen_source_id = None
            row.error = f"Rate-limited by {', '.join(rate_limited)}. Try again later."
        elif not cand_dicts:
            row.status = "not_found"
            row.chosen_source = None
            row.chosen_source_id = None
            row.error = None
        elif len(cand_dicts) == 1:
            row.status = "matched"
            row.chosen_source = cand_dicts[0]["source"]
            row.chosen_source_id = cand_dicts[0]["source_id"]
            row.error = (f"Note: {', '.join(rate_limited)} rate-limited; "
                         "results may be partial.") if rate_limited else None
        else:
            row.status = "multi"
            # Pre-select the top-ranked candidate so users who don't tweak
            # anything still get a sensible default.
            row.chosen_source = cand_dicts[0]["source"]
            row.chosen_source_id = cand_dicts[0]["source_id"]
            row.error = (f"Note: {', '.join(rate_limited)} rate-limited; "
                         "results may be partial.") if rate_limited else None
    except Exception as exc:  # pragma: no cover — defensive
        row.status = "errored"
        row.error = str(exc)[:200]

    session.add(row)
    await session.commit()
    await session.refresh(row)
    return await _render_row_card(request, session, sess, row)


@router.post("/admin/import/csv/{token}/rows/{row_id}/search-custom", response_class=HTMLResponse)
async def import_row_search_custom(
    token: str, row_id: int,
    request: Request, session: SessionDep,
    q: str = Form(""),
) -> HTMLResponse:
    """Re-run the search with a user-supplied freeform query, bypassing
    the CSV-derived series/title/ISBN/UPC. Used when the auto-search came
    up wrong or empty and the user knows what to type."""
    sess = await _load_session(session, token)
    row = await session.get(ImportRow, row_id)
    if row is None or row.session_id != sess.id:
        raise HTTPException(status_code=404, detail="row not found")
    if not q.strip():
        return await _render_row_card(request, session, sess, row)

    sources = json.loads(sess.sources or "[]")
    config = json.loads(sess.config or "{}")
    year_tol = int(config.get("year_tolerance", 1))

    try:
        from app.services.aggregator import find_candidates_multi
        result = await find_candidates_multi(
            custom_query=q,
            sources=sources or None,
            year_tolerance=year_tol,
            limit=50,
        )
        cand_dicts = [_candidate_to_dict(c) for c in result.candidates]
        row.candidates = json.dumps(cand_dicts, ensure_ascii=False)
        rate_limited = list(result.rate_limited or [])

        if rate_limited and not cand_dicts:
            row.status = "errored"
            row.chosen_source = None
            row.chosen_source_id = None
            row.error = f"Rate-limited by {', '.join(rate_limited)}. Try again later."
        elif not cand_dicts:
            row.status = "not_found"
            row.chosen_source = None
            row.chosen_source_id = None
            row.error = None
        elif len(cand_dicts) == 1:
            row.status = "matched"
            row.chosen_source = cand_dicts[0]["source"]
            row.chosen_source_id = cand_dicts[0]["source_id"]
            row.error = (f"Note: {', '.join(rate_limited)} rate-limited; "
                         "results may be partial.") if rate_limited else None
        else:
            row.status = "multi"
            row.chosen_source = cand_dicts[0]["source"]
            row.chosen_source_id = cand_dicts[0]["source_id"]
            row.error = (f"Note: {', '.join(rate_limited)} rate-limited; "
                         "results may be partial.") if rate_limited else None
    except Exception as exc:  # pragma: no cover — defensive
        row.status = "errored"
        row.error = str(exc)[:200]

    session.add(row)
    await session.commit()
    await session.refresh(row)
    return await _render_row_card(request, session, sess, row)


@router.post("/admin/import/csv/{token}/rows/{row_id}/pick", response_class=HTMLResponse)
async def import_row_pick(
    token: str, row_id: int,
    request: Request, session: SessionDep,
    source: str = Form(""),
    source_id: str = Form(""),
) -> HTMLResponse:
    sess = await _load_session(session, token)
    row = await session.get(ImportRow, row_id)
    if row is None or row.session_id != sess.id:
        raise HTTPException(status_code=404, detail="row not found")
    row.chosen_source = source or None
    row.chosen_source_id = source_id or None
    row.status = "matched" if (source and source_id) else "multi"
    session.add(row)
    await session.commit()
    return await _render_row_card(request, session, sess, row)


@router.post("/admin/import/csv/{token}/cancel", response_model=None)
async def import_cancel(
    token: str, request: Request, session: SessionDep,
):
    """Abort the in-progress import. Drops the session row + every child
    ImportRow. Any per-row search requests still in flight in the user's
    browser will be canceled when the redirect fires."""
    from sqlalchemy import delete as sa_delete
    sess = await _load_session(session, token)
    await session.exec(sa_delete(ImportRow).where(ImportRow.session_id == sess.id))
    await session.exec(sa_delete(ImportSession).where(ImportSession.id == sess.id))
    await session.commit()
    # Bounce back to the admin Import section with a flash. The fragment
    # scrolls to the right card; the query param drives the banner.
    return RedirectResponse(
        url="/admin?flash=Import+canceled.#import", status_code=303,
    )


@router.post("/admin/import/csv/{token}/rows/{row_id}/skip", response_class=HTMLResponse)
async def import_row_skip(
    token: str, row_id: int,
    request: Request, session: SessionDep,
) -> HTMLResponse:
    sess = await _load_session(session, token)
    row = await session.get(ImportRow, row_id)
    if row is None or row.session_id != sess.id:
        raise HTTPException(status_code=404, detail="row not found")
    row.status = "skipped"
    row.chosen_source = None
    row.chosen_source_id = None
    session.add(row)
    await session.commit()
    return await _render_row_card(request, session, sess, row)


@router.post("/admin/import/csv/{token}/rows/{row_id}/as-is", response_class=HTMLResponse)
async def import_row_as_is(
    token: str, row_id: int,
    request: Request, session: SessionDep,
) -> HTMLResponse:
    """Mark a not-found row to be imported with just its mapped CSV fields
    — no upstream metadata. The commit step writes a Comic with whatever
    fields the user provided."""
    sess = await _load_session(session, token)
    row = await session.get(ImportRow, row_id)
    if row is None or row.session_id != sess.id:
        raise HTTPException(status_code=404, detail="row not found")
    row.status = "as_is"
    row.chosen_source = None
    row.chosen_source_id = None
    session.add(row)
    await session.commit()
    return await _render_row_card(request, session, sess, row)


# ---------------------------------------------------------------------------
# Step 5 — commit
# ---------------------------------------------------------------------------


@router.get("/admin/import/csv/{token}/commit", response_class=HTMLResponse)
async def import_commit(token: str, request: Request, session: SessionDep) -> HTMLResponse:
    sess = await _load_session(session, token)
    if sess.state == "done":
        # Already committed once; show the (re-derived) summary.
        return await _render_done(request, session, sess)
    progress = await _row_progress(session, sess.id)
    return templates.TemplateResponse(
        request, "import_commit.html",
        {"session": sess, "progress": progress},
    )


@router.post("/admin/import/csv/{token}/commit", response_model=None)
async def import_commit_run(
    token: str, request: Request, session: SessionDep,
    background: BackgroundTasks,
):
    sess = await _load_session(session, token)
    from app.services.import_commit import commit_session
    summary = await commit_session(session, sess, background)

    return templates.TemplateResponse(
        request, "import_done.html",
        {
            "session": sess,
            "summary": summary,
        },
    )


# ---------------------------------------------------------------------------
# CSV template + library round-trip export
# ---------------------------------------------------------------------------


import csv as _csv  # local alias so module-top imports stay tidy
import io as _io


def _csv_response(rows: list[list[str]], filename: str) -> Response:
    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/import/csv/template")
async def import_csv_template() -> Response:
    """Empty CSV with the canonical header. Fill it in your spreadsheet
    of choice, save, then upload via the import wizard — every column
    autosuggests onto the right Longbox field."""
    return _csv_response([canonical_csv_headers()], "longbox-import-template.csv")


@router.get("/admin/import/csv/export-library")
async def import_csv_export_library(session: SessionDep) -> Response:
    """Dump the library as a re-importable CSV using the same canonical
    header as the template. One row per Comic (NOT per Copy — the import
    wizard creates an empty copy for each row).

    Different from `/api/export/csv` which is one-row-per-copy and not
    round-trippable. Use this when migrating between deployments via the
    import wizard rather than the JSON/zip backup."""
    from app.models import Comic, Series, Publisher
    rows = (await session.exec(
        select(Comic, Series, Publisher)
        .select_from(Comic)
        .join(Series, Series.id == Comic.series_id, isouter=True)
        .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
        .order_by(Comic.id.asc())
    )).all()

    out: list[list[str]] = [canonical_csv_headers()]
    for comic, ser, pub in rows:
        out.append([
            ser.name if ser else "",
            comic.title or "",
            comic.issue_number or "",
            str(comic.cover_date.year) if comic.cover_date else "",
            pub.name if pub else "",
            comic.format or "",
            comic.collected_issues or "",
            comic.variant or "",
            comic.fandom or "",
            comic.isbn_13 or "",
            comic.upc or "",
        ])

    from datetime import datetime, UTC
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return _csv_response(out, f"longbox-library-{stamp}.csv")


async def _render_done(request: Request, db_session, sess: ImportSession) -> HTMLResponse:
    """Render the done page from the persisted ImportRow states (used when
    the user revisits /commit after the run already completed)."""
    rows = (await db_session.exec(
        select(ImportRow).where(ImportRow.session_id == sess.id)
    )).all()
    committed = [r for r in rows if r.status == "committed"]
    skipped = [r for r in rows if r.status == "skipped"]
    errored = [r for r in rows if r.status == "errored"]

    from app.services.import_commit import CommitSummary
    summary = CommitSummary(
        committed=len(committed),
        skipped=len(skipped),
        errored=len(errored),
        errors=[(r.row_index, r.error or "") for r in errored],
        comic_ids=[r.comic_id for r in committed if r.comic_id],
    )
    return templates.TemplateResponse(
        request, "import_done.html",
        {"session": sess, "summary": summary},
    )
