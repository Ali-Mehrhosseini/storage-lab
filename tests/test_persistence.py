"""Run with python -m unittest discover -s tests (requires httpx)."""
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

import app as storage
import dashboard


class PersistenceTest(unittest.TestCase):
    def test_replacement_evidence_and_history(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            with (patch.object(storage, "DATA_DIR", folder / "volume"),
                  patch.object(dashboard, "DB", folder / "runs.db"),
                  patch.dict(os.environ, POD_UID="original"),
                  TestClient(storage.app) as api,
                  TestClient(dashboard.app) as client):
                methods = []

                def transport(request, timeout):
                    method = request.get_method()
                    methods.append(method)
                    response = api.request(method, urlsplit(request.full_url).path, content=request.data)
                    stream = io.BytesIO(response.content)
                    stream.headers = response.headers
                    stream.status = response.status_code
                    return stream

                with patch.object(dashboard, "urlopen", transport):
                    self.assertEqual(client.get("/").status_code, 200)
                    run_id = client.post("/api/runs").json()["id"]
                    url = f"/api/runs/{run_id}/verify"
                    self.assertEqual(client.post(url).json()["state"], "Same pod")
                    os.environ["POD_UID"] = "replacement"
                    self.assertEqual(client.post(url).json()["state"], "Replacement verified")
                    (storage.DATA_DIR / run_id).write_bytes(b"corrupted")
                    self.assertEqual(client.post(url).json()["state"], "Verification failed")
                    self.assertEqual(methods, ["PUT", "GET", "GET", "GET", "GET"])
                    rows = list(csv.DictReader(io.StringIO(client.get("/results.csv").text)))
                    self.assertEqual(len(rows), 5)
                    self.assertEqual(rows[-1]["error"], "Checksum mismatch")
                    self.assertEqual(client.post("/api/runs", headers={"Origin":"https://elsewhere.invalid"}).status_code, 403)
                with TestClient(dashboard.app) as reopened:
                    self.assertEqual(reopened.get("/api/runs").json()["runs"][0]["id"], run_id)

    def test_failed_baseline(self):
        with tempfile.TemporaryDirectory() as root:
            with (patch.object(dashboard, "DB", Path(root) / "db"),
                  patch.object(dashboard, "urlopen", side_effect=TimeoutError("timed out")),
                  TestClient(dashboard.app) as client):
                run_id = client.post("/api/runs").json()["id"]
                self.assertEqual(client.post(f"/api/runs/{run_id}/verify").status_code, 409)
                events = client.get(f"/api/runs/{run_id}/requests").json()
                self.assertEqual(events[0]["error"], "timed out")
