from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.migrations import run_migrations
from app.routers import (
    add, admin, cleanup, comic_series, comics, containment, detail,
    duplicates, home, imports, library, lookup, missing, pwa, reading_log,
    search, series as series_router, stats, tags,
)
from app.services.covers import covers_dir

APP_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import app.models  # noqa: F401  ensure tables are registered
    from app.services.cache import prune_expired
    from app.services.fandoms import (
        backfill_comic_series_links,
        backfill_inferred_series_from_collected_issues,
        backfill_merge_duplicate_series, backfill_normalize_format,
        backfill_prune_dangling_comicseries,
        backfill_prune_empty_inferred_series,
        backfill_strip_multiline_names, backfill_wookieepedia_fandom,
    )

    await run_migrations()
    # Sweep stale metadata-cache rows on every cold start. Cheap (single
    # indexed DELETE) and keeps the table from growing unbounded across
    # months of upstream lookups.
    await prune_expired()
    # Make sure existing Wookieepedia-sourced comics carry a fandom even if
    # they were saved before the fandom-on-Comic schema landed. Idempotent.
    await backfill_wookieepedia_fandom()
    # Lowercase every legacy `Comic.format` value so the library facet
    # chips collapse cleanly. Idempotent.
    await backfill_normalize_format()
    # Strip embedded newlines from any Series/Comic/Publisher name that
    # got saved with multi-value wikitext blobs in the past. Idempotent.
    await backfill_strip_multiline_names()
    # Merge duplicate Series rows whose names were saved differently
    # (e.g. newline-blobs) and now normalize to the same value. Runs AFTER
    # the multi-line strip so the dedup probe sees the cleaned names.
    await backfill_merge_duplicate_series()
    # Sweep dangling ComicSeries / ComicContainment rows pointing at
    # comics that no longer exist (legacy data from delete paths
    # that didn't clean up link tables). Idempotent.
    await backfill_prune_dangling_comicseries()
    # Mirror Comic.series_id into ComicSeries for every comic so the
    # multi-series-aware queries (series detail, comic-detail series
    # section) see the primary series link. Idempotent.
    await backfill_comic_series_links()
    # Walk every comic with a `collected_issues` blob and auto-attach
    # it to every singles series implied by the entries. Idempotent;
    # catches up legacy omnibuses / TPBs saved before save-time
    # inference landed.
    await backfill_inferred_series_from_collected_issues()
    # Sweep up stale empty-expected-issues inference rows left over
    # from before the "skip when issues=[]" guard. Runs AFTER the
    # inference backfill so anything the inferrer just successfully
    # populated stays. Idempotent.
    await backfill_prune_empty_inferred_series()
    yield


_error_templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


_HEADLINES = {
    404: ("Lost in hyperspace", "That page isn't in this longbox. Maybe a stale link, maybe a typo."),
    500: ("Something blew up", "An error escaped to the surface. The detail below may help."),
}


async def _render_error(request: Request, status: int, detail: str | None) -> HTMLResponse:
    headline, blurb = _HEADLINES.get(status, ("Unexpected error", "Try again or head home."))
    return _error_templates.TemplateResponse(
        request, "error.html",
        {"status": status, "headline": headline, "blurb": blurb, "detail": detail},
        status_code=status,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Longbox", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    app.mount("/covers", StaticFiles(directory=str(covers_dir())), name="covers")

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
        # Only intercept full-page HTML routes — leave HTMX partial errors and
        # /api JSON paths to FastAPI's default handler so callers see machine-
        # readable bodies.
        accept = request.headers.get("accept", "")
        path = request.url.path
        if "text/html" not in accept or path.startswith("/api/") or request.headers.get("HX-Request"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        detail = exc.detail if isinstance(exc.detail, str) and exc.status_code >= 500 else None
        return await _render_error(request, exc.status_code, detail)

    @app.get("/health")
    def health() -> dict[str, str]:
        from app.version import __version__
        return {"status": "ok", "version": __version__}

    app.include_router(home.router)
    app.include_router(comics.router)
    app.include_router(lookup.router)
    app.include_router(add.router)
    app.include_router(library.router)
    app.include_router(detail.router)
    app.include_router(stats.router)
    app.include_router(reading_log.router)
    app.include_router(imports.router)
    app.include_router(pwa.router)
    app.include_router(tags.router)
    app.include_router(admin.router)
    app.include_router(search.router)
    app.include_router(duplicates.router)
    app.include_router(series_router.router)
    app.include_router(containment.router)
    app.include_router(comic_series.router)
    app.include_router(cleanup.router)
    app.include_router(missing.router)

    return app
