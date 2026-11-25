import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_db = Path(os.environ.get("TEST_DB_PATH") or Path(tempfile.gettempdir()) / "indocount_test.db")
for suffix in ("", "-wal", "-shm"):
    p = Path(str(_db) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = os.environ.get("PG_TEST_URL") or f"sqlite:///{_db.as_posix()}"
os.environ["SIM_AUTOSTART"] = "false"
os.environ["LLM_PROVIDER"] = "mock"
