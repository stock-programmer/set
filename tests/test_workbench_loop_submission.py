import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.workbench_loop_voice import ImageRecoveryRejected, _submit


class WorkbenchRecoverySubmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        web = self.root / "outputs/web"
        web.mkdir(parents=True)
        (web / "server-process.json").write_text('{"url":"http://127.0.0.1:8765"}', encoding="utf-8")
        self.job = {"id": "a" * 32, "project_id": "b" * 16}
        self.enterContext(patch("tools.workbench_loop_voice.workbench_http_identity", return_value=True))
        self.events = self.enterContext(patch("tools.workbench_loop_voice.event"))
        self.conn = self.enterContext(patch("tools.workbench_loop_voice.http.client.HTTPConnection")).return_value

    def reply(self, status, data):
        response = Mock(status=status)
        response.read.return_value = json.dumps(data).encode()
        self.conn.getresponse.return_value = response

    def submit(self, action="asset-production"):
        return _submit(self.root, self.job, action, {"resume_images_from": self.job["id"]}, "same-intent")

    def test_verified_image_rejections_and_legacy_replies(self):
        for status, body, expected in [
                (409, {"code": "image_scope_changed", "error": "已换版"}, "image_scope_changed"),
                (409, {"code": "image_route_changed", "error": "已切换"}, "image_route_changed"),
                (409, {"code": "image_transport_unavailable", "error": "暂不可达"}, "image_transport_unavailable"),
                (400, {"error": "原工单没有仍缺少素材且供应商已明确拒绝的参考图。"}, "image_scope_changed"),
                (400, {"error": "当前参考图路由与已核对的模型不一致。"}, "image_route_changed"),
                (409, {"code": "other", "error": "参数冲突"}, "image_command_rejected"),
                (400, {"error": "参数无效"}, "image_command_rejected")]:
            with self.subTest(status=status, body=body):
                self.reply(status, body)
                with self.assertRaises(ImageRecoveryRejected) as caught:
                    self.submit()
                self.assertEqual((caught.exception.reason, caught.exception.http_status), (expected, status))
                responses = [call.args[1] for call in self.events.call_args_list
                             if call.args[0] == "workbench.images.resume.response"]
                self.assertEqual(responses[-1]["status"], status)

    def test_uncertain_receipt_keeps_identity_across_reconciliation(self):
        for status, data in [(500, {"code": "image_route_changed", "error": "服务异常"}),
                             (409, {}), (202, {"job": {"id": "f" * 32, "project_id": "wrong"}})]:
            self.reply(status, data)
            with self.assertRaises(ValueError) as caught:
                self.submit()
            self.assertNotIsInstance(caught.exception, ImageRecoveryRejected)
        self.conn.getresponse.side_effect = TimeoutError("offline timeout")
        with self.assertRaises(TimeoutError):
            self.submit()
        self.conn.getresponse.side_effect = None
        self.reply(202, {"job": {"id": "c" * 32, "kind": "asset_production", "project_id": self.job["project_id"]}})
        self.assertEqual(self.submit(), "c" * 32)
        self.assertEqual({call.kwargs["headers"]["Idempotency-Key"] for call in self.conn.request.call_args_list},
                         {"same-intent"})

    def test_voice_rejection_keeps_existing_exception(self):
        self.reply(409, {"code": "image_scope_changed", "error": "错误"})
        with self.assertRaises(ValueError) as caught:
            self.submit("asset-regenerate")
        self.assertNotIsInstance(caught.exception, ImageRecoveryRejected)
