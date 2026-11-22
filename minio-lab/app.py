import hashlib
import os
import secrets
from contextlib import ExitStack, closing
from pathlib import Path
from socket import gethostname
from statistics import median
from time import perf_counter
from typing import Annotated, Literal
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from fastapi import Path as RoutePath
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Storage Lab · S3")
PAGE = Path(__file__).with_name("index.html")
DEFAULT_SAMPLE_KIB = 1024
MAX_SAMPLE_KIB = 16 * 1024
MAX_SAMPLE_BYTES = MAX_SAMPLE_KIB * 1024
RunId = Annotated[str, RoutePath(pattern=r"^[a-f0-9]{32}$")]


Backend = Literal["minio", "aws"]
BACKEND_LABELS = {"minio": "MinIO", "aws": "Amazon S3"}


class UploadOptions(BaseModel):
    size_kib: int = Field(default=DEFAULT_SAMPLE_KIB, ge=1, le=MAX_SAMPLE_KIB, strict=True)
    backend: Backend = "minio"


class ComparisonOptions(BaseModel):
    size_kib: int = Field(default=DEFAULT_SAMPLE_KIB, ge=1, le=MAX_SAMPLE_KIB, strict=True)
    repetitions: int = Field(default=3, ge=1, le=5, strict=True)


def configured_buckets():
    return {"minio": os.getenv("S3_BUCKET"), "aws": os.getenv("AWS_S3_BUCKET")}


def storage(backend: Backend):
    label = BACKEND_LABELS[backend]
    bucket = configured_buckets()[backend]
    if not bucket:
        raise HTTPException(503, f"Configure the {label} bucket on the server.")

    options = {}
    if backend == "minio":
        endpoint = os.getenv("S3_ENDPOINT_URL")
        access_key = os.getenv("MINIO_ACCESS_KEY_ID")
        secret_key = os.getenv("MINIO_SECRET_ACCESS_KEY")
        if not os.getenv("AWS_S3_BUCKET") and access_key is None and secret_key is None:
            access_key = os.getenv("AWS_ACCESS_KEY_ID")
            secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        if not all((endpoint, access_key, secret_key)):
            raise HTTPException(503, "Configure the MinIO endpoint and MinIO credentials on the server.")
        options.update(
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=os.getenv("MINIO_REGION") or (
                os.getenv("AWS_DEFAULT_REGION") if not os.getenv("AWS_S3_BUCKET") else None
            ) or "us-east-1",
        )
    else:
        options["region_name"] = os.getenv("AWS_S3_REGION") or os.getenv("AWS_DEFAULT_REGION")
        if not options["region_name"]:
            raise HTTPException(503, "Set AWS_S3_REGION to the Amazon S3 bucket's region.")

    try:
        client = boto3.client(
            "s3", **options,
            config=Config(
                s3={"addressing_style": "path"} if backend == "minio" else None,
                ignore_configured_endpoint_urls=True,
                connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1},
            ),
        )
    except (BotoCoreError, ClientError) as error:
        raise s3_error(error) from error
    return client, bucket


def s3_error(error):
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return HTTPException(404, "Sample not found in the selected bucket.")
        if code == "NoSuchBucket":
            return HTTPException(502, "Create the selected bucket before running the test.")
        if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            return HTTPException(502, "S3 rejected the request. Check credentials and bucket permissions.")
    return HTTPException(502, "S3 request failed. Check connectivity, region, and credentials.")


def fetch_sample(client, bucket, run_id):
    started = perf_counter()
    try:
        result = client.get_object(Bucket=bucket, Key=f"experiments/{run_id}.bin")
        with closing(result["Body"]) as body:
            if not 1 <= result["ContentLength"] <= MAX_SAMPLE_BYTES:
                raise HTTPException(409, "The stored object has an unexpected size.")
            payload = body.read(result["ContentLength"] + 1)
            if len(payload) != result["ContentLength"]:
                raise HTTPException(502, "S3 returned an incomplete object.")
    except (BotoCoreError, ClientError) as error:
        raise s3_error(error) from error
    return payload, result.get("Metadata", {}).get("sha256"), round((perf_counter() - started) * 1000, 2)


def client_location():
    return os.getenv("LAB_CLIENT_LOCATION") or os.getenv("NODE_NAME") or gethostname()


def evidence(backend, bucket, run_id, payload, expected, get_ms, put_ms=None):
    actual = hashlib.sha256(payload).hexdigest()
    return {
        "id": run_id, "backend": backend, "bucket": bucket, "client": client_location(), "key": f"experiments/{run_id}.bin",
        "bytes": len(payload), "expected_sha256": expected, "sha256": actual,
        "matched": actual == expected,
        "put_ms": put_ms, "get_ms": get_ms,
    }


@app.get("/")
def index():
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


@app.get("/api/target")
def target():
    buckets = configured_buckets()
    return {"bucket": buckets["minio"], "buckets": buckets, "max_size_kib": MAX_SAMPLE_KIB}


def write_and_read(client, backend, bucket, run_id, payload, expected):
    started = perf_counter()
    try:
        client.put_object(
            Bucket=bucket, Key=f"experiments/{run_id}.bin", Body=payload,
            ContentType="application/octet-stream", Metadata={"sha256": expected},
        )
    except (BotoCoreError, ClientError) as error:
        raise s3_error(error) from error
    put_ms = round((perf_counter() - started) * 1000, 2)
    try:
        body, _, get_ms = fetch_sample(client, bucket, run_id)
    except HTTPException as error:
        raise HTTPException(error.status_code, f"Uploaded {run_id}. Read-back failed. {error.detail}") from error
    return evidence(backend, bucket, run_id, body, expected, get_ms, put_ms)


@app.post("/api/runs", status_code=201)
def upload_and_verify(options: UploadOptions | None = None):
    options = options or UploadOptions()
    client, bucket = storage(options.backend)
    payload = secrets.token_bytes(options.size_kib * 1024)
    expected = hashlib.sha256(payload).hexdigest()
    with closing(client):
        return write_and_read(client, options.backend, bucket, uuid4().hex, payload, expected)


@app.post("/api/comparisons", status_code=201)
def compare(options: ComparisonOptions):
    pairs = []
    with ExitStack() as stack:
        targets = {}
        for backend in BACKEND_LABELS:
            client, bucket = storage(backend)
            targets[backend] = (stack.enter_context(closing(client)), bucket)
        for index in range(options.repetitions):
            run_id = uuid4().hex
            payload = secrets.token_bytes(options.size_kib * 1024)
            expected = hashlib.sha256(payload).hexdigest()
            order = ("minio", "aws") if index % 2 == 0 else ("aws", "minio")
            results = []
            for backend in order:
                client, bucket = targets[backend]
                try:
                    results.append(write_and_read(client, backend, bucket, run_id, payload, expected))
                except HTTPException as error:
                    results.append({
                        "id": run_id, "backend": backend, "bucket": bucket,
                        "matched": False, "error": error.detail,
                    })
            pairs.append(results)

    verified = [pair for pair in pairs if all(run["matched"] for run in pair)]
    summaries = {}
    for backend in BACKEND_LABELS:
        runs = [run for pair in verified for run in pair if run["backend"] == backend]
        summaries[backend] = {
            metric: round(median(run[metric] for run in runs), 2) if runs else None
            for metric in ("put_ms", "get_ms")
        }
    return {
        "bytes": options.size_kib * 1024,
        "client": client_location(),
        "verified_pairs": len(verified),
        "pairs": pairs,
        "medians": summaries,
    }


@app.get("/api/runs/{run_id}")
def verify_existing(run_id: RunId, backend: Backend = "minio"):
    client, bucket = storage(backend)
    with closing(client):
        body, expected, get_ms = fetch_sample(client, bucket, run_id)
    return evidence(backend, bucket, run_id, body, expected, get_ms)


@app.get("/api/runs/{run_id}/download")
def download(run_id: RunId, backend: Backend = "minio"):
    client, bucket = storage(backend)
    with closing(client):
        body, _, _ = fetch_sample(client, bucket, run_id)
    return Response(body, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{run_id}.bin"',
        "Cache-Control": "no-store",
    })
