#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the 2026-09-24 second-round (independent review) findings.

Each test targets a specific finding:
  1. [HIGH]   302-redirect SSRF: urllib's default redirect handler replays the
              Authorization header to the unvalidated Location target.
  2. [HIGH]   uncapped HTTPError body read defeats the 1 MiB DoS cap.
  3. [MED]    fail-open mixed DNS record set (one public + one private passed).
  4. [MED]    unparseable-only record set (e.g. scoped fe80::%eth0) passed.
  5. [MED]    cap-abort misclassified as [availability] in _ensure_version.
  6. [MED]    rebinding window: http_request must re-validate the url per request.
  7. [LOW]    _is_disallowed_ip gaps: CGNAT 100.64/10, 6/8, 7/8, IPv6-unwrap.
  8. [MED]    (final review) spawn env must be minimal, not dict(os.environ).
  9. [LOW]    (final review) non-dict `notifications/cancelled` params must not kill
              the stdin reader (process DoS).

Self-contained: local ThreadingHTTPServer on 127.0.0.1 only; no external network
except a couple of public-DNS lookups that are monkeypatched in the unit checks.
Run:  python3 tests/regression_review_test.py
"""

import importlib.util
import inspect
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SERVER_PATH = os.path.join(REPO_ROOT, "server.py")

PASS = 0
FAIL = 0
TRACKER_PORT = 0  # set in main() before the redirecting server is used


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


def load_module():
    spec = importlib.util.spec_from_file_location("opencode_mcp_server", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Local HTTP servers (127.0.0.1 only)
# ---------------------------------------------------------------------------

class RedirectingHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _respond(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}):
            self.send_header(k, v)
        if body:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/info":
            # The attack: a "public" opencode host that 302-redirects the
            # registration probe (and every later request) to the internal
            # tracking target (standing in for 169.254.169.254).
            self._respond(302, headers=[("Location", "http://127.0.0.1:%d/steal" % TRACKER_PORT)])
        elif self.path == "/api/ok":
            self._respond(200, json.dumps({"ok": True}).encode("utf-8"),
                          headers=[("Content-Type", "application/json")])
        elif self.path == "/api/big404":
            self._respond(404, b"X" * (2 * 1024 * 1024))
        elif self.path == "/api/big404short":
            # 404 with a lying Content-Length (4 MiB declared, 2 KiB sent): must be handled
            # with the error body capped (no DoS), via the HTTPError path.
            self.send_response(404)
            self.send_header("Content-Length", str(4 * 1024 * 1024))
            self.end_headers()
            self.wfile.write(b"x" * 2048)
            self.wfile.flush()
        elif self.path == "/api/lied200":
            # 200 with a lying Content-Length (4 MiB declared, 2 KiB sent): the pre-read
            # declared-size check must reject it BEFORE any body read.
            self.send_response(200)
            self.send_header("Content-Length", str(4 * 1024 * 1024))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"x" * 2048)
            self.wfile.flush()
        else:
            self._respond(404, b"nope")


class TrackingHandler(BaseHTTPRequestHandler):
    """Internal target: records that it was reached (a redirect that landed
    here means the SSRF bypass works)."""

    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        TrackingHandler.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def start_server(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def main():
    mod = load_module()
    v = mod._validate_remote_url
    isdis = mod._is_disallowed_ip

    def rejects(url):
        try:
            v(url)
            return False
        except mod.OpenCodeError:
            return True

    # -- 1. [HIGH] redirect SSRF: opener must never follow 3xx ----------------
    tracker = start_server(TrackingHandler)
    global TRACKER_PORT
    TRACKER_PORT = tracker.server_address[1]
    redir = start_server(RedirectingHandler)
    redir_port = redir.server_address[1]
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d/api/info" % redir_port)
        req.add_header("Authorization", "Basic TESTSECRET")
        followed = False
        try:
            mod._build_opener().open(req, timeout=5.0)
        except urllib.error.HTTPError as exc:
            followed = exc.code == 200
        check("no-redirect: 302 raises HTTPError (not followed)",
              not followed and TrackingHandler.hits == [],
              "hits=%r" % TrackingHandler.hits)

        conn = mod.Connection("t1", "http://127.0.0.1:%d" % redir_port, "pw", is_local=True)
        try:
            mod.http_request(conn, "GET", "/api/info")
            check("no-redirect: http_request fails closed on 302", False, "no exception raised")
        except mod.OpenCodeError as exc:
            check("no-redirect: http_request fails closed on 302",
                  "redirect" in str(exc).lower() or "[availability]" in str(exc),
                  str(exc)[:160])
        check("no-redirect: internal target was never reached (no SSRF)", TrackingHandler.hits == [],
              "hits=%r" % TrackingHandler.hits)

        # -- 2. [HIGH] error body cap ------------------------------------------
        TrackingHandler.hits = []
        r = None
        try:
            mod.http_request(conn, "GET", "/api/big404")
        except mod.OpenCodeError as exc:
            r = str(exc)
        check("error-body: 2 MiB 404 body handled, error is short",
              r is not None and len(r) < 6000 and "HTTP 404" in r, (r or "no exception")[:200])

        try:
            mod.http_request(conn, "GET", "/api/big404short")
            check("error-body: 404 with lying Content-Length handled (no DoS, short error)",
                  False, "no exception raised")
        except mod.OpenCodeError as exc:
            check("error-body: 404 with lying Content-Length handled (no DoS, short error)",
                  len(str(exc)) < 6000 and "HTTP 404" in str(exc), str(exc)[:200])

        # 200 with a lying Content-Length: the pre-read declared-size check must reject it
        # before any body read (this is the DoS path the cap exists for).
        try:
            mod.http_request(conn, "GET", "/api/lied200")
            check("200 lying Content-Length (4 MiB declared) rejected before read",
                  False, "no exception raised")
        except mod.OpenCodeError as exc:
            check("200 lying Content-Length (4 MiB declared) rejected before read",
                  "cap" in str(exc).lower() and "[other]" in str(exc), str(exc)[:200])

        # -- 6. [MED] re-validation at request time ----------------------------
        import inspect
        src = inspect.getsource(mod.http_request)
        check("rebind: http_request re-validates dynamic urls per request",
              "_validate_remote_url(conn.base_url)" in src, src[:120])
        local_src = inspect.getsource(mod.http_request)
        check("rebind: local connections are exempt from per-request validation",
              "if not conn.is_local" in local_src)
        # a local connection must still work end-to-end through the no-redirect opener
        ok = mod.http_request(conn, "GET", "/api/ok")
        check("local connection works through no-redirect opener", ok == {"ok": True}, repr(ok))
    finally:
        redir.shutdown()
        tracker.shutdown()

    # -- 3/4. [MED] fail-closed DNS resolution (monkeypatched getaddrinfo) ----
    def fake_records(addrs):
        def _getaddrinfo(host, port, *a, **k):
            out = []
            for i in addrs:
                family = socket.AF_INET if ":" not in i else socket.AF_INET6
                out.append((family, socket.SOCK_STREAM, 6, "", (i, port or 80)))
            return out
        return _getaddrinfo

    real_getaddrinfo = socket.getaddrinfo
    try:
        socket.getaddrinfo = fake_records(["93.184.216.34", "127.0.0.1"])
        check("mixed records (public + loopback) rejected (fail-closed)",
              rejects("http://mixed.example:4096"))

        socket.getaddrinfo = fake_records(["93.184.216.34", "169.254.169.254"])
        check("mixed records (public + cloud metadata) rejected (fail-closed)",
              rejects("http://mixed2.example:4096"))

        socket.getaddrinfo = fake_records(["fe80::1%eth0"])
        # fe80::1%eth0 parses (Python 3.9+) -> link-local -> rejected by the any-disallowed
        # branch. Call v() directly (rejects() swallows the exception).
        try:
            v("http://scoped6.example:4096")
            scoped_rejected = False
        except mod.OpenCodeError:
            scoped_rejected = True
        check("unparseable/link-local record set rejected (fail-closed)", scoped_rejected)

        # two genuinely public addresses (93.184.216.34, 8.8.8.8; 203.0.113.x is TEST-NET-3
        # and would correctly be rejected as reserved)
        socket.getaddrinfo = fake_records(["93.184.216.34", "8.8.8.8"])
        check("all-public record set still allowed", not rejects("http://pub.example:4096"))

        # records that exist but cannot be parsed as any IP (defensive resolver output):
        # must be rejected by the "records but nothing parseable" fail-closed branch
        def _unparseable(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", port or 80))]
        socket.getaddrinfo = _unparseable
        try:
            v("http://weird.example:4096")
            unparseable_ok = False
        except mod.OpenCodeError:
            unparseable_ok = True
        check("records-but-nothing-parseable rejected (fail-closed)", unparseable_ok)
    finally:
        socket.getaddrinfo = real_getaddrinfo

    # -- 5. [MED] cap abort is not misclassified as availability ---------------
    try:
        mod.check_response_size(mod.MAX_RESPONSE_BYTES + 1)
    except mod.OpenCodeError as exc:
        check("cap-abort surfaces as [other] (not [availability])",
              "[other]" in str(exc) and "availability" not in str(exc), str(exc)[:160])
    # _ensure_version must re-raise OpenCodeError instead of wrapping it
    import inspect as _insp
    ev_src = _insp.getsource(mod._ensure_version)
    check("_ensure_version re-raises OpenCodeError verbatim", "except OpenCodeError" in ev_src)

    # -- 7. [LOW] _is_disallowed_ip coverage ------------------------------------
    ip_cases = {
        "100.64.0.1": True, "100.127.255.254": True, "100.128.0.1": False,
        "6.0.0.1": True, "7.255.255.255": True, "8.8.8.8": False,
        "127.0.0.1": True, "169.254.169.254": True, "10.1.2.3": True,
        "192.168.1.1": True, "93.184.216.34": False,
        "::1": True, "fe80::1": True, "fd12::1": True, "::": True,
        "2001:4860:4860::8888": False,
        "::ffff:127.0.0.1": True, "::ffff:100.64.9.9": True, "::ffff:8.8.8.8": False,
        "::127.0.0.1": True, "::100.64.1.1": True, "::8.8.8.8": False,
    }
    bad = []
    for s, want in ip_cases.items():
        try:
            got = isdis(ipaddress.ip_address(s))
        except ValueError:
            continue  # form this CPython build does not parse
        if got != want:
            bad.append("%s got=%s want=%s" % (s, got, want))
    check("_is_disallowed_ip full table (incl. CGNAT/6-8/IPv6-unwrap)", not bad, "; ".join(bad))

    # -- 8. [MED, final review] spawn env must be minimal ----------------------
    spawn_src = inspect.getsource(mod._spawn_local_serve)
    check("spawn env: no full-environment inheritance (no dict(os.environ) in spawn)",
          "dict(os.environ)" not in spawn_src and "os.environ)" not in spawn_src.split("env = {")[0][-400:] if "env = {" in spawn_src else "dict(os.environ)" not in spawn_src)
    check("spawn env: OPENCODE_SERVER_PASSWORD is injected",
          '"OPENCODE_SERVER_PASSWORD": password' in spawn_src)
    check("spawn env: only PATH/HOME/password keys are set",
          all(k in spawn_src for k in ('"PATH"', '"HOME"', '"OPENCODE_SERVER_PASSWORD"'))
          and spawn_src.count("os.environ.get") == 2)

    # -- 9. [LOW, final review] hostile `notifications/cancelled` cannot kill
    # the stdin reader (non-dict / missing params used to raise AttributeError,
    # killing the whole MCP process = DoS) -------------------------------------
    p = subprocess.Popen([sys.executable, SERVER_PATH], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", bufsize=1)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                              "params": [1, 2, 3]}) + "\n")
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                              "params": "not-a-dict"}) + "\n")
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                              "params": {}}) + "\n")
    p.stdin.flush()
    tools_seen = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("id") == 1 and isinstance(m.get("result"), dict) \
                    and "tools" in m["result"]:
                tools_seen = True
                break
    except Exception:
        pass
    alive = p.poll() is None
    try:
        p.stdin.close()
    except Exception:
        pass
    rc = p.wait(timeout=5) if alive else p.returncode
    try:
        p.stdout.close()
    except Exception:
        pass
    check("hostile cancelled (non-dict params) does not kill the process", alive or rc in (0,),
          "rc=%r" % (rc,))
    check("MCP still answers tools/list after hostile cancelled", tools_seen)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
