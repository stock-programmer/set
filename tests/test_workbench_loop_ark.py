import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import workbench_loop_ark as ark
from trace_system import TraceError, get_tracer, use_context


OLD = "doubao-seedream-5-0-260128"
PRO = "doubao-seedream-5-0-pro-260628"
FLASH = "doubao-seedream-5-0-flash-260915"


def image(model, status="Published"):
    return {"id": model, "status": status, "task_type": ["text_to_image"], "domain": "ImageGeneration"}


class ArkRouteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(use_context(None, tracer=get_tracer(self.root/"traces")))
        self.enterContext(get_tracer(self.root/"traces").trace("test.ark.offline"))
        self.path = self.root/"config/models.json"
        self.path.parent.mkdir()
        self.cfg = {"version": 1, "extends": "bailian", "connections": {"ark": {
            "provider": "volcengine-ark", "account": "local-test", "base_url": ark._BASE, "api_key_env": "TEST_KEY"}},
            "models": {"test-image": {"connection": "ark", "adapter": "litellm", "sdk_provider": "openai", "model_id": OLD}},
            "routes": {"reference.image": "test-image"}}
        self.path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        (self.root/".env").write_text("TEST_KEY=fixture-account-credential", encoding="utf-8")
        self.fetch = self.enterContext(patch.object(ark, "_fetch", return_value=(200, {"data": [
            image(OLD, "Retiring"), image(PRO), image(FLASH), image("doubao-seedream-5-5-261028")]})))
        self.endpoints = self.enterContext(patch.object(ark, "_endpoints", return_value=("management_credentials_unavailable", {})))

    def inspect(self, rejected=None, supplied=None):
        return ark.inspect_ark_image_route(self.root, rejected if rejected is not None else [OLD], supplied_model=supplied)

    def test_migration_requires_visible_live_image_capabilities_then_patches_one_value(self):
        before = self.path.read_bytes()
        evidence = self.inspect()
        self.assertEqual((evidence["reason"], evidence["model_id"], evidence["candidates"]),
                         ("published_successor", PRO, [PRO, FLASH]))
        self.assertFalse(evidence["access_verified"])
        self.assertEqual(self.fetch.call_args.args[0], "GET")
        result = ark.apply_ark_image_route(self.root, evidence)
        self.assertEqual(result, {"status": "ready", "changed": True, "model_id": PRO})
        self.assertEqual(self.path.read_bytes(), before.replace(OLD.encode(), PRO.encode()))
        ready = ark.apply_ark_image_route(self.root, self.inspect())
        self.assertEqual(ready, {"status": "ready", "changed": False, "model_id": PRO})
        self.fetch.return_value = 200, {"data": [{**image(PRO), "api_supported": {"images/generations": False}},
            image(FLASH, "Retiring"), {**image("doubao-seedream-5-0-lite-260120"), "task_type": ["image_to_text"]}]}
        self.assertEqual(self.inspect()["status"], "needs_input")

    def test_does_not_overwrite_concurrent_changes_or_shared_aliases(self):
        evidence = self.inspect()
        self.cfg["routes"]["voice.synthesis"] = "test-image"
        occupied = "test-image-reference-" + ark._sha(PRO.encode())[:12]
        self.cfg["models"][occupied] = dict(self.cfg["models"]["test-image"])
        self.path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.assertEqual(ark.apply_ark_image_route(self.root, evidence)["reason"], "configuration_changed")
        ark.apply_ark_image_route(self.root, self.inspect())
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after["routes"]["voice.synthesis"], "test-image")
        self.assertEqual(after["models"]["test-image"]["model_id"], OLD)
        self.assertEqual(after["models"][occupied]["model_id"], OLD)
        self.assertEqual(after["models"][after["routes"]["reference.image"]]["model_id"], PRO)

    def test_excludes_refused_routes_and_accepts_only_an_attested_endpoint_or_visible_image(self):
        self.assertEqual(self.inspect([OLD, PRO])["model_id"], FLASH)
        self.assertEqual(self.inspect([OLD, PRO, FLASH])["reason"], "provider_access")
        evidence = self.inspect({OLD: "InvalidEndpointOrModel.ModelIDAccessDisabled"})
        self.assertEqual(evidence["status"], "needs_input")
        self.assertEqual(self.inspect(supplied="https://thirdparty.invalid")["reason"], "invalid_supplied_model")
        self.assertEqual(self.inspect(supplied="ep-verified-by-user")["model_id"], "ep-verified-by-user")
        self.endpoints.return_value = "listed", {"ep-live": OLD, "ep-successor": PRO}
        self.assertEqual(self.inspect()["model_id"], "ep-live")
        self.assertEqual(self.inspect({OLD: "InvalidEndpointOrModel.ModelIDAccessDisabled"})["candidates"],
                         ["ep-live", "ep-successor"])
        self.fetch.return_value = 403, {}
        self.endpoints.return_value = "listed", {"ep-successor": PRO}
        self.assertEqual(self.inspect()["candidates"], ["ep-successor"])

    def test_authentication_failure_is_distinct_from_directory_outage(self):
        for http, status, reason in [(401, "needs_input", "invalid_api_key"),
                (403, "needs_input", "catalog_permission_denied"), (500, "retry_later", "catalog_unavailable"),
                (None, "retry_later", "catalog_unavailable")]:
            self.fetch.return_value = http, {}
            with self.subTest(http=http):
                evidence = self.inspect()
                self.assertEqual((evidence["status"], evidence["reason"]), (status, reason))
        self.cfg["connections"]["ark"]["base_url"] = "https://unrelated.example/api/v3"
        self.path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.fetch.reset_mock()
        self.assertEqual(self.inspect()["reason"], "unsupported_image_connection")
        self.fetch.assert_not_called()

    def test_endpoint_catalog_requires_running_and_matching_model_and_handles_pagination(self):
        # Test the actual signed control-plane reader independently of the mocked inspector.
        with patch.object(ark, "_endpoint_page", side_effect=[
                (200, {"Result": {"Items": [{"Status": "Stopped"}] * 100, "TotalCount": 101}}),
                (200, {"Result": {"Items": [{"Status": "Running", "Id": "ep-active", "ModelReference": {
                    "FoundationModel": {"Name": "doubao-seedream-5-0-pro", "ModelVersion": "260628"}}}],
                                 "TotalCount": 101}})]) as pages, patch.object(ark, "_management_credentials", return_value=("ak", "sk", None)):
            result = self.read_endpoints(self.root)
        self.assertEqual(result, ("listed", {"ep-active": PRO}))
        self.assertEqual(pages.call_count, 2)

    read_endpoints = staticmethod(ark._endpoints)

    def test_trace_failure_before_write_leaves_config_intact(self):
        evidence = self.inspect()
        before = self.path.read_bytes()
        with patch.object(ark, "event", side_effect=TraceError("offline")):
            with self.assertRaises(TraceError):
                ark.apply_ark_image_route(self.root, evidence)
        self.assertEqual(self.path.read_bytes(), before)

    def test_transport_probe_requires_exact_unsent_origin_and_current_model(self):
        url = ark._BASE + "/images/generations"
        proof = {"request_kind": "image", "model_id": OLD, "url_digest": ark.digest_json(url),
                 "dispatch": "not_sent", "phase": "start_tls", "error_type": "ConnectError", "trace_id": "a"*32,
                 "span_id": "b"*32, "request_seq": 17, "response_seq": None}
        with patch("tools.workbench_loop_connectivity._head_route") as head:
            for edit in ({"dispatch":"unknown"}, {"phase":"send_request_headers"}, {"model_id":FLASH},
                         {"url_digest":ark.digest_json(ark._BASE + "/models")}, {"response_seq":21}):
                self.assertEqual(ark.probe_image_transport(self.root, {**proof, **edit})["status"], "retry_later")
            head.assert_not_called()
            head.return_value = {"status":"http_response", "http_status":401}
            result = ark.probe_image_transport(self.root, proof)
            self.assertEqual((result["status"], result["http_status"]), ("reachable", 401))
            head.assert_called_once_with(url, "workbench.image_probe")
            head.return_value = {"status":"http_response", "http_status":502}
            self.assertEqual(ark.probe_image_transport(self.root, proof)["status"], "retry_later")
