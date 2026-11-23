"""Offline durable inbox and story isolation checks; no provider access."""
import tempfile
import unittest
from pathlib import Path
from pi_bridge.repository import SessionRepository
from trace_system import get_tracer,use_context
from pi_bridge.requirements import check_quantities


class PiSessionTests(unittest.TestCase):
    def test_one_fifteen_second_shot_is_not_three_five_second_shots(self):
        idea = "一个角色、一个场景、一个十五秒镜头，三个连续可拍的节拍"
        script = {"episodes":[{"scenes":[{"beats":[{},{},{}]}]}]}
        project = {"entities":{"actor":{"kind":"character"},"station":{"kind":"scene"}},
                   "shots":[{"duration_s":5} for _ in range(3)]}
        with tempfile.TemporaryDirectory() as folder:
            tracer=get_tracer(Path(folder)/"trace")
            with use_context(None,tracer=tracer), tracer.trace("test.pi.requested_duration"):
                checks = check_quantities(idea,script,project)
                self.assertEqual({r["path"] for r in checks}, {
                    "requested.characters","requested.scenes","requested.shots",
                    "requested.shot_duration_s","requested.beats"})
                failed = {r["path"]:(r["expected"],r["actual"],r["source_quote"])
                          for r in checks if r["status"]=="FAIL"}
                self.assertEqual(failed,{"requested.shots":(1,3,"一个十五秒镜头"),
                                         "requested.shot_duration_s":(15,5,"一个十五秒镜头")})
                project["shots"] = [{"duration_s":5}]
                self.assertEqual([r["path"] for r in check_quantities(idea,script,project)
                                  if r["status"]=="FAIL"],["requested.shot_duration_s"])
                project["shots"][0]["duration_s"] = 15
                self.assertTrue(all(r["status"]=="PASS" for r in check_quantities(idea,script,project)))
                for text in ("一场雨持续十五秒，路人看了三个镜头的回放",
                             "要求每场三个镜头", "要求第一集出现一个镜头", "要求一至三个镜头",
                             "要求拍三个镜头左右"):
                    with self.subTest(text=text):
                        self.assertEqual(check_quantities(text,script,project),[])
                checks = check_quantities("1个镜头，时长15秒",script,project)
                self.assertEqual([r["expected"] for r in checks],[1,15])
                self.assertEqual([r["path"] for r in check_quantities("全剧一集每集三场",script,project)],
                                 ["requested.episodes"])

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
                repo=SessionRepository(root/"pi");session=repo.ensure("story-a","video")
                seen="c"*32;unseen="d"*32
                repo.append(session["id"],"user","已进入Pi",message_id=seen,status="dispatching")
                repo.append(session["id"],"user","尚未进入Pi",message_id=unseen,status="dispatching")
                active_button = repo.append(session["id"],"user","继续原委托",job_id="interrupted",
                                            continue_commission="a"*32,status="dispatching")
                waiting_button = repo.append(session["id"],"user","排队的制作要求",job_id="waiting",
                                             continue_commission="b"*32)
                history=root/"session.jsonl";history.write_text('{"type":"message","message":{"role":"user","content":"[message_id='+seen+']"}}\n',encoding="utf-8")
                repo.update(session["id"],session_file=str(history),active_job="interrupted")
                restarted=SessionRepository(root/"pi");result=restarted.recover()
                states={m["id"]:m["status"] for m in restarted.messages(session["id"])}
                self.assertEqual(states[seen],"delivered");self.assertEqual(states[unseen],"queued")
                self.assertEqual(states[active_button["id"]],"interrupted")
                self.assertEqual(states[waiting_button["id"]],"queued")
                self.assertEqual(result["replayed_paid_operations"],0)
                self.assertIsNone(restarted.get(session["id"])["active_job"])

if __name__=="__main__":unittest.main()
