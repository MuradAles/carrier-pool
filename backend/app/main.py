"""FastAPI entrypoint.

Scaffold only: health check plus the route surface from PRD section 10, so the
shape is visible. The recommendation, pricing, and ingestion logic is not
implemented yet — those endpoints deliberately return 501 rather than fake data.
"""

import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://carrier:carrier@localhost:5432/carrier_pool")
DATA_DIR = Path(os.environ.get("DATA_DIR", "../data"))

app = FastAPI(title="Carrier Pool", version="0.1.0")


@app.get("/api/health")
def health() -> dict[str, str]:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        database = "ok"
    except Exception as exc:  # surface the reason instead of a bare 500
        database = f"unavailable: {type(exc).__name__}"

    sync_files = sorted(DATA_DIR.glob("*/*_sync.json")) if DATA_DIR.is_dir() else []
    return {
        "status": "ok",
        "database": database,
        "data_dir": f"{DATA_DIR} ({len(sync_files)} sync files)",
    }


@app.get("/api/brokers")
def list_brokers() -> list[dict[str, str]]:
    raise HTTPException(status_code=501, detail="Not implemented")


@app.get("/api/loads")
def list_loads(broker_id: str | None = None, status: str | None = None) -> list[dict[str, str]]:
    raise HTTPException(status_code=501, detail="Not implemented")


@app.get("/api/loads/{load_id}")
def get_load(load_id: str) -> dict[str, str]:
    raise HTTPException(status_code=501, detail="Not implemented")


@app.get("/api/loads/{load_id}/recommendations")
def get_recommendations(load_id: str) -> dict[str, str]:
    raise HTTPException(status_code=501, detail="Not implemented")


@app.get("/api/loads/{load_id}/price-estimate")
def get_price_estimate(load_id: str) -> dict[str, str]:
    raise HTTPException(status_code=501, detail="Not implemented")


@app.post("/api/admin/ingest")
def ingest() -> dict[str, str]:
    raise HTTPException(status_code=501, detail="Not implemented")
