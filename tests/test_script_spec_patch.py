import json
import tempfile
import unittest
from pathlib import Path

from pianchang.agent_loop import _validate
from pi_bridge.script_tools import SPEC_PATCH, StoryTools
from trace_system import get_tracer, use_context


class ScriptPatchTests(unittest.TestCase):
    def test_invalid_legacy_batch_then_structured_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = StoryTools.__new__(StoryTools)
            workspace.directory = Path(tmp)
            workspace.spec_path = Path(tmp) / "spec-candidate.json"
            workspace.spec = None
            workspace.script = None
            workspace.retry_patch_step = None
            tracer = get_tracer(Path(tmp) / "traces")
            with use_context(None, tracer=tracer), tracer.trace("test.patch.retry"):
                change = {"path":["shots"], "value":[{"description":'屏幕显示“收盘”。'}]}
                with self.assertRaisesRegex(ValueError, "未写入"):
                    workspace.patch_spec(json.dumps([change])[:-1])
                self.assertFalse(workspace.spec_path.exists())
                workspace.patch_spec('[{"path":["title"],"value":"已有标题"}]')
                self.assertFalse(workspace.stop_check({"steps":3})["allowed"])
                args = {"changes":[change]}
                _validate(args, SPEC_PATCH)
                workspace.patch_spec(**args)
                self.assertTrue(workspace.stop_check({"steps":3})["allowed"])
                workspace.patch_spec(changes=[{"path":["shots",1],"value":{"description":"第二场"}}])
                saved = json.loads(workspace.spec_path.read_text("utf-8-sig"))
                self.assertEqual(len(saved["shots"]), 2)
                self.assertEqual(saved["title"], "已有标题")
                with self.assertRaises(ValueError):
                    workspace.patch_spec(changes=[{"path":["script_source"],"value":{}}])
                self.assertEqual(saved, workspace.spec)
