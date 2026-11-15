"""Offline durable inbox and story isolation checks; no provider access."""
import tempfile
import unittest
from pathlib import Path
from pi_bridge.repository import SessionRepository
from trace_system import get_tracer,use_context
from pi_bridge.requirements import check_quantities


class PiSessionTests(unittest.TestCase):
    def test_explicit_shot_count_does_not_disappear_at_resume(self):
        script={"episodes":[{"scenes":[{},{},{}]}]}
        with tempfile.TemporaryDirectory() as folder:
            tracer=get_tracer(Path(folder)/"trace")
            with use_context(None,tracer=tracer),tracer.trace("test.pi.requirements",inputs={"label":"offline"}):
                checks=check_quantities("校园故事，要求剧本只写一集三场三个镜头",script,{"shots":[{}]*9})
                failed=[r for r in checks if r["status"]=="FAIL"]
                self.assertEqual(len(failed),1);self.assertEqual(failed[0]["path"],"requested.shots")
                self.assertEqual(failed[0]["source_quote"],"三个镜头")
                self.assertTrue(all(r["status"]=="PASS" for r in check_quantities("要求一集三场三个镜头",script,{"shots":[{}]*3})))

    def test_identity_role_and_idempotency(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);tracer=get_tracer(root/"trace")
            with use_context(None,tracer=tracer),tracer.trace("test.pi.sessions",inputs={"label":"offline"}):
                repo=SessionRepository(root/"pi")
                first=repo.ensure("story-a","script",source_job="a"*32)
                self.assertEqual(first["id"],repo.ensure("story-a","script")["id"])
                self.assertNotEqual(first["id"],repo.ensure("story-a","video")["id"])
                self.assertNotEqual(first["id"],repo.ensure("story-b","script")["id"])
                message=repo.append(first["id"],"user","继续运行",message_id="b"*32)
                self.assertEqual(message,repo.append(first["id"],"user","继续运行",message_id="b"*32))
                with self.assertRaises(ValueError):repo.append(first["id"],"user","改写另一个故事",message_id="b"*32)
                with self.assertRaises(ValueError):repo.append(first["id"],"user","继续运行",message_id="b"*32,mode="inspect")
                self.assertEqual(len(repo.messages(first["id"])),1)
                updated=repo.ensure("story-a","script",project_id="p1")
                self.assertEqual(updated["id"],first["id"]);self.assertEqual(updated["project_id"],"p1")

    def test_restart_reconciles_actual_session_before_redelivery(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);tracer=get_tracer(root/"trace")
            with use_context(None,tracer=tracer),tracer.trace("test.pi.restart",inputs={"label":"offline historical session fixture"}):
                repo=SessionRepository(root/"pi");session=repo.ensure("story-a","script")
                seen="c"*32;unseen="d"*32
                repo.append(session["id"],"user","已进入Pi",message_id=seen,status="dispatching")
                repo.append(session["id"],"user","尚未进入Pi",message_id=unseen,status="dispatching")
                history=root/"session.jsonl";history.write_text('{"type":"message","message":{"role":"user","content":"[message_id='+seen+']"}}\n',encoding="utf-8")
                repo.update(session["id"],session_file=str(history),active_job="interrupted")
                restarted=SessionRepository(root/"pi");result=restarted.recover()
                states={m["id"]:m["status"] for m in restarted.messages(session["id"])}
                self.assertEqual(states[seen],"delivered");self.assertEqual(states[unseen],"queued")
                self.assertEqual(result["replayed_paid_operations"],0)
                self.assertIsNone(restarted.get(session["id"])["active_job"])

if __name__=="__main__":unittest.main()
