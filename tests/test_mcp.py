"""MCP client and OAuth sign-in against a local fake server.

Run: python -m unittest discover tests
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))
sys.path.insert(0, os.path.dirname(__file__))

from genfill import config  # noqa: E402
from genfill.backends import base, mcp, oauth  # noqa: E402
from fake_servers import FakeMcpServer, text_result  # noqa: E402


class ConfigSandbox(unittest.TestCase):
    """Points config.json at a temp folder."""

    def setUp(self):
        self.cfg_dir = tempfile.mkdtemp()
        self._patches = [mock.patch.object(config, "CONFIG_DIR", self.cfg_dir),
                         mock.patch.object(config, "CONFIG_PATH",
                                           os.path.join(self.cfg_dir, "config.json"))]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.cfg_dir, ignore_errors=True)


def _free_ports(n=2):
    import socket
    ports = []
    socks = []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return tuple(ports)


class HelperTests(unittest.TestCase):
    def test_www_authenticate(self):
        v = 'Bearer realm="mcp", resource_metadata="https://x/.well-known/a", scope="a b"'
        self.assertEqual(oauth.parse_www_authenticate(v),
                         {"realm": "mcp", "resource_metadata": "https://x/.well-known/a",
                          "scope": "a b"})
        self.assertEqual(oauth.parse_www_authenticate('Bearer error=invalid_token'),
                         {"error": "invalid_token"})
        self.assertEqual(oauth.parse_www_authenticate(None), {})

    def test_pkce(self):
        import base64
        import hashlib
        v, c = oauth.pkce_pair()
        self.assertGreaterEqual(len(v), 43)
        self.assertEqual(c, base64.urlsafe_b64encode(
            hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode())

    def test_sse_parsing(self):
        lines = [b"event: message\n", b'data: {"a":\n', b"data: 1}\n", b"\n",
                 b": comment\n", b"\n", b'data: {"b": 2}\r\n']
        self.assertEqual(list(mcp.parse_sse(lines)), [{"a": 1}, {"b": 2}])

    def test_tool_result(self):
        self.assertEqual(mcp.tool_result({"structuredContent": {"x": 1},
                                          "content": [{"type": "text", "text": "{}"}]}),
                         {"x": 1})
        self.assertEqual(mcp.tool_result(text_result({"y": 2})), {"y": 2})
        self.assertEqual(mcp.tool_result({"content": [{"type": "text", "text": "hi"}]}), "hi")
        with self.assertRaises(base.BackendError) as cm:
            mcp.tool_result({"isError": True,
                             "content": [{"type": "text", "text": "no credits"}]}, "gen")
        self.assertIn("no credits", str(cm.exception))

    def test_sniff_image(self):
        self.assertEqual(base.sniff_image(b"\x89PNG\r\n\x1a\nxx"), ".png")
        self.assertEqual(base.sniff_image(b"\xff\xd8\xff\xe0"), ".jpg")
        self.assertEqual(base.sniff_image(b"RIFF\0\0\0\0WEBPVP8"), ".webp")
        self.assertIsNone(base.sniff_image(b"<html>"))
        with self.assertRaises(base.BackendError):
            base.check_image(b"RIFF\0\0\0\0WEBPVP8", "X")


class McpTransportTests(ConfigSandbox):
    def _server(self, **kw):
        tools = {"echo": lambda a: text_result({"echo": a})}
        srv = FakeMcpServer(tools=tools, **kw)
        self.addCleanup(srv.close)
        return srv

    def test_api_key_json(self):
        srv = self._server(api_key="k1")
        c = mcp.McpClient(srv.url, mcp.ApiKeyAuth("X-API-Key", "k1"))
        self.assertEqual(c.call_tool("echo", {"v": 1}), {"echo": {"v": 1}})
        self.assertEqual(c.session_id, "s1")
        self.assertEqual(c.server_info["name"], "fake")
        self.assertEqual([t["name"] for t in c.list_tools()], ["echo"])

    def test_sse_responses(self):
        srv = self._server(api_key="k1", sse=True)
        c = mcp.McpClient(srv.url, mcp.ApiKeyAuth("X-API-Key", "k1"))
        self.assertEqual(c.call_tool("echo", {"v": 2}), {"echo": {"v": 2}})

    def test_bad_key(self):
        srv = self._server(api_key="k1")
        c = mcp.McpClient(srv.url, mcp.ApiKeyAuth("X-API-Key", "nope", "Comfy"))
        with self.assertRaises(base.BackendError) as cm:
            c.call_tool("echo")
        self.assertIn("rejected the API key", str(cm.exception))

    def test_unknown_tool_is_backend_error(self):
        srv = self._server(api_key="k1")
        c = mcp.McpClient(srv.url, mcp.ApiKeyAuth("X-API-Key", "k1"))
        with self.assertRaises(base.BackendError):
            c.call_tool("missing")

    def test_expired_session_reconnects(self):
        srv = self._server(api_key="k1")
        c = mcp.McpClient(srv.url, mcp.ApiKeyAuth("X-API-Key", "k1"))
        c.call_tool("echo")
        srv.sessions.clear()
        self.assertEqual(c.call_tool("echo", {"v": 3}), {"echo": {"v": 3}})
        self.assertEqual(c.session_id, "s1")  # a new session (the set was cleared)

    def test_cancelled(self):
        srv = self._server(api_key="k1")
        c = mcp.McpClient(srv.url, mcp.ApiKeyAuth("X-API-Key", "k1"), cancelled=lambda: True)
        with self.assertRaises(base.Cancelled):
            c.call_tool("echo")


class OAuthTests(ConfigSandbox):
    def setUp(self):
        super(OAuthTests, self).setUp()
        self.srv = FakeMcpServer(tools={"echo": lambda a: text_result({"ok": True})})
        self.addCleanup(self.srv.close)
        self.ports = _free_ports()
        self.provider = oauth.OAuthProvider("fake", self.srv.url, label="Fake",
                                            open_browser=self.srv.browser, ports=self.ports)

    def _client(self, interactive=True):
        return mcp.McpClient(self.srv.url, mcp.OAuthAuth(self.provider), interactive=interactive)

    def test_not_signed_in_without_interaction(self):
        with self.assertRaises(oauth.LoginRequired):
            self._client(interactive=False).call_tool("echo")

    def test_full_sign_in_then_reuse(self):
        self.assertFalse(self.provider.signed_in())
        self.assertEqual(self._client().call_tool("echo"), {"ok": True})
        self.assertTrue(self.provider.signed_in())
        self.assertEqual(self.srv.registrations, 1)
        client = list(self.srv.clients.values())[0]
        self.assertEqual(client["token_endpoint_auth_method"], "none")
        self.assertEqual(client["redirect_uris"], ["http://127.0.0.1:%d/callback" % self.ports[0]])
        st = config.get_oauth("fake")
        self.assertEqual(st["scope"], "mcp offline_access")
        self.assertEqual(st["resource"], self.srv.url)
        # A second client reuses the stored token: no browser, no new registration.
        self.provider.open_browser = lambda url: self.fail("browser opened again")
        self.assertEqual(self._client(interactive=False).call_tool("echo"), {"ok": True})
        self.assertEqual(len(self.srv.token_requests), 1)

    def test_refresh_when_expired(self):
        self._client().call_tool("echo")
        st = config.get_oauth("fake")
        st["tokens"]["expires_at"] = time.time() - 10
        config.set_oauth("fake", st)
        self.provider.open_browser = lambda url: self.fail("browser opened for a refresh")
        self.assertEqual(self._client(interactive=False).call_tool("echo"), {"ok": True})
        self.assertEqual(self.srv.token_requests[-1]["grant_type"], "refresh_token")

    def test_server_revokes_access_token(self):
        self._client().call_tool("echo")
        self.srv.access_tokens.clear()   # server-side revoke; refresh token still valid
        self.provider.open_browser = lambda url: self.fail("browser opened")
        self.assertEqual(self._client(interactive=False).call_tool("echo"), {"ok": True})

    def test_rejected_refresh_requires_sign_in(self):
        self._client().call_tool("echo")
        self.srv.access_tokens.clear()
        self.srv.refresh_tokens.clear()
        with self.assertRaises(oauth.LoginRequired):
            self._client(interactive=False).call_tool("echo")
        self.assertFalse(self.provider.signed_in())

    def test_port_busy_registers_new_redirect(self):
        self._client().call_tool("echo")
        self.provider.sign_out()
        import socket
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", self.ports[0]))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        self.assertEqual(self._client().call_tool("echo"), {"ok": True})
        self.assertEqual(self.srv.registrations, 2)

    def test_sign_in_timeout_and_cancel(self):
        self.provider.open_browser = lambda url: True   # the artist never finishes
        with mock.patch.object(oauth, "LOGIN_TIMEOUT", 1):
            with self.assertRaises(base.BackendError) as cm:
                self.provider.login()
            self.assertIn("timed out", str(cm.exception))
        with self.assertRaises(base.Cancelled):
            self.provider.login(cancelled=lambda: True)

    def test_sign_out(self):
        self.provider.login()
        self.assertTrue(self.provider.signed_in())
        self.provider.sign_out()
        self.assertFalse(self.provider.signed_in())
        self.assertEqual(config.get_oauth("fake"), {})


if __name__ == "__main__":
    unittest.main()
