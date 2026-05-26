"""Lightweight security middleware for the no-auth LAN deploy model.

Two layers, both opt-in via env vars so the first-run / local-dev
experience stays painless:

* :class:`OriginCheckMiddleware` — rejects non-GET requests whose
  ``Origin`` header isn't in the configured allowlist. Mitigates
  cross-site request forgery from a malicious site the user happens
  to have open in another tab while their Longbox tab is also open.
* Starlette's built-in
  :class:`~starlette.middleware.trustedhost.TrustedHostMiddleware` is
  wired up in :mod:`app.main` directly when ``ALLOWED_HOSTS`` is
  tighter than the ``*`` default — no custom code needed here.

Both layers are no-ops on the LAN-only single-user default. They
become useful as soon as you start fronting Longbox with a reverse
proxy + auth: declare the allowlist values in ``.env`` and any
cross-origin POST (e.g. a CSRF attempt) is dropped at the door.
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Reject non-GET requests whose ``Origin`` claims a different
    site than the allowlist.

    The defense relies on the browser always sending ``Origin`` on
    cross-origin POST/PUT/DELETE — true for every browser shipped
    since ~2019. Requests with NO ``Origin`` header at all (curl,
    Home Assistant, anything not a browser) are allowed through so
    scripted access keeps working. Same-origin browser requests pass
    because the ``Origin`` matches one of the allowed values.

    Behaviour:
      * Empty ``allowed_origins`` → middleware is a no-op (good for
        first-run / local dev where the user hasn't configured CSRF
        yet).
      * Safe method (GET/HEAD/OPTIONS) → always pass.
      * Non-GET with ``Origin`` matching the allowlist → pass.
      * Non-GET with ``Origin`` MISSING (no header at all) → pass
        (machine clients, no signal to act on).
      * Non-GET with ``Origin`` set but not in allowlist → 403.

    Each allowed origin is matched verbatim on the
    ``scheme://host[:port]`` form. ``http://longbox.lan:8080`` and
    ``https://longbox.lan`` are distinct entries.
    """

    def __init__(self, app, allowed_origins: list[str]) -> None:
        super().__init__(app)
        # Normalise: strip trailing slashes, drop empty entries.
        self._allowed: set[str] = {
            o.rstrip("/").strip()
            for o in allowed_origins
            if o.strip()
        }

    @staticmethod
    def parse_setting(raw: str) -> list[str]:
        """Comma-separated env-var → list of origin strings."""
        return [s.strip() for s in (raw or "").split(",") if s.strip()]

    async def dispatch(self, request: Request, call_next):
        # No-op when unconfigured.
        if not self._allowed:
            return await call_next(request)

        if request.method in _SAFE_METHODS:
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin is None:
            # No browser signal to validate — non-browser caller.
            return await call_next(request)

        parsed = urlparse(origin)
        # Normalize to scheme://host[:port], drop anything past.
        if not parsed.scheme or not parsed.netloc:
            return _forbid(request, "malformed Origin header")
        normalised = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if normalised not in self._allowed:
            return _forbid(
                request,
                f"Origin {normalised!r} is not in the configured "
                f"allowlist for this Longbox instance.",
            )
        return await call_next(request)


def _forbid(request: Request, detail: str) -> Response:
    # HTMX requests + /api JSON paths get JSON; full-page HTML
    # navigations get a friendlier text response. Mirrors the same
    # accept-aware branch the global error handler uses.
    accept = request.headers.get("accept", "")
    path = request.url.path
    if "text/html" not in accept or path.startswith("/api/") or request.headers.get("HX-Request"):
        return JSONResponse({"detail": detail}, status_code=403)
    return Response(
        content=(
            "403 — request rejected by Longbox CSRF guard.\n\n"
            + detail
            + "\n\nIf you reached this page normally (not via a malicious link), "
            "the configured CSRF_ALLOWED_ORIGINS list is probably too narrow. "
            "Adjust the env var or unset it to disable the check."
        ),
        status_code=403,
        media_type="text/plain",
    )
