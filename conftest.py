import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Root-level hidden/acceptance tests should not accidentally mutate the local
# development database. Explicit environment assignments still win.
os.environ.setdefault(
    "SORTBAY_DB", f"/tmp/sortbay-pytest-root-{os.getpid()}.db"
)
os.environ.setdefault("SORTBAY_SYNCHRONOUS", "OFF")
