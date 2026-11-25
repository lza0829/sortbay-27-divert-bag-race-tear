from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Give app import-time seeding a disposable database before app.config is imported.
os.environ.setdefault(
    "SORTBAY_DB", f"/tmp/sortbay-pytest-import-{os.getpid()}.db"
)
# Temporary pytest databases do not need durable fsync; transaction rollback
# semantics and lock ordering are unchanged.
os.environ.setdefault("SORTBAY_SYNCHRONOUS", "OFF")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH  # noqa: E402
import app.db as db_module  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sortbay-test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    # app.db imported DB_PATH by value? connect() reads db_module.DB_PATH global,
    # which the monkeypatch above replaces. Keep config in sync for diagnostics.
    monkeypatch.setattr("app.config.DB_PATH", path)
    seed_if_empty()
    yield path


@pytest.fixture
def client(db_path: Path):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_ids(client: TestClient) -> dict:
    chutes = client.get("/api/口门").json()
    chute_a01 = next(c for c in chutes if c["code"] == "A01")
    shift = client.get("/api/班次").json()[0]
    return {"chute_id": chute_a01["id"], "shift_id": shift["id"]}


@pytest.fixture
def routed_scan(client: TestClient) -> dict:
    barcode = f"YB-PYTEST-{os.getpid()}-{os.urandom(4).hex()}"
    created = client.post(
        "/api/扫描",
        json={"条码": barcode, "目的地码": "110", "备注": "并发夹具"},
    )
    assert created.status_code == 200, created.text
    scan_id = created.json()["id"]
    routed = client.post(f"/api/扫描/{scan_id}/路由")
    assert routed.status_code == 200, routed.text
    return routed.json()


@pytest.fixture
def open_bag(client: TestClient, seeded_ids: dict) -> dict:
    resp = client.post("/api/封袋/开袋", json={"口门编号": seeded_ids["chute_id"]})
    assert resp.status_code == 200, resp.text
    return resp.json()
