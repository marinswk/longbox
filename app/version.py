"""Application version — single source of truth.

Semantic versioning, ``major.minor.patch``. Bumped on every commit so
the running build is always identifiable:

  * patch — bug fixes, parser tweaks, small internal changes
  * minor — a new user-facing feature
  * major — a breaking or structural change

Surfaced on the /admin page and in the /health endpoint's JSON.
"""

from __future__ import annotations

__version__ = "1.0.3"
