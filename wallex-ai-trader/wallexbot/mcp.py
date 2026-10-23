"""کلاینت MCP والکس (JSON-RPC 2.0 روی HTTP/SSE).

طبق مشخصات MCP: initialize → notifications/initialized → tools/list → tools/call.
پاسخ سرور می‌تواند JSON ساده یا استریم SSE (خطوط `data: ...`) باشد؛ هر دو پشتیبانی می‌شود.
این ماژول اختیاری است: خطای MCP هرگز مسیر معامله را متوقف نمی‌کند (fail-open برای داده، fail-closed برای AI ورود).
"""
from __future__ import annotations
import json
import logging
import time

import requests

log = logging.getLogger("wtb")
PROTOCOL = "2025-06-18"


class MCPError(Exception):
    pass


def _parse_body(resp):
    ctype = (resp.headers.get("content-type") or "").lower()
    text = resp.text or ""
    if "text/event-stream" in ctype or text.lstrip().startswith("event:") or text.lstrip().startswith("data:"):
        objs = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    objs.append(json.loads(chunk))
                except ValueError:
                    continue
        if not objs:
            raise MCPError("SSE response without JSON data")
        return objs[-1]
    try:
        return resp.json()
    except ValueError as e:
        raise MCPError(f"non-JSON response: {text[:120]!r}") from e


class WallexMCP:
    def __init__(self, url="https://api.wallex.ir/mcp", timeout=12, session=None, client_name="wallex-ai-trader"):
        self.url, self.timeout = url, timeout
        self.s = session or requests.Session()
        self.session_id = None
        self._id = 0
        self.client_name = client_name
        self.server_info = {}
        self.initialized = False

    def _post(self, payload, notify=False):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            r = self.s.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise MCPError(f"network: {e}") from e
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
        if notify:
            return None
        body = _parse_body(r)
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            raise MCPError(f"rpc error {err.get('code')}: {err.get('message')}")
        if isinstance(body, dict) and "id" in body:
            return body.get("result")
        # برخی سرورها نتیجه را بدون envelope برمی‌گردانند
        return body

    def _next_id(self):
        self._id += 1
        return self._id

    def initialize(self):
        res = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                          "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": self.client_name, "version": "1.0"}}})
        self.server_info = (res or {}).get("serverInfo", {}) if isinstance(res, dict) else {}
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, notify=True)
        except MCPError:
            pass
        self.initialized = True
        return res

    def list_tools(self):
        if not self.initialized:
            self.initialize()
        res = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}})
        tools = (res or {}).get("tools", []) if isinstance(res, dict) else []
        if not isinstance(tools, list):
            raise MCPError("tools/list: tools not a list")
        return tools

    def call_tool(self, name, arguments=None, timeout=None):
        if not self.initialized:
            self.initialize()
        old = self.timeout
        if timeout:
            self.timeout = timeout
        try:
            return self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
                               "params": {"name": name, "arguments": arguments or {}}})
        finally:
            self.timeout = old

    @staticmethod
    def text_of(result):
        """محتوای متنی نتیجهٔ tools/call؛ اگر JSON بود، پارس می‌کند."""
        if not isinstance(result, dict):
            return result
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        try:
            return json.loads(joined)
        except ValueError:
            return joined

    def probe_market_tools(self):
        """فهرست ابزارها + تلاش برای خواندن اسنپ‌شات بازار (برای پنل کنترل)."""
        tools = self.list_tools()
        names = [t.get("name") for t in tools if isinstance(t, dict)]
        price_tools = [n for n in names if n and any(k in n.lower() for k in ("market", "ticker", "price", "orderbook", "depth"))]
        return {"server": self.server_info, "tools": tools, "market_tools": price_tools}

    def snapshot(self, symbol, cache_ttl=30):
        """تلاش می‌کند از MCP یک تصویر بازار بگیرد؛ در صورت نبود ابزار مناسب None."""
        now = time.time()
        cache = getattr(self, "_snap_cache", {})
        hit = cache.get(symbol)
        if hit and now - hit[0] < cache_ttl:
            return hit[1]
        try:
            for name in self.probe_market_tools()["market_tools"]:
                for args in ({"symbol": symbol}, {"market": symbol}, {"pair": symbol}):
                    try:
                        res = self.call_tool(name, args)
                        data = self.text_of(res)
                        if data:
                            self._snap_cache = {**cache, symbol: (now, data)}
                            return data
                    except MCPError:
                        continue
        except MCPError as e:
            log.info("MCP snapshot unavailable: %s", e)
        return None


def main_list(url, timeout=12):
    c = WallexMCP(url, timeout=timeout)
    info = c.probe_market_tools()
    print(json.dumps(info, indent=2, ensure_ascii=False, default=str))
    return 0


def main_call(url, name, args, timeout=20):
    c = WallexMCP(url, timeout=timeout)
    res = c.call_tool(name, args)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    return 0
