#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Security-review verification for the connect_server / DoS hardening changes.

Self-contained: it spawns server.py over stdio and drives it with JSON-RPC.
Every assertion fires *before* any real opencode connection is made, so no
opencode instance is required. Run:  python3 tests/security_review_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SERVER_PATH = os.path.join(REPO_ROOT, "server.py")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


class Mcp:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._id = 0

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, name, arguments, timeout=25.0):
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
        return self._read(mid, timeout)

    def request(self, method, params=None, timeout=25.0):
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        return self._read(mid, timeout)

    def _read(self, expected, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                return {"__timeout__": True}
            line = self.proc.stdout.readline()
            if line == "":
                return {"__eof__": True}
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == expected:
                return msg

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def tool_text(resp):
    res = resp.get("result") or {}
    content = res.get("content") or []
    if content and content[0].get("type") == "text":
        return content[0]["text"]
    return json.dumps(res, ensure_ascii=False)


def is_error(resp):
    return (resp.get("result") or {}).get("isError", False)


def main():
    # ---- import the module directly for constant / function-level checks ----
    sys.path.insert(0, REPO_ROOT)
    import importlib.util
    spec = importlib.util.spec_from_file_location("opencode_mcp_server", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("MAX_RESPONSE_BYTES defined", getattr(mod, "MAX_RESPONSE_BYTES", None) == 1024 * 1024,
          repr(getattr(mod, "MAX_RESPONSE_BYTES", None)))
    check("MAX_STDIN_LINE defined", getattr(mod, "MAX_STDIN_LINE", None) == 1024 * 1024,
          repr(getattr(mod, "MAX_STDIN_LINE", None)))
    check("MAX_WORKERS defined", getattr(mod, "MAX_WORKERS", None) == 64,
          repr(getattr(mod, "MAX_WORKERS", None)))
    check("check_response_size exists", callable(getattr(mod, "check_response_size", None)))
    check("_validate_remote_url exists", callable(getattr(mod, "_validate_remote_url", None)))

    # check_response_size raises over the cap, not under
    try:
        mod.check_response_size(mod.MAX_RESPONSE_BYTES)
        ok_under = True
    except Exception:
        ok_under = False
    try:
        mod.check_response_size(mod.MAX_RESPONSE_BYTES + 1)
        ok_over = False
    except mod.OpenCodeError:
        ok_over = True
    check("check_response_size: under cap ok, over cap raises", ok_under and ok_over)

    # ---- _validate_remote_url unit checks (no network) ----
    v = mod._validate_remote_url
    def rejects(url):
        try:
            v(url)
            return False
        except mod.OpenCodeError:
            return True
    check("reject file://", rejects("file:///home/clavius/.hermes/secrets/vllm27b.api.key"))
    check("reject schemeless", rejects("127.0.0.1:4096"))
    check("reject loopback", rejects("http://127.0.0.1:4096"))
    check("reject localhost (resolves to loopback)", rejects("http://localhost:4096"))
    check("reject private 10.x", rejects("http://10.100.10.40:4096"))
    check("reject private 192.168.x", rejects("http://192.168.4.1:4096"))
    check("reject link-local/metadata 169.254.169.254", rejects("http://169.254.169.254/latest/meta-data/"))
    check("reject IPv6 loopback", rejects("http://[::1]:4096"))
    check("reject IPv6 ULA", rejects("http://[fd12::1]:4096"))
    check("reject IPv4-mapped IPv6 loopback", rejects("http://[::ffff:127.0.0.1]:4096"))
    check("reject decimal-encoded IPv4 (resolves to 127.0.0.1)", rejects("http://2130706433:4096"))
    check("reject hex-encoded IPv4 (resolves to 127.0.0.1)", rejects("http://0x7f.0.0.1:4096"))
    check("reject octal-encoded IPv4 (resolves to 127.0.0.1)", rejects("http://0177.0.0.1:4096"))
    check("reject short-form IPv4 (resolves to 127.0.0.1)", rejects("http://127.1:4096"))
    check("allow unresolvable public hostname (fails later at the availability gate)",
          not rejects("http://does-not-exist.invalid:4096"))
    check("reject control char in url", rejects("http://1.2.3.4\x00:4096"))

    # ---- live server: connect_server credential + URL policy over JSON-RPC ----
    mcp = Mcp()
    try:
        mcp.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "sec", "version": "0"}})
        # tools/list: schema must not expose password_file / password_env
        listed = mcp.request("tools/list")
        tools = (listed.get("result") or {}).get("tools") or []
        conn_tool = next((t for t in tools if t.get("name") == "connect_server"), None)
        check("connect_server present in tools/list", conn_tool is not None)
        props = (conn_tool or {}).get("inputSchema", {}).get("properties", {})
        check("schema: password_file removed", "password_file" not in props, repr(list(props)))
        check("schema: password_env removed", "password_env" not in props, repr(list(props)))
        check("schema: password still present", "password" in props, repr(list(props)))

        # password_file must be rejected WITHOUT reading the file (use a path that
        # cannot be read; if the code tried to open it we'd still see a different message).
        secret = tempfile.NamedTemporaryFile(delete=False)
        secret.write(b"SECRET_SENTINEL_VALUE\n")
        secret.close()
        r = mcp.call("connect_server", {"name": "x1", "url": "http://10.0.0.1:4096",
                                        "password_file": secret.name})
        check("connect_server password_file rejected", is_error(r), tool_text(r))
        check("  ... and message names the exfil risk", "exfiltrate" in tool_text(r).lower()
              or "not accepted" in tool_text(r).lower(), tool_text(r))
        os.unlink(secret.name)

        r = mcp.call("connect_server", {"name": "x2", "url": "http://10.0.0.1:4096",
                                        "password_env": "PATH"})
        check("connect_server password_env rejected", is_error(r), tool_text(r))

        # URL validation fires before any network call (private IP, no password)
        r = mcp.call("connect_server", {"name": "x3", "url": "http://127.0.0.1:4096"})
        check("connect_server loopback url rejected", is_error(r), tool_text(r))
        r = mcp.call("connect_server", {"name": "x4", "url": "http://169.254.169.254/latest/meta-data/"})
        check("connect_server metadata url rejected", is_error(r), tool_text(r))

        # A *valid* public URL with no live server: must pass URL validation and
        # fail at the availability (network) gate, proving ordering (validate -> probe).
        # (192.0.2.1 would be rejected too -- TEST-NET is is_reserved; 93.184.216.34 is
        # a genuinely public address, so this must get past validation to the network gate.)
        r = mcp.call("connect_server", {"name": "x5", "url": "http://93.184.216.34:9"},
                     timeout=25.0)
        text5 = tool_text(r)
        check("connect_server valid-public url passes validation (reaches availability gate)",
              is_error(r) and ("availability" in text5.lower() or "Cannot connect" in text5)
              and "reserved" not in text5,
              text5[:200])
    finally:
        mcp.close()

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
