import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pi_bridge.runtime import PiLoop
from pianchang.agent_loop import Tool
from pianchang.bailian_text import TextConfig
from pi_bridge.requirements import seedance_request_limit
from pianchang.acceptance import create_contract
from pianchang.generation_groups import group_generation_tasks
from pianchang.video_agent import compile_shot
from pianchang.video_repair import authorized_video_count
from trace_system import Tracer, use_context


class GenerationGroupTests(unittest.TestCase):
    def test_one_request_keeps_three_creative_shots_and_both_cuts(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/story_project.json"
        project = json.loads(fixture.read_bytes())
        with tempfile.TemporaryDirectory() as directory, use_context(None, tracer=Tracer(Path(directory))):
            grouped = group_generation_tasks(project, 1)
            task, = grouped["shots"]
            self.assertEqual(task["creative_shots"], project["shots"])
            compiled = compile_shot(grouped, task["id"])
            self.assertEqual(compiled["duration"], 15)
            self.assertIn("有切镜", compiled["prompt"])
            self.assertEqual(sorted(compiled["prompt"].index(s["id"]) for s in project["shots"]),
                             [compiled["prompt"].index(s["id"]) for s in project["shots"]])
            checks = create_contract(grouped, task["id"])["requirements"]
            self.assertEqual([r["creative_pair"] for r in checks if r["kind"]=="internal_cut" and r["required"]],
                             [["S001","S002"],["S002","S003"]])
            self.assertEqual(sum(r["kind"]=="creative_shot" and r["required"] for r in checks), 3)

    def test_nine_to_three_retakes_share_cap(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/story_project.json"
        project = json.loads(fixture.read_bytes())
        first = project["shots"][0]
        project["shots"] = [dict(copy.deepcopy(first), id=f"S{i+1:03d}", script_scene_id=f"SC{i//3+1:03d}",
                                 duration_s=5, continuity_from=[]) for i in range(9)]
        limit = seedance_request_limit("三场戏，向 Seedance 视频模型发送三次视频生成请求")
        self.assertEqual(limit, 3)
        grouped = group_generation_tasks(project, limit)
        tasks = [s["id"] for s in grouped["shots"]]
        self.assertEqual(tasks, ["G001","G002","G003"])
        plan = dict(new_shots=tasks, retake_shots=[], generation_request_limit=limit,
                    acceptance_loop=dict(shot_ids=tasks, max_repairs_per_shot=1))
        self.assertEqual(authorized_video_count(plan), 3)
        self.assertEqual(authorized_video_count(dict(plan,new_shots=[],retake_shots=tasks)), 3)
        with self.assertRaises(ValueError):
            authorized_video_count(dict(plan,new_shots=tasks+["G004"]))

    def test_runtime_rejects_fourth_dispatch_including_repair(self):
        calls = []
        schema = {"type":"object","properties":{"shot_id":{"type":"string"}},"required":["shot_id"]}
        tools = [Tool(name, "offline", schema, lambda shot_id: calls.append(shot_id), effect="video")
                 for name in ("generate_shot", "regenerate_shot")]
        with tempfile.TemporaryDirectory() as directory, use_context(None, tracer=Tracer(Path(directory)/"traces")):
            work = Path(directory)/"run"
            work.mkdir()
            loop = PiLoop(SimpleNamespace(config=TextConfig(api_key="offline")), tools, work,
                          snapshot={}, finish_check=lambda: {"complete":False})
            ids = ["G001","G002","G003"]
            loop.record = {"limits":{"max_video_generations":3},"video_generations":0,"messages":[],
                           "shooting_plan":{"new_shots":ids,"acceptance_loop":{"shot_ids":ids,"max_repairs_per_shot":1}},
                           "reserved_shots":[],"repair_reservations":{}}
            for sid in ids:
                loop.execute("generate_shot", {"shot_id":sid}, sid)
            with self.assertRaisesRegex(ValueError, "额度"):
                loop.execute("regenerate_shot", {"shot_id":ids[0]}, "fourth")
            self.assertEqual(calls, ids)
            self.assertEqual(json.loads((work/"run.json").read_bytes())["video_generations"], 3)
