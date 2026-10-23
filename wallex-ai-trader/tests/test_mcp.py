"""تست کلاینت MCP با سرور جعلی: initialize، SSE، tools/list، tools/call و session id."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wallexbot.mcp import MCPError, WallexMCP

TOOLS = [{"name": "market_snapshot", "description": "snapshot", "inputSchema": {"type": "object"}}]


class FakeMCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_session = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, ctype="application/json", headers=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if "Mcp-Session-Id" in self.headers:
            FakeMCP.seen_session.append(self.headers["Mcp-Session-Id"])
        method = req.get("method")
        if method == "initialize":
            return self._send(200, {"jsonrpc": "2.0", "id": req["id"],
                                    "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}},
                              headers={"Mcp-Session-Id": "sess-1"})
        if method == "notifications/initialized":
            return self._send(202)
        if method == "tools/list":
            payload = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"tools": TOOLS}})
            body = ("event: message\ndata: " + payload + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if method == "tools/call":
            if req["params"].get("name") != "market_snapshot":
                return self._send(200, {"jsonrpc": "2.0", "id": req["id"],
                                        "error": {"code": -32602, "message": "unknown tool"}})
            text = json.dumps({"symbol": req["params"]["arguments"].get("symbol"), "price": 123})
            return self._send(200, {"jsonrpc": "2.0", "id": req["id"],
                                    "result": {"content": [{"type": "text", "text": text}], "isError": False}})
        return self._send(200, {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": "nope"}})


class MCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCP)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_initialize_lists_tools_and_calls(self):
        c = WallexMCP(self.url, timeout=3)
        tools = c.list_tools()
        self.assertEqual(tools[0]["name"], "market_snapshot")
        self.assertEqual(c.session_id, "sess-1")
        res = c.call_tool("market_snapshot", {"symbol": "BTCTMN"})
        self.assertEqual(WallexMCP.text_of(res)["price"], 123)
        self.assertIn("sess-1", FakeMCP.seen_session)

    def test_probe_and_snapshot(self):
        c = WallexMCP(self.url, timeout=3)
        info = c.probe_market_tools()
        self.assertIn("market_snapshot", info["market_tools"])
        snap = c.snapshot("BTCTMN")
        self.assertEqual(snap["symbol"], "BTCTMN")

    def test_rpc_error_raises(self):
        c = WallexMCP(self.url, timeout=3)
        with self.assertRaises(MCPError):
            c.call_tool("does_not_exist", {})


if __name__ == "__main__":
    unittest.main()
