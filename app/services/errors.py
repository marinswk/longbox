"""Typed upstream errors that the aggregator and refresh routes can
distinguish from generic failures.

Today the only specialised case is rate limiting (HTTP 429 / "quota
exhausted"-style payloads), so the picker / refresh views can show a
"served from cache" or "try again later" hint instead of silently
dropping the source. Other errors stay as plain Exceptions and are
swallowed-and-logged by the aggregator's `_safe` wrapper.
"""

from __future__ import annotations


class UpstreamRateLimit(Exception):
    """Raised when an upstream metadata source returns a quota-exhausted
    response (typically HTTP 429). Carries the source name so callers can
    render a "<source>: rate-limited" message without sniffing exception
    text.
    """

    def __init__(self, source: str, detail: str = "rate-limited") -> None:
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail
