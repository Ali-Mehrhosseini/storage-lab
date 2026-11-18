"""Single-process experiment runner and SQLite-backed dashboard."""
import csv
import hashlib
import io
import http.client
import os
import socket
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Request as BrowserRequest
from fastapi.responses import FileResponse, Response

DB = Path(os.getenv("RESULTS_DB", "./results/runs.sqlite3"))
TARGET = os.getenv("STORAGE_URL", "http://127.0.0.1:8000").rstrip("/")
BACKEND = os.getenv("BACKEND_LABEL", "Local filesystem")
RUNNER = os.getenv("NODE_NAME", socket.gethostname())
SIZE = 1024 * 1024
gate = Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def database():
    connection = sqlite3.connect(DB, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, started TEXT NOT NULL, backend TEXT NOT NULL,
                target TEXT NOT NULL, runner TEXT NOT NULL, bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL, state TEXT NOT NULL, baseline_uid TEXT
            );
            CREATE TABLE IF NOT EXISTS requests (
                sequence INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                started TEXT NOT NULL, stage TEXT NOT NULL, method TEXT NOT NULL,
                status INTEGER, elapsed_ms REAL NOT NULL, observed_sha256 TEXT,
                pod_uid TEXT, pod_name TEXT, node TEXT, error TEXT
            );
        """)
        db.execute("UPDATE runs SET state='Interrupted' WHERE state='Preparing'")
    yield


app = FastAPI(title="Storage Lab Experiments", lifespan=lifespan)


@contextmanager
def experiment(request: BrowserRequest):
    # Reject cross-origin browser actions; ordinary CLI clients may omit Origin.
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Open the dashboard on its local address")
    if not gate.acquire(blocking=False):
        raise HTTPException(409, "Another experiment is running")
    try:
        yield
    finally:
        gate.release()


def measure(run, stage, payload=None):
    method = "GET" if payload is None else "PUT"
    req = Request(f"{run['target']}/files/{run['id']}", method=method, data=payload,
                  headers={"Content-Type": "application/octet-stream"})
    started = now()
    status = digest = error = None
    headers = {}
    begin = time.perf_counter()
    try:
        with urlopen(req, timeout=20) as response:
            status = response.status
            headers = response.headers
            body = response.read(run["bytes"] + 1)
        elapsed = time.perf_counter() - begin
        if status != (200 if method == "GET" else 204):
            error = f"Unexpected HTTP {status}"
        if method == "GET":
            digest = hashlib.sha256(body).hexdigest()
            if digest != run["sha256"]:
                error = "Checksum mismatch"
    except (OSError, HTTPError, http.client.HTTPException) as exc:
        elapsed = time.perf_counter() - begin
        status = getattr(exc, "code", status)
        error = str(exc)
    uid = headers.get("X-Pod-UID") or None
    with database() as db:
        db.execute("""INSERT INTO requests
            (run_id,started,stage,method,status,elapsed_ms,observed_sha256,pod_uid,pod_name,node,error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run["id"], started, stage, method, status, elapsed * 1000, digest, uid,
             headers.get("X-Pod-Name"), headers.get("X-Node-Name"), error))
    return error, uid


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).with_name("dashboard.html"))


@app.get("/api/runs")
def history():
    with database() as db:
        rows = db.execute("SELECT * FROM runs ORDER BY started DESC, rowid DESC LIMIT 100")
        return {"backend": BACKEND, "runs": [dict(row) for row in rows]}


@app.get("/api/runs/{run_id}/requests")
def details(run_id: str):
    with database() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM requests WHERE run_id=? ORDER BY sequence", (run_id,))]


@app.post("/api/runs", status_code=201)
def start(request: BrowserRequest):
    with experiment(request):
        sample = os.urandom(SIZE)
        run_id = uuid.uuid4().hex
        with database() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,'Preparing',NULL)",
                       (run_id, now(), BACKEND, TARGET, RUNNER, SIZE, hashlib.sha256(sample).hexdigest()))
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        error, uid = measure(run, "Baseline", sample)
        if not error:
            error, uid = measure(run, "Baseline")
        with database() as db:
            db.execute("UPDATE runs SET state=?, baseline_uid=? WHERE id=?",
                       ("Baseline failed" if error else "Ready to verify", uid, run_id))
        return {"id": run_id}


@app.post("/api/runs/{run_id}/verify")
def verify(run_id: str, request: BrowserRequest):
    with experiment(request):
        with database() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "Unknown run")
        if run["state"] in ("Preparing", "Interrupted", "Baseline failed"):
            raise HTTPException(409, "A successful baseline is required")
        error, uid = measure(run, "Verification")
        state = "Bytes verified"
        if error:
            state = "Verification failed"
        elif uid and run["baseline_uid"]:
            state = "Same pod" if uid == run["baseline_uid"] else "Replacement verified"
        with database() as db:
            db.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
        return {"state": state}


@app.get("/results.csv")
def export():
    with database() as db:
        rows = db.execute("""SELECT r.*, q.sequence, q.started AS request_started,
            q.stage,q.method,q.status,q.elapsed_ms,q.observed_sha256,q.pod_uid,q.pod_name,q.node,q.error
            FROM runs r LEFT JOIN requests q ON r.id=q.run_id ORDER BY r.started,q.sequence""")
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow([col[0] for col in rows.description])
        writer.writerows(rows)
    return Response(stream.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="storage-results.csv"'})
