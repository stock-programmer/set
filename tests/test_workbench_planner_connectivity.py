from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools import workbench_loop_connectivity as network
from trace_system import get_tracer, use_context


class PlannerConnectivityTests(unittest.TestCase):
    def test_unsubmitted_planner_on_post_only_route(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tracer = get_tracer(root/"traces")
            route = SimpleNamespace(adapter="litellm", sdk_provider="openai", model_id="planner",
                                    base_url="https://api.test/v1")
            url = route.base_url+"/chat/completions"
            proxy = {"mode":"proxy","target_host":"api.test","proxy_scheme":"http","proxy_host":"127.0.0.1","proxy_port":8080}
            proof = {"request_kind":"model","dispatch":"not_sent","phase":"start_tls","error_type":"ConnectError",
                     "model_id":"planner","trace_id":"1"*32,"span_id":"2"*32,"request_seq":15,"error_seq":24,
                     "url_digest":network.digest_json(url),"proxy_mode":"proxy",**proxy}
            with (use_context(None,tracer=tracer), tracer.trace("test.planner.probe"),
                  patch.object(network,"load_routes") as config, patch.object(network,"_request_route") as wire):
                config.return_value.resolve.return_value = route
                for change in ({"dispatch":"unknown"},{"phase":"send_request_body"},{"model_id":"other"},
                               {"target_host":"wrong.test"},{"response_seq":22}):
                    self.assertEqual(network.check_planner_route(root,{**proof,**change})["status"],"retry_later")
                wire.assert_not_called()
                def reply(address, label, method, *, include_route):
                    status = {(url, "HEAD"):404, (route.base_url+"/models","GET"):401}[(address,method)]
                    return {"http_status":status,"route":proxy}
                wire.side_effect = reply
                received = network.check_planner_route(root,proof)
                self.assertEqual((received["status"],received["same_proxy"],received["catalog_http_status"]),
                                 ("reachable",True,401))
                self.assertEqual(network.check_planner_route(root,proof),received)
                self.assertEqual(wire.call_count,2)
