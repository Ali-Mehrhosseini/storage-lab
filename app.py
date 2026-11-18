"""A small HTTP file service for comparing mounted Kubernetes volumes."""

import errno
import os
import tempfile
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, BinaryIO

from anyio import to_thread
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParameter
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
FileID = Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Storage Lab", version="1.1", lifespan=lifespan)


def evidence_headers() -> dict[str, str]:
    return {
        "X-Pod-UID": os.getenv("POD_UID", ""),
        "X-Pod-Name": os.getenv("POD_NAME", ""),
        "X-Node-Name": os.getenv("NODE_NAME", ""),
    }


def publish(staged: BinaryIO, destination: Path) -> None:
    """Make the complete upload visible, then sync its directory entry."""
    staged.flush()
    os.fsync(staged.fileno())
    os.replace(staged.name, destination)
    directory_fd = os.open(DATA_DIR, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@app.put("/files/{file_id}", status_code=204)
async def put_file(file_id: FileID, request: Request) -> Response:
    # Stage on the same volume so rename is atomic even during concurrent GETs.
    try:
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, prefix=".upload-", delete=False) as staged:
            try:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "Upload exceeds 64 MiB")
                    await to_thread.run_sync(staged.write, chunk)
                await to_thread.run_sync(publish, staged, DATA_DIR / file_id)
            finally:
                Path(staged.name).unlink(missing_ok=True)
    except OSError as error:
        if error.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(507, "Insufficient storage") from error
        raise
    return Response(status_code=204, headers=evidence_headers())


def read_chunks(source: BinaryIO) -> Iterator[bytes]:
    with source:
        while chunk := source.read(CHUNK_BYTES):
            yield chunk


@app.get("/files/{file_id}")
def get_file(file_id: FileID) -> StreamingResponse:
    try:
        source = (DATA_DIR / file_id).open("rb")
    except FileNotFoundError:
        raise HTTPException(404, "File does not exist") from None

    # Use the same open file for size and content, even if a PUT replaces it.
    size = os.fstat(source.fileno()).st_size
    return StreamingResponse(
        read_chunks(source),
        media_type="application/octet-stream",
        headers={"Content-Length": str(size), **evidence_headers()},
        background=BackgroundTask(source.close),
    )
