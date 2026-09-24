#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode-mcp smoke-test client (pure standard library).

Usage:
    python3 test_client.py                # MCP handshake + tools/list assertions only (no network requests)
    python3 test_client.py --chat "hello"  # additionally do a real conversation: create_session + chat (requires opencode running)

Exit code: 0 means passed, 1 means failed.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

EXPECTED_TOOLS = {
    "create_session",
    "chat",
    "wait_session",
    "get_messages",
    "permission_reply",
    "form_reply",
    "list_agents",
    "interrupt",
    "pending_interactions",
    "connect_server",
    "list_servers",
    "disconnect_server",
    "list_sessions",
    "compact",
    "get_context",
    "delete_session",
}


class McpClient:
    def __init__(self, server_path=SERVER):
        self.proc = subprocess.Popen(
            [sys.executable, server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 1

    def send(self, message):
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=30.0):
        msg_id = self._next_id
        self._next_id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
        )
        return self.read_response(msg_id, timeout=timeout)

    def notify(self, method, params=None):
        self.send(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}
        )

    def read_response(self, expected_id, timeout=30.0):
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for response (id=%s)" % expected_id)
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError("server process exited without sending a response")
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if message.get("id") == expected_id:
                return message

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def tool_text(response):
    """Extract the text content from a tools/call response."""
    result = response.get("result") or {}
    content = result.get("content") or []
    if content and content[0].get("type") == "text":
        return content[0]["text"]
    return json.dumps(result, ensure_ascii=False)


def handshake(client):
    """initialize → notifications/initialized → tools/list, asserting the tool catalog."""
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "opencode-mcp-test", "version": "1.0.0"},
        },
    )
    result = init.get("result") or {}
    assert result.get("protocolVersion") == "2024-11-05", (
        "protocolVersion mismatch: %r" % result.get("protocolVersion")
    )
    server_info = result.get("serverInfo") or {}
    assert server_info.get("name") == "opencode-mcp", (
        "serverInfo.name is wrong: %r" % server_info.get("name")
    )
    assert "tools" in (result.get("capabilities") or {}), "capabilities.tools is missing"

    client.notify("notifications/initialized")

    listed = client.request("tools/list")
    tools = (listed.get("result") or {}).get("tools") or []
    names = [tool.get("name") for tool in tools]
    print("Tool count: %d" % len(tools))
    for name in names:
        print("  - %s" % name)

    missing = EXPECTED_TOOLS - set(names)
    assert not missing, "Missing tools: %s" % ", ".join(sorted(missing))
    assert len(tools) == len(EXPECTED_TOOLS), (
        "Tool count mismatch: expected %d, got %d" % (len(EXPECTED_TOOLS), len(tools))
    )
    return tools


def chat_roundtrip(client, text):
    """Real conversation test (requires opencode running)."""
    resp = client.request(
        "tools/call",
        {
            "name": "create_session",
            "arguments": {"title": "opencode-mcp smoke test"},
        },
        timeout=60.0,
    )
    session = json.loads(tool_text(resp))
    session_id = session.get("session_id")
    print("Created session: %s" % session_id)
    assert session_id, "create_session did not return a session_id"

    resp = client.request(
        "tools/call",
        {
            "name": "chat",
            "arguments": {
                "session_id": session_id,
                "text": text,
                "timeout_secs": 120,
            },
        },
        timeout=180.0,
    )
    if resp.get("result", {}).get("isError"):
        print("chat returned an error: %s" % tool_text(resp))
        return False
    payload = json.loads(tool_text(resp))
    print("chat status: %s" % payload.get("status"))
    if payload.get("status") == "succeeded":
        print("assistant_text:\n%s" % payload.get("assistant_text"))
        return True
    print("Not succeeded, payload:\n%s" % json.dumps(payload, ensure_ascii=False, indent=2))
    return payload.get("status") in ("succeeded", "needs_permission", "needs_form")


def main():
    parser = argparse.ArgumentParser(description="opencode-mcp smoke test")
    parser.add_argument("--chat", metavar="TEXT", help="additionally run a real conversation test")
    args = parser.parse_args()

    client = McpClient()
    try:
        handshake(client)
        if args.chat:
            ok = chat_roundtrip(client, args.chat)
            if not ok:
                print("Conversation test failed")
                return 1
        print("Smoke test passed ✅")
        return 0
    except Exception as exc:
        print("Smoke test failed ❌: %s" % exc)
        try:
            err = client.proc.stderr.read()
            if err:
                print("--- server stderr ---")
                print(err)
        except Exception:
            pass
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
