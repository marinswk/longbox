import os
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp(prefix="longbox-test-")
os.environ["DATA_DIR"] = _tmpdir
os.environ.setdefault("COMICVINE_API_KEY", "test-key")
os.environ.setdefault("METRON_USER", "test-user")
os.environ.setdefault("METRON_PASS", "test-pass")
db_file = Path(_tmpdir) / "longbox.db"
if db_file.exists():
    db_file.unlink()


@pytest.fixture(scope="session", autouse=True)
def _set_data_dir():
    yield
