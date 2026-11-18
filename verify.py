"""Upload and/or verify a small sample against a running Storage Lab server."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["put", "get"])
    parser.add_argument("sample", type=Path, help="original file for upload or verification")
    parser.add_argument("--url", default="http://127.0.0.1:8000/files/test-001")
    args = parser.parse_args()
    sample = args.sample.read_bytes()
    expected = hashlib.sha256(sample).hexdigest()
    payload = sample if args.action == "put" else None
    request = Request(args.url, data=payload, method=args.action.upper(),
                      headers={"Content-Type": "application/octet-stream"})
    start = time.perf_counter()
    with urlopen(request, timeout=60) as response:
        body = response.read()
        status = response.status
    elapsed = time.perf_counter() - start
    if args.action == "get" and hashlib.sha256(body).hexdigest() != expected:
        raise SystemExit("FAIL: downloaded content differs from the original sample")
    print(json.dumps(dict(method=request.method, status=status, bytes=len(sample),
                          seconds=round(elapsed, 6), sample_sha256=expected)))


if __name__ == "__main__":
    main()
