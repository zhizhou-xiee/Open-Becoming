import json
import unittest
from unittest.mock import patch

from mcp_client import MCPClient, validate_mcp_url


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, text=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {"content-type": "application/json"}
        self.text = text if text is not None else json.dumps(payload or {})
        self.content = self.text.encode()

    def json(self):
        return self._payload


class MCPClientTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertEqual(validate_mcp_url("https://example.com/mcp"), "https://example.com/mcp")
        with self.assertRaises(ValueError):
            validate_mcp_url("file:///tmp/mcp")
        with self.assertRaises(ValueError):
            validate_mcp_url("https://user:pass@example.com/mcp")

    @patch("mcp_client.requests.post")
    def test_initialize_list_and_call_with_bearer_and_session(self, post):
        post.side_effect = [
            FakeResponse({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "Test MCP", "version": "1"},
                    "capabilities": {"tools": {}},
                },
            }, headers={"content-type": "application/json", "MCP-Session-Id": "session-1"}),
            FakeResponse(status=202, text="", headers={}),
            FakeResponse({
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]},
            }),
            FakeResponse({
                "jsonrpc": "2.0",
                "id": 3,
                "result": {"content": [{"type": "text", "text": "hello"}]},
            }),
        ]

        client = MCPClient("https://example.com/mcp", "secret")
        self.assertEqual(client.list_tools()[0]["name"], "echo")
        self.assertEqual(client.call_tool("echo", {"text": "hello"})["content"][0]["text"], "hello")

        self.assertEqual(post.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(post.call_args_list[2].kwargs["headers"]["MCP-Session-Id"], "session-1")
        self.assertEqual(post.call_args_list[3].kwargs["headers"]["Mcp-Name"], "echo")

    @patch("mcp_client.requests.post")
    def test_sse_response_is_supported(self, post):
        post.side_effect = [
            FakeResponse({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "SSE MCP"},
                },
            }),
            FakeResponse(status=202, text="", headers={}),
            FakeResponse(
                text='event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n',
                headers={"content-type": "text/event-stream"},
            ),
        ]
        self.assertEqual(MCPClient("http://example.test/mcp").list_tools(), [])


if __name__ == "__main__":
    unittest.main()
