"""Small Streamable HTTP client for one user-configured MCP server."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import requests


PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


def validate_mcp_url(url: str) -> str:
    clean = (url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MCP 地址必须是完整的 http:// 或 https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("MCP 地址里不能直接包含账号或密码")
    return clean


class MCPClient:
    def __init__(self, url: str, token: str = "", timeout: float = 15):
        self.url = validate_mcp_url(url)
        self.token = (token or "").strip()
        self.timeout = timeout
        self.session_id = None
        self.protocol_version = PROTOCOL_VERSION
        self.server_info = {}
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self, method: str, name: str | None = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            "Mcp-Method": method,
        }
        if name:
            headers["Mcp-Name"] = name
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        return headers

    @staticmethod
    def _decode_response(response, request_id):
        if not response.content:
            return None
        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return response.json()

        messages = []
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                messages.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        for message in reversed(messages):
            if request_id is None or message.get("id") == request_id:
                return message
        return messages[-1] if messages else None

    def _post(self, method: str, params=None, notification=False):
        request_id = None if notification else self._next_id()
        payload = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params

        name = params.get("name") if isinstance(params, dict) else None
        try:
            response = requests.post(
                self.url,
                headers=self._headers(method, name),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MCPError(f"MCP 连接失败：{exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            detail = (response.text or "").strip().replace("\n", " ")[:240]
            raise MCPError(f"MCP 返回 HTTP {response.status_code}" + (f"：{detail}" if detail else ""))

        session_id = response.headers.get("MCP-Session-Id")
        if session_id:
            self.session_id = session_id
        if notification:
            return None

        data = self._decode_response(response, request_id)
        if not isinstance(data, dict):
            raise MCPError("MCP 没有返回可识别的 JSON-RPC 响应")
        if data.get("error"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPError(f"MCP 错误：{message or '未知错误'}")
        if "result" not in data:
            raise MCPError("MCP 响应缺少 result")
        return data["result"]

    def initialize(self):
        result = self._post("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "Becoming", "version": "1.0"},
        })
        if not isinstance(result, dict):
            raise MCPError("MCP initialize 响应格式不正确")
        self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo") or {}
        self._post("notifications/initialized", notification=True)
        return result
    def list_tools(self) -> list[dict]:
        if not self.server_info:
            self.initialize()
        tools = []
        cursor = None
        for _ in range(10):
            params = {"cursor": cursor} if cursor else {}
            result = self._post("tools/list", params)
            if not isinstance(result, dict):
                raise MCPError("MCP tools/list 响应格式不正确")
            page = result.get("tools") or []
            tools.extend(tool for tool in page if isinstance(tool, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        if not self.server_info:
            self.initialize()
        result = self._post("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        if not isinstance(result, dict):
            raise MCPError("MCP tools/call 响应格式不正确")
        return result
