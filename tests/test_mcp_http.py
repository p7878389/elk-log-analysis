"""Real HTTP transport tests without external ELK credentials or services."""
from http.client import HTTPConnection
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import mcp_http as http


class HttpTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = patch.object(http, "TOKENS")
        tokens = cls.auth.start()
        tokens.lookup.side_effect = lambda token: (
            {"name": "test", "admin": False, "prod": False, "code": False}
            if token == "test-token" else None)
        cls.no_auth = patch.object(http, "NO_AUTH", False)
        cls.no_auth.start()
        cls.server = http.ThreadingHTTPServer(("127.0.0.1", 0), http.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.auth.stop()
        cls.no_auth.stop()

    def request(self, body, headers=None, method="POST", path="/mcp"):
        conn = HTTPConnection(*self.server.server_address, timeout=3)
        try:
            actual_headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
            actual_headers.update(headers or {})
            conn.request(method, path, body, actual_headers)
            response = conn.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload and path != "/healthz" else payload
        finally:
            conn.close()

    def rpc(self, method, params=None):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {} if params is None else params})

    def test_handshake_and_tools(self):
        status, result = self.request(self.rpc("initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(status, 200)
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18")
        status, result = self.request(self.rpc("tools/list"))
        self.assertEqual(status, 200)
        self.assertIn("elk_search", [tool["name"] for tool in result["result"]["tools"]])

    def test_notification(self):
        self.assertEqual(self.request(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})), (202, b""))

    def test_auth_and_origin(self):
        self.assertEqual(self.request(self.rpc("ping"), {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request(self.rpc("ping"), {"Origin": "https://untrusted.invalid"})[0], 403)

    def test_bad_requests_return_rpc_errors(self):
        for value in (None, 1, {}, {"jsonrpc": "2.0", "id": 1, "method": []}):
            with self.subTest(value=value):
                _, result = self.request(json.dumps(value))
                self.assertEqual(result["error"]["code"], -32600)
        for params in ([], "bad", {"arguments": {}}, {"name": "elk_envs", "arguments": []}):
            with self.subTest(params=params):
                _, result = self.request(self.rpc("tools/call", params))
                self.assertEqual(result["error"]["code"], -32602)
        self.assertEqual(self.request("[")[1]["error"]["code"], -32700)
        self.assertEqual(self.request("[]")[0], 400)
        self.assertEqual(self.request(self.rpc("ping"))[1]["result"], {})

    def test_bad_framing(self):
        for value in ("-1", "invalid"):
            self.assertEqual(self.request("", {"Content-Length": value})[0], 400)
        self.assertEqual(self.request("", {"Transfer-Encoding": "chunked"})[0], 400)
        with patch.object(http, "MAX_BODY", 2):
            self.assertEqual(self.request("123")[0], 413)

    def test_permissions(self):
        for name, arguments in (("code_services", {}), ("elk_search", {"confirm_production": True})):
            _, result = self.request(self.rpc("tools/call", {"name": name, "arguments": arguments}))
            self.assertTrue(result["result"]["isError"])

    def test_manual_clone_requires_admin(self):
        _, result = self.request(self.rpc("tools/call", {"name": "code_prepare", "arguments": {
            "git_url": "https://gitlab.example.invalid/group/repo.git", "access_token": "secret"
        }}))
        self.assertEqual(result["result"]["isError"], True)
        self.assertNotIn("secret", json.dumps(result))

    def test_health_and_no_sse(self):
        self.assertEqual(self.request(None, method="GET", path="/healthz"), (200, b"ok"))
        self.assertEqual(self.request(None, method="GET")[0], 405)


class TokenSources(unittest.TestCase):
    """ELK_HTTP_TOKENS（环境变量）与 ELK_HTTP_TOKENS_FILE（文件）两种来源。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / "tokens.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_token_items_formats(self):
        items = http.token_items([
            {"name": "admin", "sha256": http.sha256("a").upper(), "prod": True, "admin": True},
            {"name": "plain", "token": "p"},
            {"name": "off", "sha256": http.sha256("o"), "disabled": True},
            {"sha256": http.sha256("x")},
        ])
        self.assertEqual([t["name"] for t in items], ["admin", "plain"])
        self.assertEqual(items[0]["sha256"], http.sha256("a"))
        self.assertTrue(items[0]["prod"] and items[0]["admin"] and items[0]["code"])
        self.assertEqual(http.token_items({"tokens": [{"name": "b", "token": "b"}]})[0]["name"], "b")

    def test_env_only_when_file_missing(self):
        tokens = http.Tokens(str(self.path), http.token_items([{"name": "env", "token": "e"}]))
        self.assertEqual(tokens.lookup("e")["name"], "env")
        self.assertIsNone(tokens.lookup("other"))

    def test_empty_env_tokens_without_file_rejects_all(self):
        tokens = http.Tokens(str(self.path), [])
        self.assertIsNone(tokens.lookup("e"))

    def test_env_and_file_merged_and_file_reloaded(self):
        self.path.write_text(json.dumps({"tokens": [{"name": "file", "token": "f"}]}), encoding="utf-8")
        tokens = http.Tokens(str(self.path), http.token_items([{"name": "env", "token": "e"}]))
        self.assertEqual(tokens.lookup("e")["name"], "env")
        self.assertEqual(tokens.lookup("f")["name"], "file")
        self.path.write_text(json.dumps({"tokens": [{"name": "file2", "token": "g"}]}), encoding="utf-8")
        os.utime(self.path, (tokens.mtime + 5, tokens.mtime + 5))
        self.assertIsNone(tokens.lookup("f"))
        self.assertEqual(tokens.lookup("g")["name"], "file2")
        self.assertEqual(tokens.lookup("e")["name"], "env")

    def test_file_only_unchanged(self):
        self.path.write_text(json.dumps({"tokens": [{"name": "file", "token": "f"}]}), encoding="utf-8")
        tokens = http.Tokens(str(self.path))
        self.assertEqual(tokens.lookup("f")["name"], "file")


if __name__ == "__main__":
    unittest.main()
