"""Behavioral tests of continuity and version failures, without AI mocks."""
import json
import unittest
from pathlib import Path

from pianchang.production import impact, validate


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.project = json.loads((Path(__file__).resolve().parents[1] / "examples" / "example.json").read_text(encoding="utf-8"))

    def codes(self):
        return {i["code"] for i in validate(self.project)["issues"]}

    def test_valid_spec_never_claims_visual_success(self):
        result = validate(self.project)
        self.assertTrue(result["spec_pass"])
        self.assertIsNone(result["visual_pass"])
        self.assertFalse(result["media_generated"])
        self.assertEqual(result["final_declared_state"]["prop:key.holder"], "char:chen")

    def test_prop_cannot_return_to_previous_owner_without_event(self):
        self.project["shots"][2]["requires"]["prop:key.holder"] = "char:lin"
        self.assertIn("CONTINUITY", self.codes())

    def test_character_cannot_know_unrevealed_information(self):
        self.project["shots"][2]["requires"]["char:chen.knows_key_origin"] = True
        self.assertIn("CONTINUITY", self.codes())

    def test_event_cannot_move_canonical_door(self):
        self.project["events"][0]["changes"]["scene:office.door_wall"] = "east"
        self.assertIn("IMMUTABLE_FACT", self.codes())

    def test_shot_cannot_assert_unexplained_change(self):
        self.project["shots"][0]["expected_after"]["prop:key.holder"] = "char:chen"
        self.assertIn("UNEXPLAINED_CHANGE", self.codes())

    def test_stale_character_version_is_detected(self):
        self.project["entities"]["char:lin"]["version"] = 2
        self.assertIn("STALE_INPUT", self.codes())

    def test_unknown_character_reference_is_detected(self):
        self.project["shots"][0]["entity_ids"].append("char:unknown")
        self.assertIn("UNKNOWN_ENTITY", self.codes())

    def test_failed_shot_does_not_commit_event(self):
        self.project["shots"][1]["expected_after"]["char:chen.knows_key_origin"] = True
        result = validate(self.project)
        self.assertFalse(result["state_trace"][1]["committed"])
        self.assertEqual(result["final_declared_state"]["prop:key.holder"], "char:lin")

    def test_event_is_not_replayed(self):
        self.project["shots"][2]["event_ids"].append("event:handover")
        self.assertIn("EVENT_REPLAY", self.codes())

    def test_changed_input_finds_direct_and_downstream_rechecks(self):
        result = impact(self.project, "char:lin")
        self.assertEqual(result["direct_recheck"], ["S001", "S002"])
        self.assertEqual(result["downstream_recheck"], ["S003"])

    def test_empty_production_cannot_pass(self):
        self.project["shots"] = []
        self.assertIn("NO_SHOTS", self.codes())

    def test_missing_input_pin_is_detected(self):
        del self.project["shots"][0]["input_versions"]["scene:office"]
        self.assertIn("UNPINNED_INPUT", self.codes())

    def test_boolean_is_not_a_numeric_version(self):
        self.project["shots"][0]["input_versions"]["char:lin"] = True
        self.assertIn("STALE_INPUT", self.codes())

    def test_flashback_is_explicitly_unsupported_in_v01(self):
        self.project["timeline_mode"] = "flashback"
        self.assertIn("UNSUPPORTED_TIMELINE", self.codes())


if __name__ == "__main__":
    unittest.main()
