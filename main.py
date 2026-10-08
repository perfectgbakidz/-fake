import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

DB_PATH = os.getenv("DB_PATH", "health.db")
API_TOKEN = os.getenv("API_TOKEN")

# Comma-separated list of allowed frontend origins, e.g.
# "https://yourname.github.io,http://localhost:5500"
# Defaults to "*" (any origin) if not set.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT    NOT NULL,
    bpm         INTEGER NOT NULL,
    ppg         INTEGER NOT NULL,
    temperature REAL    NOT NULL,
    received_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_device_time
    ON readings (device_id, received_at);
"""


def init_db() -> None:
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not API_TOKEN:
        print("WARNING: API_TOKEN is not set. All authenticated requests will fail.")
    yield


app = FastAPI(
    title="Health Data Backend",
    description="Receives simulated health readings from ESP32-CAM devices.",
    version="1.0.0",
    lifespan=lifespan,
)

# ------------------------------- CORS -----------------------------------
# Must be added before routes are served. Lets the browser frontend call
# the API, including the preflight OPTIONS request sent for requests
# that carry an Authorization header.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,          # we use a Bearer token, not cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,                      # cache preflight results for 10 minutes
)


# ----------------------------- Dependencies -----------------------------

def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server API_TOKEN is not configured",
        )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme != "Bearer" or not secrets.compare_digest(token, API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ------------------------------- Schemas --------------------------------

class HealthIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=64)
    bpm: int = Field(ge=0, le=250)
    ppg: int = Field(ge=0, le=4095)            # ESP32 12-bit ADC range
    temperature: float = Field(ge=25.0, le=45.0)


class ReadingOut(HealthIn):
    id: int
    received_at: str


# -------------------------------- Routes --------------------------------

@app.get("/healthz", tags=["meta"])
def healthz():
    """Unauthenticated liveness check (used by Render)."""
    return {"status": "ok"}


@app.post(
    "/health",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_token)],
    tags=["readings"],
)
def ingest_reading(reading: HealthIn, db: sqlite3.Connection = Depends(get_db)):
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = db.execute(
        """
        INSERT INTO readings (device_id, bpm, ppg, temperature, received_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (reading.device_id, reading.bpm, reading.ppg, reading.temperature, received_at),
    )
    db.commit()
    return {"id": cur.lastrowid, "status": "stored"}


@app.get(
    "/readings",
    response_model=list[ReadingOut],
    dependencies=[Depends(require_token)],
    tags=["readings"],
)
def list_readings(
    device_id: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=1000),
    db: sqlite3.Connection = Depends(get_db),
):
    if device_id:
        rows = db.execute(
            """
            SELECT * FROM readings
            WHERE device_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (device_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get(
    "/readings/latest/{device_id}",
    response_model=ReadingOut,
    dependencies=[Depends(require_token)],
    tags=["readings"],
)
def latest_reading(device_id: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute(
        "SELECT * FROM readings WHERE device_id = ? ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No readings for this device")
    return dict(row)
