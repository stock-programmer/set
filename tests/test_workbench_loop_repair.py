import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import workbench_loop_repair as repair
from trace_system import get_tracer, use_context


class RepairTests(unittest.TestCase):
    def test_tools_frame_uses_its_importing_suite_and_preserves_trace_failure_handler(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/"tools").mkdir()
            (root/"tests").mkdir()
            source=b"def recover():\n    try: work()\n    except TraceError: raise\n"
            (root/"tools/worker2.py").write_bytes(source)
            (root/"tests/test_restore_worker.py").write_text("from tools.worker2 import recover\n")
            bound=repair._evidence(root,{"frames":[{"file":"tools/worker2.py","function":"recover"}]})
            data,suite,_,location=bound["tools/worker2.py"]
            self.assertEqual((data,suite),(source,"tests.test_restore_worker"))
            self.assertIn("import recover",location["regression"]["source"])
            self.assertNotEqual(repair._gate_definitions(source),
                                repair._gate_definitions(source.replace(b"TraceError: raise",b"TraceError: pass")))

    def test_failed_suite_retains_actionable_locations_without_failure_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            tracer=get_tracer(root/"traces")
            def failed(cmd,**kwargs):
                kwargs["stdout"].write(b'FAIL: test_restore (tests.test_example.RestoreTests.test_restore)\n'
                    b'  File "/tmp/stage/tests/test_example.py", line 17, in test_restore\n'
                    b'AssertionError: unpublished story fragment\nRan 1 test in 0.02s\nFAILED (failures=1)\n')
                return subprocess.CompletedProcess(cmd,1)
            with use_context(None,tracer=tracer),tracer.trace("test.repair_feedback"), \
                    patch.object(repair.subprocess,"run",side_effect=failed),self.assertRaises(repair.RegressionFailed) as error:
                repair._offline_tests("codex",root,root,"tests.test_example",1)
            checks=error.exception.args[0]
            self.assertEqual((checks["failed_tests"],checks["lines"]),(["test_restore"],[17]))
            self.assertNotIn("unpublished",json.dumps(checks))

    def test_offline_proposal_checks_then_changes_the_original_hunk_only(self):
        for scenario in ("pass","test_fails","concurrent_write"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                path = root / "pianchang/example.py"
                path.parent.mkdir()
                path.write_bytes(b"# existing edit\r\nvalue = 1\r\n")
                (root/"tests").mkdir()
                (root/"tests/test_example.py").write_text("def test_restore_example(): pass",encoding="utf-8")
                fault = {"error_type":"ValueError","frames":[{"file":"pianchang/example.py","line":2}],
                         "message":"private user input must not reach the subprocess"}
                def propose(args,**kwargs):
                    self.assertNotIn(b"private user input", kwargs["input"])
                    self.assertIn(b"def test_restore_example()",kwargs["input"])
                    self.assertNotIn("ARK_VIDEO_API_KEY",kwargs["env"])
                    self.assertIn("--ephemeral",args)
                    output = Path(args[args.index("--output-last-message")+1])
                    output.write_text(json.dumps({"file":"pianchang/example.py",
                                                  "old":"value = 1","new":"value = 2"}))
                    return subprocess.CompletedProcess(args,0)
                def verify(cli,root,stage,suite,timeout):
                    self.assertEqual((stage/"pianchang/example.py").read_bytes(),b"# existing edit\r\nvalue = 2\r\n")
                    if scenario=="concurrent_write":
                        path.write_text("user edit")
                    return "offline_fixture" if scenario!="test_fails" else None
                tr=get_tracer(root/"traces")
                with use_context(None,tracer=tr),tr.trace("test.repair_fixture"), \
                        patch.dict(repair.os.environ,{"ARK_VIDEO_API_KEY":"private fixture"}), \
                        patch.object(repair.shutil,"which",return_value="/fake/codex"), \
                        patch.object(repair.subprocess,"run",side_effect=propose), \
                        patch.object(repair,"_offline_tests",side_effect=verify):
                    result=repair.repair_fault(root,fault)
                if scenario=="pass":
                    self.assertEqual(result["status"],"applied")
                    self.assertEqual(path.read_bytes(),b"# existing edit\r\nvalue = 2\r\n")
                else:
                    self.assertEqual(result["status"],"blocked")
                    self.assertEqual(path.read_bytes(),b"user edit" if scenario=="concurrent_write" else b"# existing edit\r\nvalue = 1\r\n")
