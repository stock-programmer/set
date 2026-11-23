"""Offline HTTP checks for the read-only production viewer."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from pianchang.studio_progress_server import ProgressSource
from trace_system import Tracer
from pianchang.web_server import create_server


class ProgressHTTPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.source = ProgressSource(root, state_dir=root, tracer=Tracer(root / "offline-traces"))
        self.clip = root / "test.mp4"
        self.clip.write_bytes(b"offline-clip")
        self.job_id = "a" * 32
        (root / "state.json").write_text(json.dumps({"jobs":[{"id":self.job_id, "kind":"studio_video",
            "project_id":"p", "status":"running"}]}), encoding="utf-8")
        self.server = create_server(self.source, port=0, progress_only=True)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, url, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        try:
            conn.request(method, url, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_only_snapshot_and_exact_scoped_media_can_be_read(self):
        prefix = "/api/projects/p/studio/progress"
        expected = {"job_id": self.job_id, "shot_id":"S001", "sha256":"b" * 64}
        def projection(root, pid, job, *, video_root, preview_url):
            return {"preview": preview_url("S001", expected["sha256"]),"shots":[]}
        with patch("pianchang.studio_live_progress.read_production_progress", side_effect=projection), patch(
                "pianchang.studio_live_progress.resolve_production_clip", return_value=self.clip) as resolver:
            status, headers, body = self.request("GET", prefix + "?job_id=" + self.job_id)
            self.assertEqual(status, 200)
            self.assertIn("X-Trace-Id", headers)
            trace_id = headers["X-Trace-Id"]
            records = self.source.tracer.get_events(trace_id)
            recorded = next(record for record in records if record["name"] == "http.response.body")
            self.assertEqual(self.source.tracer.resolve(recorded["data"])["body"], json.loads(body))
            self.assertTrue(self.source.tracer.verify(trace_id)["ok"])
            url = json.loads(body)["preview"]
            self.assertEqual(url, prefix + "/media?" + urlencode(expected))
            status, headers, body = self.request("GET", url, {"Range":"bytes=0-6"})
            self.assertEqual((status, body), (206, b"offline"))
            self.assertEqual(headers["Content-Range"], "bytes 0-6/12")
            self.assertEqual(self.request("GET", url.replace("/p/", "/another/"))[0], 404)
            self.assertEqual(resolver.call_count, 1)
        self.assertEqual(self.request("POST", "/api/projects/p/studio/video")[0], 405)
        self.assertEqual(self.request("GET", "/api/jobs")[0], 404)
        self.assertEqual(self.request("GET", prefix + "?job_id=" + self.job_id, {"Host": "example.test"})[0], 403)
