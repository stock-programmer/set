"""Offline Windows proxy routing through the actual single-attempt SDK wire."""
import json
import os
import ssl
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import httpx
except ImportError:
    httpx = None

from pianchang.sdk_transport import _Wire, _upstream_transport
from trace_system import TraceError, Tracer, event, use_context


@unittest.skipUnless(httpx is not None, "requires the optional models dependencies")
class SDKProxyTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.tracer = Tracer(directory)
        self.enterContext(self.tracer.storage_session())
        self.enterContext(use_context(None, tracer=self.tracer))
        self.root = self.enterContext(self.tracer.trace("offline.sdk_proxy"))
        self.created = []
        self.requests = []

    def transport_factory(self, respond=None):
        def make(**options):
            self.assertIsInstance(options["verify"], ssl.SSLContext)
            self.assertTrue(options["verify"].check_hostname)
            self.assertEqual(options["verify"].verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(options["retries"], 0)
            self.assertFalse(options["trust_env"])

            def send(req):
                self.requests.append((options.get("proxy"), req))
                return respond(req) if respond else httpx.Response(200, json={"ok": True})

            transport = httpx.MockTransport(send)
            transport.close = Mock(wraps=transport.close)
            self.created.append((options, transport))
            return transport
        return make

    def windows(self, proxies, *, bypass=False, respond=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("pianchang.sdk_transport.sys.platform", "win32"))
        # Keep a real validating SSLContext, without requiring Windows certificate
        # APIs when this offline test runs on another operating system.
        stack.enter_context(patch.object(ssl.SSLContext, "load_default_certs"))
        stack.enter_context(patch("pianchang.sdk_transport.urllib_request.getproxies_registry",
                                  return_value=proxies, create=True))
        stack.enter_context(patch("pianchang.sdk_transport.urllib_request.proxy_bypass_registry",
                                  **({"side_effect": bypass} if callable(bypass) else {"return_value": bypass}),
                                  create=True))
        stack.enter_context(patch("httpx.HTTPTransport", side_effect=self.transport_factory(respond)))
        return stack

    def wire(self, operation="completion", task_id=None):
        return _Wire(SimpleNamespace(base_url="https://model.invalid/v1"), operation, 5, None, task_id)

    def test_windows_https_proxy_is_used_with_durable_evidence_and_no_credential_leak(self):
        proxy = "http://user:offline%2Dproxy%2Dpassword@127.0.0.1:7897"

        def respond(req):
            names = [row["name"] for row in self.tracer.get_events(self.root.trace_id)]
            self.assertIn("http.request", names)
            self.assertIn("http.proxy.selected", names)
            # Simulate a provider/library error string containing decoded proxy credentials.
            event("offline.proxy_diagnostic", {"role": "user", "detail": proxy + " offline-proxy-password"})
            return httpx.Response(200, json={"ok": True})

        with self.windows({"http": "http://wrong.invalid:8000", "https": proxy}, respond=respond), \
             patch.dict(os.environ, {"HTTPS_PROXY": "http://unrelated.invalid:9999",
                                     "SSL_CERT_FILE": "must-not-be-loaded.pem"}):
            with self.wire() as wire:
                req = httpx.Request("POST", "https://model.invalid/v1/chat/completions", json={"messages": []})
                self.assertEqual(wire.handle_request(req).status_code, 200)
                with self.assertRaisesRegex(ValueError, "单次提交"):
                    wire.handle_request(req)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.created[0][0]["proxy"], proxy)
        self.created[0][1].close.assert_called_once()
        saved = json.dumps(self.tracer.export_trace(self.root.trace_id))
        for secret in (proxy, "offline-proxy-password", "offline%2Dproxy%2Dpassword"):
            self.assertNotIn(secret, saved)
        diagnostic = next(row["data"] for row in self.tracer.get_events(self.root.trace_id)
                          if row["name"] == "offline.proxy_diagnostic")
        self.assertEqual(diagnostic["role"], "user", "A common proxy username is not a global secret")

    def test_disabled_proxy_and_windows_exclusions_use_direct_transport(self):
        for proxies, bypass in (({}, False), ({"https": "http://127.0.0.1:7897"}, True)):
            with self.subTest(proxies_enabled=bool(proxies)), self.windows(proxies, bypass=bypass):
                with self.wire() as wire:
                    wire.handle_request(httpx.Request("POST", "https://model.invalid/v1/chat/completions", json={}))
                self.assertIsNone(self.created[-1][0]["proxy"])
                self.created[-1][1].close.assert_called_once()

    def test_video_download_reselects_proxy_for_its_host_and_closes_every_transport(self):
        def respond(req):
            if req.url.host == "model.invalid":
                return httpx.Response(200, json={"id": "offline-task", "content": {
                    "video_url": "https://media.invalid/clip.mp4"}})
            self.assertNotIn("authorization", req.headers)
            self.assertNotIn("proxy-authorization", req.headers)
            return httpx.Response(200, content=b"offline video")

        with self.windows({"https": "http://127.0.0.1:7897"},
                          bypass=lambda host: host == "media.invalid", respond=respond):
            wire = self.wire("video_content", "offline-task")
            wire.handle_request(httpx.Request("GET", "https://model.invalid/v1/videos/offline-task"))
            response = wire.handle_request(httpx.Request("GET", "https://media.invalid/clip.mp4",
                headers={"authorization": "Bearer offline-provider-key", "proxy-authorization": "offline-auth"}))
            self.assertEqual(response.content, b"offline video")
            self.assertEqual([proxy for proxy, _ in self.requests], ["http://127.0.0.1:7897", None])
            # A close error must not leave another proxy/direct pool open.
            self.created[-1][1].close.side_effect = RuntimeError("offline close failure")
            with self.assertRaisesRegex(RuntimeError, "offline close failure"):
                wire.close()
            for _, transport in self.created:
                transport.close.assert_called_once()
            wire.close()

    def test_proxy_trace_failure_prevents_transport_creation_and_network_send(self):
        real_event = event

        def record(name, *args, **kwargs):
            if name == "http.proxy.selected":
                raise TraceError("offline trace failure")
            return real_event(name, *args, **kwargs)

        with self.windows({"https": "http://127.0.0.1:7897"}), \
             patch("pianchang.sdk_transport.event", side_effect=record):
            with self.wire() as wire:
                req = httpx.Request("POST", "https://model.invalid/v1/chat/completions", json={})
                for _ in range(2):
                    with self.assertRaisesRegex(TraceError, "offline trace failure"):
                        wire.handle_request(req)
        self.assertEqual(self.created, [])
        self.assertEqual(self.requests, [])

    def test_failed_proxy_connection_is_not_retried_or_fallen_back_to_direct(self):
        def timeout(req):
            raise httpx.ConnectTimeout("offline proxy unavailable", request=req)

        with self.windows({"https": "http://127.0.0.1:7897"}, respond=timeout):
            with self.wire() as wire:
                req = httpx.Request("POST", "https://model.invalid/v1/chat/completions", json={})
                for _ in range(2):
                    with self.assertRaises(httpx.ConnectTimeout):
                        wire.handle_request(req)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([options["proxy"] for options, _ in self.created], ["http://127.0.0.1:7897"])
        proof = [row["data"] for row in self.tracer.get_events(self.root.trace_id)
                 if row["name"] == "http.error"][-1]
        self.assertEqual((proof["phase"], proof["dispatch"]), ("unknown", "unknown"))

    def test_proxy_connect_is_distinguished_from_origin_post_on_real_httpcore(self):
        import httpcore

        for fail_tls in (True, False):
            with self.subTest(fail_tls=fail_tls):
                writes = []
                stream = httpcore.MockStream([b"HTTP/1.1 200 Connection established\r\n\r\n"])
                stream.write = lambda data, **kwargs: writes.append(data)
                if fail_tls:
                    stream.start_tls = Mock(side_effect=httpcore.ConnectError("offline TLS handshake"))
                transport = httpx.HTTPTransport(proxy="http://proxy.invalid", trust_env=False, retries=0)
                transport._pool._network_backend = Mock(connect_tcp=Mock(return_value=stream))
                with patch("pianchang.sdk_transport._upstream_transport", return_value=transport), self.wire() as wire:
                    req = httpx.Request("POST", "https://model.invalid/v1/chat/completions", json={})
                    with self.assertRaises(httpx.TransportError) as raised:
                        wire.handle_request(req)
                proof = raised.exception.wire_receipt
                self.assertTrue(b"".join(writes).startswith(b"CONNECT "))
                self.assertEqual(b"POST /v1" in b"".join(writes), not fail_tls)
                self.assertEqual(proof["dispatch"], "not_sent" if fail_tls else "unknown")
                self.assertEqual(proof["phase"], "start_tls" if fail_tls else "receive_response_headers")
                self.assertEqual(getattr(raised.exception, "network_dispatched", None), False if fail_tls else None)

    def test_non_windows_retains_direct_transport_without_environment_overrides(self):
        with patch("pianchang.sdk_transport.sys.platform", "linux"), \
             patch.object(ssl.SSLContext, "load_default_certs"), \
             patch.dict(os.environ, {"HTTPS_PROXY": "http://unrelated.invalid:9999"}), \
             patch("pianchang.sdk_transport.urllib_request.getproxies_registry", create=True) as registry, \
             patch("httpx.HTTPTransport", side_effect=self.transport_factory()):
            transport = _upstream_transport()
            transport.handle_request(httpx.Request("GET", "https://model.invalid/v1/models"))
            transport.close()
            registry.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.created[0][0].get("proxy"))


if __name__ == "__main__":
    unittest.main()
