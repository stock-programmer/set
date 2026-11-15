"""Source integrity and declared-coverage tests; no model calls."""
import copy
import json
import unittest
from pathlib import Path

from pianchang.story_spec import (StoryError, digest_json, index_sources, normalize_script,
                        validate_compilation, validate_script)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class StorySpecTests(unittest.TestCase):
    def setUp(self):
        self.script = json.loads((FIXTURES / "story_script.json").read_text(encoding="utf-8"))
        self.project = json.loads((FIXTURES / "story_project.json").read_text(encoding="utf-8"))

    def codes(self, project=None, script=None):
        report = validate_compilation(self.project if project is None else project, script)
        return {i["code"] for i in report["issues"] if i["severity"] == "error"}

    def draft(self):
        script = self.script
        return {"title": script["title"], "logline": script["logline"], "outline": script["outline"],
                "setup": [s["text"] for s in script["setup"]],
                "episodes": [{"title": ep["title"], "scenes": [
                    {"title": sc["title"], "beats": [b["text"] for b in sc["beats"]]}
                    for sc in ep["scenes"]]} for ep in script["episodes"]]}

    def rebind(self, script):
        self.project["script_source"] = {"sha256": digest_json(script), "script": copy.deepcopy(script)}

    def test_complete_fixture_passes_without_claiming_semantic_or_visual_success(self):
        report = validate_compilation(self.project, self.script)
        for field in ("spec_pass", "provenance_pass", "coverage_pass", "ready_for_generation"):
            self.assertTrue(report[field], report["issues"])
        for field in ("semantic_fidelity_pass", "visual_pass", "audio_pass"):
            self.assertIsNone(report[field])
        self.assertFalse(report["media_generated"])
        self.assertEqual({r["source_id"]: r["shot_ids"] for r in report["coverage"]},
                         {"B001": ["S001"], "B002": ["S002"], "B003": ["S003"]})
        self.assertEqual(report["event_coverage"], [{"event_id": "event:handover", "shot_ids": ["S002"]}])
        self.assertEqual(report["state_trace"][-1]["after"]["prop:key.holder"], "char:chen")

    def test_normalizer_assigns_global_ids_and_required_beats(self):
        draft = self.draft()
        draft["episodes"].append({"title": "第二集", "scenes": [{"title": "新场次", "beats": ["陈默仍握着钥匙。"]}]})
        script = normalize_script(draft, idea="验证顺序编号。", script_version=2)
        self.assertEqual(validate_script(script), [])
        self.assertEqual(script["episodes"][1]["id"], "EP002")
        self.assertEqual(script["episodes"][1]["scenes"][0]["id"], "SC002")
        beat = script["episodes"][1]["scenes"][0]["beats"][0]
        self.assertEqual(beat, {"id": "B004", "text": "陈默仍握着钥匙。", "required": True})
        self.assertNotIn("id", draft["episodes"][0])

    def test_existing_normalized_snapshot_keeps_its_ids_and_is_detached(self):
        self.script["episodes"][0]["scenes"][0]["beats"][0]["id"] = "B100"
        result = normalize_script(self.script, idea="不能覆盖原始简报。", script_version=8)
        self.assertEqual(result, self.script)
        result["setup"][0]["text"] = "changed"
        self.assertNotEqual(result, self.script)

    def test_missing_idea_defaults_to_logline(self):
        script = normalize_script(self.draft())
        self.assertEqual(script["idea"], script["logline"])

    def test_model_cannot_mark_a_beat_optional(self):
        self.script["episodes"][0]["scenes"][0]["beats"][0]["required"] = False
        self.assertIn("SCRIPT_REQUIRED", {i["code"] for i in validate_script(self.script)})
        with self.assertRaises(StoryError):
            normalize_script(self.script)
        draft = self.draft()
        draft["episodes"][0]["scenes"][0]["beats"] = [{"text": "删掉这个要求", "required": False}]
        with self.assertRaises(StoryError):
            normalize_script(draft)

    def test_duplicate_source_ids_are_invalid(self):
        beats = self.script["episodes"][0]["scenes"][0]["beats"]
        beats[1]["id"] = beats[0]["id"]
        self.assertIn("SCRIPT_DUPLICATE_ID", {i["code"] for i in validate_script(self.script)})
        with self.assertRaises(StoryError):
            index_sources(self.script)

    def test_source_index_retains_exact_text_and_locations(self):
        sources = index_sources(self.script)
        self.assertEqual(sources["SET001"]["text"], self.script["setup"][0]["text"])
        self.assertIsNone(sources["SET001"]["episode_id"])
        self.assertFalse(sources["SET001"]["required"])
        self.assertEqual(sources["B002"]["scene_id"], "SC001")
        self.assertEqual(sources["B002"]["episode_id"], "EP001")
        self.assertLess(sources["B001"]["order"], sources["B002"]["order"])

    def test_source_index_includes_brief_logline_and_outline_as_optional_context(self):
        sources = index_sources(self.script)
        for source_id, field in (("SRC_IDEA", "idea"), ("SRC_LOGLINE", "logline"),
                                 ("SRC_OUTLINE", "outline")):
            with self.subTest(source_id=source_id):
                source = sources[source_id]
                self.assertEqual(source["text"], self.script[field])
                self.assertEqual(source["kind"], field)
                self.assertFalse(source["required"])
                self.assertIsNone(source["episode_id"])
                self.assertIsNone(source["scene_id"])
                self.assertLess(source["order"], sources["SET001"]["order"])

    def test_entities_and_facts_can_cite_exact_context_source_text(self):
        sources = index_sources(self.script)
        for group, key, source_id in (
                ("entities", "char:lin", "SRC_IDEA"),
                ("facts", "prop:key.holder", "SRC_LOGLINE"),
                ("entities", "scene:office", "SRC_OUTLINE")):
            self.project[group][key]["source_refs"] = [
                {"source_id": source_id, "quote": sources[source_id]["text"]}]
        report = validate_compilation(self.project)
        self.assertTrue(report["ready_for_generation"], report["issues"])
        self.assertIsNone(report["semantic_fidelity_pass"])
        self.assertEqual({item["source_id"] for item in report["coverage"]},
                         {"B001", "B002", "B003"})

    def test_outline_only_shot_does_not_cover_a_required_beat(self):
        self.project["shots"][0]["source_refs"] = [
            {"source_id": "SRC_OUTLINE", "quote": self.script["outline"]}]
        report = validate_compilation(self.project)
        self.assertTrue(report["provenance_pass"], report["issues"])
        self.assertFalse(report["coverage_pass"])
        self.assertFalse(report["ready_for_generation"])
        self.assertIn("SHOT_WITHOUT_BEAT", self.codes())
        self.assertIn("UNCOVERED_BEAT", self.codes())
        self.assertEqual(next(item["shot_ids"] for item in report["coverage"]
                              if item["source_id"] == "B001"), [])

    def test_outline_only_event_does_not_replace_its_beat_source(self):
        self.project["events"][0]["source_refs"] = [
            {"source_id": "SRC_OUTLINE", "quote": self.script["outline"]}]
        report = validate_compilation(self.project)
        self.assertTrue(report["provenance_pass"], report["issues"])
        self.assertFalse(report["coverage_pass"])
        self.assertFalse(report["ready_for_generation"])
        self.assertIn("EVENT_WITHOUT_BEAT", self.codes())

    def test_limits_reject_oversized_drafts(self):
        draft = self.draft()
        draft["episodes"] *= 5
        with self.assertRaises(StoryError):
            normalize_script(draft)
        draft = self.draft()
        draft["episodes"][0]["scenes"] *= 13
        with self.assertRaises(StoryError):
            normalize_script(draft)
        draft = self.draft()
        draft["episodes"][0]["scenes"][0]["beats"] *= 3
        with self.assertRaises(StoryError):
            normalize_script(draft)
        draft = self.draft()
        draft["episodes"][0]["scenes"] = [{"title": "场次", "beats": ["行动。"] * 7} for _ in range(7)]
        with self.assertRaises(StoryError):
            normalize_script(draft)

    def test_exact_contiguous_quote_is_required(self):
        self.project["shots"][0]["source_refs"][0]["quote"] = "林岚给陈默一把银钥匙"
        report = validate_compilation(self.project)
        self.assertFalse(report["provenance_pass"])
        self.assertFalse(report["coverage_pass"])
        self.assertIn("SOURCE_QUOTE", self.codes())
        self.assertIn("UNCOVERED_BEAT", self.codes())

    def test_empty_quote_and_unknown_source_cannot_count_as_coverage(self):
        for replacement in ({"source_id": "B001", "quote": " "},
                            {"source_id": "B999", "quote": "林岚"}):
            with self.subTest(replacement=replacement):
                project = copy.deepcopy(self.project)
                project["shots"][0]["source_refs"] = [replacement]
                result = validate_compilation(project)
                self.assertFalse(result["provenance_pass"])
                self.assertFalse(result["coverage_pass"])

    def test_every_entity_fact_event_and_shot_needs_source_refs(self):
        for group, key in (("entities", "char:lin"), ("facts", "prop:key.holder"), ("events", 0), ("shots", 0)):
            with self.subTest(group=group):
                project = copy.deepcopy(self.project)
                del project[group][key]["source_refs"]
                self.assertIn("MISSING_SOURCE_REFS", self.codes(project))

    def test_deleting_a_required_beat_shot_fails_even_if_state_spec_passes(self):
        self.project["shots"].pop()
        result = validate_compilation(self.project)
        self.assertTrue(result["spec_pass"])
        self.assertFalse(result["coverage_pass"])
        self.assertFalse(result["ready_for_generation"])
        self.assertIn("UNCOVERED_BEAT", self.codes())

    def test_compiler_cannot_delete_frozen_beat_and_rehash_when_external_source_is_supplied(self):
        altered = copy.deepcopy(self.script)
        altered["episodes"][0]["scenes"][0]["beats"].pop()
        self.rebind(altered)
        self.project["shots"].pop()
        self.assertIn("STALE_SCRIPT_SOURCE", self.codes(script=self.script))
        self.assertIn("UNCOVERED_BEAT", self.codes(script=self.script))

    def test_stale_hash_and_version_are_rejected(self):
        self.project["script_source"]["sha256"] = "0" * 64
        self.project["script_version"] = 2
        codes = self.codes()
        self.assertIn("STALE_SCRIPT_SOURCE", codes)
        self.assertIn("SCRIPT_VERSION_MISMATCH", codes)

    def test_modified_embedded_script_is_detected_with_external_source(self):
        self.project["script_source"]["script"]["title"] = "偷偷换剧本"
        self.assertIn("STALE_SCRIPT_SOURCE", self.codes(script=self.script))

    def test_event_must_execute_in_a_committed_shot(self):
        self.project["shots"][1]["event_ids"] = []
        self.assertIn("UNEXECUTED_EVENT", self.codes())
        self.assertFalse(validate_compilation(self.project)["coverage_pass"])

    def test_event_source_must_also_be_a_source_of_its_execution_shot(self):
        source = index_sources(self.script)["B001"]
        self.project["events"][0]["source_refs"] = [{"source_id": "B001", "quote": source["text"]}]
        result = validate_compilation(self.project)
        self.assertTrue(result["spec_pass"])
        self.assertIn("EVENT_SOURCE_NOT_IN_SHOT", self.codes())
        self.assertFalse(result["ready_for_generation"])

    def test_setup_only_event_or_shot_cannot_hide_missing_beats(self):
        refs = [{"source_id": "SET001", "quote": self.script["setup"][0]["text"]}]
        self.project["events"][0]["source_refs"] = refs
        self.project["shots"][0]["source_refs"] = refs
        codes = self.codes()
        self.assertIn("EVENT_WITHOUT_BEAT", codes)
        self.assertIn("SHOT_WITHOUT_BEAT", codes)
        self.assertIn("UNCOVERED_BEAT", codes)

    def test_cross_scene_beat_reference_cannot_count_for_a_shot(self):
        scenes = self.script["episodes"][0]["scenes"]
        third_beat = scenes[0]["beats"].pop()
        scenes.append({"id": "SC002", "title": "另一个场次", "beats": [third_beat]})
        self.rebind(self.script)
        self.assertIn("CROSS_SCENE_SOURCE", self.codes())
        self.assertIn("UNCOVERED_BEAT", self.codes())

    def test_cross_episode_location_is_rejected(self):
        self.project["shots"][0]["episode_id"] = "EP999"
        self.assertIn("SHOT_SOURCE_SCENE", self.codes())
        self.assertIn("CROSS_SCENE_SOURCE", self.codes())

    def test_one_script_scene_cannot_bind_to_two_scene_entities(self):
        self.project["entities"]["scene:other_office"] = copy.deepcopy(
            self.project["entities"]["scene:office"])
        shot = self.project["shots"][1]
        shot["scene_id"] = "scene:other_office"
        shot["input_versions"]["scene:other_office"] = 1
        report = validate_compilation(self.project)
        self.assertTrue(report["spec_pass"], report["issues"])
        self.assertFalse(report["provenance_pass"])
        self.assertFalse(report["ready_for_generation"])
        self.assertIn("SCENE_BINDING_CONFLICT", self.codes())

    def test_empty_facts_cannot_bypass_all_declared_state_checks(self):
        self.project["facts"] = {}
        self.project["events"] = []
        for shot in self.project["shots"]:
            shot["requires"] = {}
            shot["expected_after"] = {}
            shot["event_ids"] = []
        report = validate_compilation(self.project)
        self.assertTrue(report["coverage_pass"], report["issues"])
        self.assertTrue(report["provenance_pass"], report["issues"])
        self.assertFalse(report["spec_pass"])
        self.assertFalse(report["ready_for_generation"])
        self.assertIn("EMPTY_FACTS", self.codes())
        self.assertIsNone(report["semantic_fidelity_pass"])

    def test_reordered_beats_are_rejected_even_when_each_is_covered(self):
        refs = [s["source_refs"] for s in self.project["shots"]]
        self.project["shots"][0]["source_refs"] = refs[2]
        self.project["shots"][2]["source_refs"] = refs[0]
        report = validate_compilation(self.project)
        self.assertTrue(all(item["shot_ids"] for item in report["coverage"]))
        self.assertIn("SHOT_SOURCE_ORDER", self.codes())
        self.assertFalse(report["ready_for_generation"])

    def test_original_continuity_errors_still_block_generation(self):
        self.project["shots"][2]["requires"]["prop:key.holder"] = "char:lin"
        report = validate_compilation(self.project)
        self.assertFalse(report["spec_pass"])
        self.assertFalse(report["ready_for_generation"])
        self.assertIn("CONTINUITY", self.codes())

    def test_empty_descriptions_and_unsupported_durations_are_rejected(self):
        self.project["entities"]["char:lin"]["canonical"]["identity"] = " "
        self.project["shots"][0]["description"] = ""
        self.project["shots"][0]["duration_s"] = 1.5
        codes = self.codes()
        self.assertIn("EMPTY_CANONICAL", codes)
        self.assertIn("EMPTY_SHOT_DESCRIPTION", codes)
        self.assertIn("VIDEO_DURATION", codes)

    def test_malformed_llm_json_returns_report_or_story_error(self):
        for value in (None, [], "text", {}, {"episodes": [None]}):
            with self.subTest(value=value):
                with self.assertRaises(StoryError):
                    normalize_script(value)
                self.assertFalse(validate_compilation(value)["ready_for_generation"])
        for field, value in (("facts", {"bad": {}}), ("events", [None]),
                             ("shots", [{"id": [], "event_ids": [{}]}]),
                             ("entities", {"bad": {"canonical": None}})):
            with self.subTest(field=field):
                project = copy.deepcopy(self.project)
                project[field] = value
                self.assertFalse(validate_compilation(project)["ready_for_generation"])

    def test_hash_is_canonical_and_rejects_non_json_numbers(self):
        self.assertEqual(digest_json({"a": 1, "b": 2}), digest_json({"b": 2, "a": 1}))
        with self.assertRaises(StoryError):
            digest_json({"bad": float("nan")})


if __name__ == "__main__":
    unittest.main()
