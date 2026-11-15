"""Offline reference-asset contracts. The test media are generated locally."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave
import zlib

from pianchang.collab_store import CollabStore
from pianchang.production import validate
from pianchang.reference_assets import _blob_path, bind_project, import_reference, main, pin_issues, resolve_references
from trace_system import Tracer, use_context


def offline_png(pixel):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))

    headers = struct.pack(">2I5B", 4, 3, 8, 2, 0, 0, 0)
    body = b"".join(b"\x00" + bytes(pixel) * 4 for _ in range(3))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", headers) + chunk(b"IDAT", zlib.compress(body)) + chunk(b"IEND", b"")


class ReferenceAssetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.tracer = Tracer(self.root / "offline-trace")
        binding = use_context(None, tracer=self.tracer)
        binding.__enter__()
        self.addCleanup(binding.__exit__, None, None, None)
        self.store = CollabStore(self.root / "store.sqlite3")
        self.image = self.root / "actor.png"
        self.image.write_bytes(offline_png((30, 40, 50)))
        self.project = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json").read_text(encoding="utf8"))

    def register(self, asset_id="portrait", story_id="story", parent_revision=None):
        return import_reference(self.store, story_id=story_id, asset_id=asset_id, subject_id="char:lin",
                                purpose="portrait", path=self.image, parent_revision=parent_revision)

    def test_source_changes_preserve_r1_bytes_and_r1_bindings(self):
        first = self.register()
        original = self.image.read_bytes()
        self.image.write_bytes(offline_png((90, 20, 30)))
        second = self.register(parent_revision="r1")
        self.image.unlink()
        first_resolved, = resolve_references(self.store, [first], story_id="story")
        self.assertEqual(base64.b64decode(first_resolved["provider_url"].split(",", 1)[1]), original)
        self.assertEqual(first["media_sha256"], hashlib.sha256(original).hexdigest())
        self.assertNotEqual(first["media_sha256"], first["sha256"])
        self.assertEqual((first_resolved["width"], first_resolved["height"]), (4, 3))
        self.assertEqual((first["revision_id"], second["revision_id"]), ("r1", "r2"))
        old_project = copy.deepcopy(self.project)
        v1 = bind_project(self.store, self.project, story_id="story", shot_id="S002",
                          pins=[first], spec_asset_id="spec")
        v2 = bind_project(self.store, v1["content"], story_id="story", shot_id="S002",
                          pins=[second], spec_asset_id="spec", parent_revision="r1")
        self.assertEqual(old_project, self.project)
        self.assertEqual(v1["content"]["shots"][1]["required_refs"], [first])
        self.assertEqual(v2["content"]["shots"][1]["required_refs"], [second])
        self.assertEqual(v2["content"]["reference_story_id"], "story")
        self.assertEqual([r["role"] for r in self.store.dependents("portrait","r1")], ["required_reference"])
        self.assertEqual(self.store.dependents("portrait","r2")[0]["source_id"], "S002")
        with self.assertRaisesRegex(ValueError, "bind.base"):
            bind_project(self.store, self.project, story_id="story", shot_id="S002",
                         pins=[first], spec_asset_id="spec", parent_revision="r2")

    def test_tampered_blob_forged_revision_and_wrong_story_are_blocked(self):
        pin = self.register()
        with self.assertRaisesRegex(ValueError, "reference.story"):
            resolve_references(self.store, [pin], story_id="other")
        second = self.register(asset_id="other-portrait", story_id="other")
        with self.assertRaisesRegex(ValueError, "reference.story"):
            resolve_references(self.store, [pin, second])
        for fields in ({"sha256":"0" * 64}, {"subject_id":"char:chen"}, {"purpose":"voice"}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "reference.pin"):
                resolve_references(self.store, [{**pin, **fields}], story_id="story")
        _blob_path(self.store, pin["media_sha256"]).write_bytes(offline_png((0,0,0)))
        with self.assertRaisesRegex(ValueError, "reference.media_sha256"):
            resolve_references(self.store, [pin])

    def test_actual_audio_and_subject_scope(self):
        audio = self.root / "opaque.audio"
        with wave.open(str(audio), "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x00" * 9600)
        voice = import_reference(self.store, asset_id="voice", story_id="story", subject_id="char:lin",
                                 purpose="voice", path=audio)
        result, = resolve_references(self.store, [voice])
        self.assertEqual(result["mime_type"], "audio/wav")
        self.assertAlmostEqual(result["duration_s"], 1.2)
        wrong = copy.deepcopy(self.project)
        wrong["shots"][-1]["required_refs"] = [{**voice, "subject_id":wrong["shots"][-1]["scene_id"]}]
        self.assertIn("INVALID_REQUIRED_REFERENCE", [i["code"] for i in validate(wrong)["issues"]])
        self.assertTrue(pin_issues([voice, voice]))
        with self.assertRaisesRegex(ValueError, "reference.media_type"):
            import_reference(self.store, story_id="story", asset_id="not-voice", subject_id="char:lin",
                             purpose="voice", path=self.image)

    def test_bind_cli_exports_new_project_and_preserves_legacy_declarations(self):
        pin = self.register()
        source = self.root / "original.json"
        output = self.root / "with-pin.json"
        self.project["entities"]["char:lin"]["references"] = ["legacy artist note"]
        self.assertIn("UNBOUND_REFERENCE", [i["code"] for i in validate(self.project)["issues"]])
        original_bytes = json.dumps(self.project).encode()
        source.write_bytes(original_bytes)
        args = ["--store", str(self.store.path), "bind", "--story-id", "story", "--shot-id", "S002",
                "--reference", "portrait", "r1", "--spec-asset-id", "spec", "--project", str(source)]
        with self.assertRaisesRegex(ValueError, "已存在"):
            main([*args, "--output", str(source)])
        self.assertEqual(main([*args, "--output", str(output)]), 0)
        spec = json.loads(output.read_text(encoding="utf8"))
        self.assertEqual(spec["shots"][1]["required_refs"], [pin])
        self.assertNotIn("required_refs", spec["shots"][0])
        self.assertEqual(spec["entities"]["char:lin"]["references"], ["legacy artist note"])
        self.assertEqual(source.read_bytes(), original_bytes)
