import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools import workbench_loop_restart as restart
from trace_system import TraceError


class RestartReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.web = Path(temporary.name) / "web"
        self.web.mkdir()
        self.runs = Path(temporary.name) / "runs"
        self.state = {"old_pid": 421, "url": "http://127.0.0.1:8765", "port": 8765,
                      "old_started_at": "2026-11-21T17:00:00+08:00"}
        self.write_manifest(421, self.state["old_started_at"])
        for name, value in (("event", None), ("_stop_if_idle", True), ("_replacement_ready", True)):
            mock = patch.object(restart, name, return_value=value)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    def write_manifest(self, pid, stamp):
        path = self.web / "server-process.json"
        path.write_text(json.dumps({"pid": pid, "url": self.state["url"], "started_at": stamp}), "utf-8")

    def run_restart(self, start):
        return restart.restart_original(self.state, self.web, self.runs, lambda: False, start)

    def test_readiness_timeout_after_spawn_uses_original_listener_receipt_once(self):
        def slow_start(port, url):
            self.write_manifest(812, "2026-11-21T17:03:00+08:00")
            raise RuntimeError("launcher readiness window expired")

        start = Mock(side_effect=slow_start)
        self._replacement_ready.side_effect = [False, True, True]
        with patch.object(restart.time, "sleep"):
            self.assertTrue(self.run_restart(start))
            self.assertTrue(self.run_restart(start))
        start.assert_called_once_with(8765, self.state["url"])
        self._stop_if_idle.assert_called_once()
        self.assertEqual(self._replacement_ready.call_args.args[0]["pid"], 812)

    def test_lost_launch_receipt_prevents_another_start_on_resume(self):
        start = Mock(side_effect=OSError("launch outcome unavailable"))
        for _ in range(2):
            with patch.object(restart.time, "monotonic", side_effect=[0, 181]):
                with self.assertRaisesRegex(RuntimeError, "回执"):
                    self.run_restart(start)
        start.assert_called_once()
        self._stop_if_idle.assert_called_once()

    def test_trace_failure_stops_before_readiness_check(self):
        with self.assertRaises(TraceError):
            self.run_restart(Mock(side_effect=TraceError("write failed")))
        self._replacement_ready.assert_not_called()
