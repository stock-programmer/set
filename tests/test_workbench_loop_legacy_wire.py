import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools import workbench_loop_legacy_wire as legacy


class LegacyWireTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd()
        sdk = str(self.root / "pianchang/sdk_transport.py")
        transport = str(self.root / "packages/httpx/_transports/default.py")
        frames = [(sdk,100,"handle_request"), (sdk,134,"_send"), (sdk,56,"handle_request"),
                  (transport,249,"handle_request"), ("contextlib.py",155,"__exit__"),
                  (transport,118,"map_httpcore_exceptions")]
        message = "[Errno 2] No such file or directory"
        stack = "".join(f'  File "{p}", line {n}, in {fn}\n' for p,n,fn in frames)
        self.payloads = [
            {"attributes":{"module":"pianchang.sdk_transport", "qualname":"call_sdk", "source_file":sdk,
                           "source_sha256":legacy._SDK_SOURCE, "loaded_code_sha256":legacy._SDK_CODE}},
            {"operation":"completion","route":{"adapter_version":"1.109.1","sdk_provider":"openai","provider":"openai"}},
            {"method":"POST","url":"https://api.openai.com/v1/chat/completions",
             "headers":{"x-stainless-retry-count":"0","x-stainless-package-version":"3.52.0"},
             "body":{"modalities":["audio","text"],"audio":{}}},
            {"mode":"proxy","source":"windows","proxy_scheme":"http","target_host":"api.openai.com"},
            {"error_type":"ConnectError"},
            {"status":"ERROR","error":{"message":message,"type":"ConnectError","stack":stack+"httpx.ConnectError: "+message}},
        ]

    def rows(self, data):
        return [(i, kind, name, "2026-11-21T09:12:06+00:00", json.dumps(d))
                for i, ((kind,name),d) in enumerate(zip(legacy._EVENTS, data))]

    def test_complete_witness_and_uncertain_installation(self):
        rows = self.rows(self.payloads)
        with patch.object(legacy, "_installed_at", return_value=True):
            self.assertEqual(legacy._classify(self.root, rows, 2)["delivery"], "not_sent")
            self.assertIsNone(legacy._classify(self.root, rows + rows[-1:], 2))
        with patch.object(legacy, "_installed_at", return_value=False):
            self.assertIsNone(legacy._classify(self.root, rows, 2))

    def test_no_classification_from_error_label_alone(self):
        for index, path, value in (
            (0, ("attributes","loaded_code_sha256"), "other"),
            (2, ("headers","x-stainless-retry-count"), "1"),
            (4, ("error_type",), "ReadError"),
            (5, ("error","stack"), "httpx.ConnectError: [Errno 2] No such file or directory"),
        ):
            with self.subTest(path=path), patch.object(legacy, "_installed_at", return_value=True):
                data = copy.deepcopy(self.payloads)
                owner = data[index]
                for part in path[:-1]:
                    owner = owner[part]
                owner[path[-1]] = value
                self.assertIsNone(legacy._classify(self.root, self.rows(data), 2))
