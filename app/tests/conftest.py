import os
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp(prefix="longbox-test-")
os.environ["DATA_DIR"] = _tmpdir
os.environ.setdefault("COMICVINE_API_KEY", "test-key")
os.environ.setdefault("METRON_USER", "test-user")
os.environ.setdefault("METRON_PASS", "test-pass")
os.environ.setdefault("STARTUP_BACKFILLS_IN_BACKGROUND", "false")
db_file = Path(_tmpdir) / "longbox.db"
if db_file.exists():
    db_file.unlink()


@pytest.fixture(scope="session", autouse=True)
def _set_data_dir():
    yield


@pytest.fixture(autouse=True)
def _reset_wookieepedia_throttle():
    """The Wookieepedia cool-down is process-wide: start and end every test
    without one, so a 429 mocked in one test can't fail the next."""
    from app.services import wookieepedia
    wookieepedia.reset_throttle()
    yield
    wookieepedia.reset_throttle()
