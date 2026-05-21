"""Library-wide cleanup endpoint.

`POST /library/cleanup`        — start the background rebuild job.
`GET  /library/cleanup/status` — progress partial the UI polls.

The heavy lifting lives in `app.services.library_cleanup`; this
router is just the start trigger + the HTMX-polled status fragment.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.library_cleanup import get_progress, start_cleanup

router = APIRouter(tags=["cleanup"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _render(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/_cleanup_progress.html",
        {"p": get_progress()},
    )


@router.post("/library/cleanup", response_class=HTMLResponse)
async def library_cleanup_start(request: Request) -> HTMLResponse:
    """Kick off the rebuild. No-op if a run is already in flight —
    either way the caller gets the current progress fragment back."""
    await start_cleanup()
    return _render(request)


@router.get("/library/cleanup/status", response_class=HTMLResponse)
async def library_cleanup_status(request: Request) -> HTMLResponse:
    """Progress fragment. While the job runs the fragment re-arms its
    own HTMX poll; once finished it renders the final summary with no
    further polling."""
    return _render(request)
