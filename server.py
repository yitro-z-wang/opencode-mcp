#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode-mcp — MCP (Model Context Protocol) stdio server implemented with the pure Python standard library.

Drives the conversation capabilities of a local opencode (Session / Prompt / permission / form / interrupt).
Zero third-party dependencies; Python 3 standard library only.

Transport: MCP over stdio, one JSON-RPC 2.0 message per line (newline-delimited, not LSP Content-Length framing).
Logs go to stderr; protocol messages go to stdout.
"""

import atexit
import base64
import ipaddress
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_NAME = "opencode-mcp"
SERVER_VERSION = "1.0.0"

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_PASSWORD = "opencode"

HTTP_TIMEOUT = 30.0
POLL_INTERVAL = 1.0

# DoS bounds (a hostile endpoint or client must not be able to grow this process
# without bound): every HTTP response read and every single stdin JSON-RPC line is
# capped, and the worker count is clamped.
MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MiB
MAX_STDIN_LINE = 1024 * 1024  # 1 MiB
MAX_WORKERS = 64


def check_response_size(size):
    """Raise OpenCodeError for a response body beyond MAX_RESPONSE_BYTES (prevents unbounded
    memory growth when a hostile endpoint streams a huge body)."""
    if size > MAX_RESPONSE_BYTES:
        raise OpenCodeError(
            "[other] Response exceeds the %d-byte cap (declared %d bytes); the response was "
            "rejected to protect this process (declared sizes are enforced before any read, "
            "so a server that declares a large size but sends a small body is rejected too)"
            % (MAX_RESPONSE_BYTES, size),
            kind="other",
        )

VALID_AUTO_PERMISSION = ("once", "always", "reject", "manual")

# --- Subtree (subagent) resolution -----------------------------------------
# Structure only: the subtree is the reverse closure of parentID via ?parentID= (never any
# message-content heuristic). Depth/node caps keep one runaway tree from costing unbounded work.
SUBTREE_MAX_DEPTH = 3
SUBTREE_MAX_NODES = 64
SUBTREE_AUTOREPLY = ("once", "always", "reject")

# Per-connection capability keys: marked unsupported (once, permanently for that connection) when
# the endpoint genuinely does not exist (404 / reworked API), so we fall back instead of hanging.
CAP_SESSION_ACTIVE = "session_active"
CAP_GLOBAL_PERMISSION = "global_permission"
CAP_GLOBAL_FORM = "global_form"
CAP_SESSION_PARENTID = "session_parentid"

SUBTREE_UNVERIFIED_NOTE = (
    "Subagent state could not be verified (fail-closed); the reported status is this session's own "
    "outcome only and may be stale with respect to child sessions."
)
SUBTREE_UNSUPPORTED_NOTE = (
    "This opencode server does not support subagent verification (/api/session/active or ?parentID=); "
    "the reported status is this session's own outcome only and may be stale with respect to child sessions."
)


def log(*parts):
    """Write logs to stderr to avoid polluting the stdout protocol stream."""
    try:
        sys.stderr.write(" ".join(str(p) for p in parts) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class OpenCodeError(Exception):
    """Interaction with opencode failed. kind ∈ {availability, compatibility, other}.

    other must carry the raw error so it can be reported to the developer verbatim.
    """

    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Connection layer: multiple opencode servers (MCP-spawned local serve + dynamic remotes)
# Local: explicit direct connection via OPENCODE_URL (skips the spawn); otherwise the MCP spawns a dedicated serve
# (random high port + random password; the child process lives as long as this MCP instance).
# No inferential service discovery of any kind (including service.json).
# ---------------------------------------------------------------------------

DEVELOPMENT_BASELINE_VERSION = (
    os.environ.get("OPENCODE_MCP_BASELINE_VERSION") or "2.0.12"
)
DEFAULT_LOCAL_NAME = "local"
SESSION_ROUTE_LIMIT = 1000

_CONNECTIONS = {}  # name -> Connection (guarded by _STATE_LOCK)
_SESSION_ROUTE = {}  # session_id -> connection name (insertion order; oldest evicted past the limit)
_LOCAL_LOCK = threading.Lock()
# Serial registration lock: eliminates the check-then-write race for concurrent connects with the same name (registration is infrequent)
_REGISTER_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Spawned-child lifecycle
#
# The spawned `opencode serve` is a child process of this MCP, so this MCP owns its
# lifetime: every code path that ends the process must end the child too, otherwise the
# child is re-parented to init and serves a random port forever — one leaked serve per
# MCP restart. Coverage:
#   - stdin EOF (the normal MCP shutdown) and any other normal exit  -> atexit
#   - SIGTERM / SIGHUP / SIGINT (host-managed kill)                  -> signal handlers
#   - SIGKILL (uncatchable on any platform)                          -> a stale serve may survive;
#     the next MCP start does not adopt it (no inferential discovery, by design).
# ---------------------------------------------------------------------------

_SPAWNED_PROCS = []  # every opencode serve this process started (guarded by _CLEANUP_LOCK)
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_DONE = False


def _kill_proc(proc):
    """Terminate a child and reap it (no zombie). Never raises."""
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)  # Reap the child process to avoid a zombie
    except Exception:
        pass


def _track_spawned_proc(proc):
    with _CLEANUP_LOCK:
        _SPAWNED_PROCS.append(proc)


def _cleanup_spawned_procs():
    """Kill every serve this process spawned; idempotent and safe from any exit path."""
    global _CLEANUP_DONE
    with _CLEANUP_LOCK:
        if _CLEANUP_DONE:
            return
        _CLEANUP_DONE = True
        procs = list(_SPAWNED_PROCS)
        _SPAWNED_PROCS.clear()
    for proc in procs:
        _kill_proc(proc)


def _install_signal_handlers():
    """Kill spawned children before dying from a catchable signal, then exit with the signal's default semantics."""
    def _handler(signum, _frame):
        _cleanup_spawned_procs()
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            os._exit(128 + signum)

    candidates = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGHUP"):
        candidates.append(signal.SIGHUP)
    for sig in candidates:
        try:
            signal.signal(sig, _handler)
        except Exception:
            pass


atexit.register(_cleanup_spawned_procs)


class Connection:
    """A single opencode server connection."""

    def __init__(self, name, base_url, password, is_local=False, source="dynamic"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.password = password or None
        self.is_local = is_local
        self.source = source  # spawned / env / dynamic
        self.server_version = None
        self.sessions_warned = {}  # session_id -> server_version at warning time
        self.spawned_proc = None
        self.subtree_unsupported = set()  # subtree capability keys proven unavailable on this server

    def auth_header(self):
        if not self.password:
            return None  # No credential source: do not send Authorization (some remotes use an empty username/password)
        token = base64.b64encode(
            ("opencode:" + self.password).encode("utf-8")
        ).decode("ascii")
        return "Basic " + token

    def default_auto_permission(self):
        return "once" if self.is_local else "manual"

    def describe(self):
        if self.server_version == DEVELOPMENT_BASELINE_VERSION:
            check = "ok"
        elif self.server_version:
            check = "mismatch(%s)" % self.server_version
        else:
            check = "unknown"
        return {
            "name": self.name,
            "url": self.base_url,
            "local": self.is_local,
            "source": self.source,
            "version": self.server_version,
            "baseline": DEVELOPMENT_BASELINE_VERSION,
            "baseline_check": check,
        }


def _build_opener():
    """Opener that never follows 3xx redirects (see _NoRedirectHandler: the default
    handler would replay the Authorization header to an unvalidated Location target)."""
    return urllib.request.build_opener(_NoRedirectHandler)


def _raw_probe(conn, timeout=8.0):
    """Raw GET /api/info, returns the parsed dict; network failure raises a connection exception, a non-JSON response raises ValueError."""
    req = urllib.request.Request(conn.base_url + "/api/info")
    header = conn.auth_header()
    if header:
        req.add_header("Authorization", header)
    req.add_header("Accept", "application/json")
    with _build_opener().open(req, timeout=timeout) as resp:
        length = resp.headers.get("Content-Length")
        if length:
            try:
                check_response_size(int(length))
            except ValueError:
                pass
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    check_response_size(len(raw))
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Response is not JSON: %s" % exc)


def _ensure_version(conn):
    """Creation-time check (hard gate).

    Unreachable = availability; reachable but /api/info is non-JSON or returns 404/5xx = compatibility (not the opencode API);
    401 = authentication problem (wrong password).
    A size-cap abort (OpenCodeError) is a local protection event, not a server state: it is re-raised
    verbatim instead of being reclassified (it must not be reported as "cannot connect").
    """
    try:
        info = _raw_probe(conn)
    except OpenCodeError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise OpenCodeError(
                "[availability] %s(%s) authentication rejected (401): wrong password or password changed"
                % (conn.name, conn.base_url),
                kind="availability",
            )
        raise OpenCodeError(
            "[compatibility] %s(%s) is reachable but /api/info returned HTTP %s, which is not the opencode API"
            " (looks like some other service; baseline v%s)"
            % (conn.name, conn.base_url, exc.code, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except ValueError:
        raise OpenCodeError(
            "[compatibility] %s(%s) is reachable but /api/info did not return opencode v2 API JSON"
            " (observed cases: without correct credentials the request falls back to the Web UI, or this is another service; baseline v%s. "
            "If this is confirmed to be opencode, check the password)"
            % (conn.name, conn.base_url, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    except Exception as exc:
        raise OpenCodeError(
            "[availability] Cannot connect to opencode server %s(%s): %s"
            % (conn.name, conn.base_url, exc),
            kind="availability",
        )
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise OpenCodeError(
            "[compatibility] %s is reachable but /api/info has no version field; the API looks completely reworked"
            " (development baseline v%s)" % (conn.name, DEVELOPMENT_BASELINE_VERSION),
            kind="compatibility",
        )
    conn.server_version = info["version"]
    return info


def _spawn_local_serve():
    """Spawn a dedicated local serve: random high port + random password.

    No opencode on PATH → an availability error that states the user's environment problem; no retry.
    Child-process model: lives as long as this MCP instance; multiple instances use random ports and do not conflict.
    """
    if shutil.which("opencode") is None:
        raise OpenCodeError(
            "[availability] No opencode command on PATH, cannot spawn the local server. "
            "Install opencode or add it to PATH and retry (a user environment problem; the MCP will not try again).",
            kind="availability",
        )
    last_err = None
    for _ in range(3):
        port = random.randint(20000, 60000)
        password = (
            base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
        )
        # Minimal spawn environment (behavioral test against opencode v2.0.16, 2026-09-24:
        # a locally spawned `opencode serve` with only PATH + HOME + OPENCODE_SERVER_PASSWORD
        # starts, authenticates, and completes a real model turn). PATH is needed to find the
        # opencode binary; HOME so it can read its own config; everything else (LLM / provider
        # API keys etc.) is deliberately NOT passed, so a compromised opencode or a plugin it
        # loads cannot read the whole host environment from its process.
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "OPENCODE_SERVER_PASSWORD": password,
        }
        try:
            proc = subprocess.Popen(
                ["opencode", "serve", "--port", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception as exc:
            last_err = exc
            continue
        conn = Connection(
            DEFAULT_LOCAL_NAME,
            "http://127.0.0.1:%d" % port,
            password,
            is_local=True,
            source="spawned",
        )
        conn.spawned_proc = proc
        _track_spawned_proc(proc)  # Owned by this process: killed on exit (atexit / signals)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # Port in use, etc.: retry with another port
            try:
                info = _raw_probe(conn, timeout=2.0)
                if isinstance(info, dict) and info.get("version"):
                    conn.server_version = info["version"]
                    return conn
            except Exception:
                pass
            time.sleep(0.3)
        _kill_proc(proc)  # Failed attempt: kill and reap before trying another port
    raise OpenCodeError(
        "[availability] Failed to spawn the local opencode serve (none of 3 random ports became ready): %s"
        % last_err,
        kind="availability",
    )


def _local_connection():
    """Local connection: explicit env direct connection, otherwise spawn a dedicated serve; a per-process singleton."""
    with _LOCAL_LOCK:
        with _STATE_LOCK:
            conn = _CONNECTIONS.get(DEFAULT_LOCAL_NAME)
        if conn is not None:
            return conn
        url = os.environ.get("OPENCODE_URL")
        if url:
            conn = Connection(
                DEFAULT_LOCAL_NAME,
                url,
                os.environ.get("OPENCODE_PASSWORD") or "opencode",
                is_local=True,
                source="env",
            )
            _ensure_version(conn)
        else:
            conn = _spawn_local_serve()
        with _STATE_LOCK:
            _CONNECTIONS[DEFAULT_LOCAL_NAME] = conn
        log(
            "[opencode-mcp] local connection ready:",
            conn.base_url,
            "version=",
            conn.server_version,
        )
        return conn


def _is_disallowed_ip(ip):
    """Blocked address set: loopback / private / link-local (incl. 169.254.169.254 cloud
    metadata) / reserved / multicast / unspecified, plus IANA special ranges that CPython's
    ipaddress flags do not cover (100.64.0.0/10 CGNAT, 6.0.0.0/8, 7.0.0.0/8).

    IPv4-mapped (::ffff:a.b.c.d) and IPv4-compatible (::a.b.c.d) forms are unwrapped to their
    IPv4 address so an IPv6 literal cannot smuggle a blocked IPv4 address past the gate; pure
    IPv4 literals get the same full predicate. Single source of truth for both the literal-IP
    path and the resolved-hostname path."""
    v4 = ip
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            v4 = mapped
        elif (int(ip) >> 32) == 0:
            # IPv4-compatible (::a.b.c.d, 0:0::/96): unroutable in practice, but unwrapped
            # here so the blocklist can never be bypassed by an alternate literal form.
            # int(ip) is the 128-bit value; upper 96 bits zero => low 32 bits are the IPv4.
            v4 = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
        else:
            # Pure IPv6 (global unicast, ULA, multicast, ...): no IPv4 special ranges apply.
            return (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            )
    return (
        v4.is_loopback
        or v4.is_private
        or v4.is_link_local
        or v4.is_reserved
        or v4.is_multicast
        or v4.is_unspecified
        or v4 in ipaddress.ip_network("100.64.0.0/10")
        or v4 in ipaddress.ip_network("6.0.0.0/8")
        or v4 in ipaddress.ip_network("7.0.0.0/8")
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow 3xx.

    CPython's default HTTPRedirectHandler copies every request header except
    Content-Length / Content-Type -- including Authorization: Basic *** -- to the
    unvalidated Location target. A public attacker host that answers /api/info with
    a 302 to http://169.254.169.254/ (or 127.0.0.1:4096) would restore the SSRF +
    credential-injection primitive that _validate_remote_url is meant to close, on
    every request, not just at registration. Any 3xx is a failure of the opencode
    API contract (the API never redirects); fail closed.
    """

    def http_error_30x(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed: %s" % msg, headers, fp)

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_30x


def _validate_remote_url(base_url):
    """Validate a caller-supplied remote opencode URL before any credential or request is sent to it.

    A connect_server request comes from the MCP client (in production: an LLM). Without this
    gate, a single crafted call (directly or via a prompt injection in the client's context)
    could point this process at an attacker-controlled host and hand it the caller-supplied
    password via the Authorization header (SSRF + credential exfiltration + internal network
    scanning). This is the defense for that path: http/https only, an explicit host required
    (no file://, no schemeless, no control characters), and no private / loopback / link-local
    / reserved / cloud-metadata addresses (IPv4 + IPv6). A human who genuinely needs to connect
    to a local server uses the documented OPENCODE_URL / OPENCODE_PASSWORD environment
    variables, which this process's operator controls directly.

    Fail-closed semantics (review 2026-09-24):
    - IP literal: blocked when _is_disallowed_ip matches (incl. IPv6-mapped / IPv4-compatible
      unwrapping and the CGNAT 100.64/10, 6/8, 7/8 IANA special ranges).
    - Hostname: resolve via getaddrinfo. A pure lookup failure (no records) passes -- the
      first real request then fails with a clean [availability] error. But when the name
      resolves, the check is fail-closed: it blocks when ANY record is a disallowed address
      (a mixed public+private set lets the attacker order a private record first), and also
      when records exist but none can be parsed as an address (e.g. a scoped link-local
      fe80::1%eth0 parses and is blocked as link-local; a record the resolver returns that
      is not an IP at all is blocked too) -- the request would otherwise proceed to an
      unvalidated address.
    - Rebinding: http_request re-runs this validation before every request on a dynamic
      connection, so a name that is public at registration but rebinds to 169.254.169.254
      before the first request is caught at request time.
    """
    if not isinstance(base_url, str):
        raise OpenCodeError("url must be a string")
    try:
        parts = urllib.parse.urlsplit(base_url)
    except Exception as exc:
        raise OpenCodeError("url %r is not a valid URL: %s" % (base_url, exc))
    if parts.scheme not in ("http", "https"):
        raise OpenCodeError(
            "url scheme must be http or https (got %r); file:// and other local schemes "
            "are not allowed for remote connections" % parts.scheme
        )
    if any(ord(c) < 0x20 for c in base_url):
        raise OpenCodeError("url must not contain control characters")
    try:
        host = parts.hostname
        parts.port  # forces ValueError on a malformed port
    except ValueError as exc:
        raise OpenCodeError("url %r is not a valid URL: %s" % (base_url, exc))
    if not host:
        raise OpenCodeError("url must include a host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (not an IP literal). The system resolver (getaddrinfo) also interprets
        # non-standard IPv4 encodings -- decimal ("2130706433"), hex ("0x7f.0.0.1"), octal
        # ("0177.0.0.1"), short-form ("127.1") -- which ipaddress.ip_address() rejects, so
        # resolving the name is the only way to see where such a literal actually points.
        try:
            records = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
        except (socket.gaierror, OSError):
            return  # unresolvable now; the first request reports [availability]
        resolved = set()
        for rec in records:
            try:
                resolved.add(ipaddress.ip_address(rec[4][0]))
            except (ValueError, IndexError, TypeError):
                continue
        if any(_is_disallowed_ip(ip) for ip in resolved):
            raise OpenCodeError(
                "url host %r resolves to (among other records) a private / loopback / link-local / "
                "reserved address and cannot be registered as a remote connection (connect_server is "
                "for remote opencode servers only); use the OPENCODE_URL / OPENCODE_PASSWORD "
                "environment variables to point this MCP at a server the operator runs locally" % host
            )
        if records and not resolved:
            raise OpenCodeError(
                "url host %r resolves to addresses that cannot be validated as public "
                "(no parseable public A/AAAA record); refusing to connect (fail-closed); use the "
                "OPENCODE_URL / OPENCODE_PASSWORD environment variables for a server the operator "
                "runs locally" % host
            )
        return
    if _is_disallowed_ip(ip):
        raise OpenCodeError(
            "url host %r is a private / loopback / link-local / reserved address and cannot "
            "be registered as a remote connection (connect_server is for remote opencode "
            "servers only); use the OPENCODE_URL / OPENCODE_PASSWORD environment variables to "
            "point this MCP at a server the operator runs locally" % host
        )


def _register_connection(name, base_url, password, dynamic=False):
    if not name or not isinstance(name, str):
        raise OpenCodeError("Missing required parameter name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("Connection name %r is reserved and cannot be used" % name)
    if dynamic:
        _validate_remote_url(base_url)
    # Fully serial: eliminates the check-then-write race in concurrent registration of the same name (registration is infrequent, so serial is fine)
    with _REGISTER_LOCK:
        with _STATE_LOCK:
            if name in _CONNECTIONS:
                raise OpenCodeError(
                    "Connection name already exists: %s (see list_servers)" % name
                )
        conn = Connection(name, base_url, password, is_local=False, source="dynamic")
        _ensure_version(conn)  # Creation-time check (hard gate)
        with _STATE_LOCK:
            _CONNECTIONS[name] = conn
    return conn


def _resolve_connection(args):
    """server parameter > session routing > local. An unknown name errors and lists the existing connections."""
    name = args.get("server")
    session_id = args.get("session_id")
    if not name and session_id:
        with _STATE_LOCK:
            name = _SESSION_ROUTE.get(session_id)
    if not name or name == DEFAULT_LOCAL_NAME:
        return _local_connection()
    with _STATE_LOCK:
        conn = _CONNECTIONS.get(name)
        names = sorted(_CONNECTIONS) or [DEFAULT_LOCAL_NAME]
    if conn is None:
        raise OpenCodeError(
            "Unknown connection %r. Existing connections: %s (a remote must be registered with connect_server first)"
            % (name, ", ".join(names))
        )
    return conn


def _route_session(session_id, conn):
    if not session_id:
        return
    with _STATE_LOCK:
        _SESSION_ROUTE[session_id] = conn.name
        while len(_SESSION_ROUTE) > SESSION_ROUTE_LIMIT:
            _SESSION_ROUTE.pop(next(iter(_SESSION_ROUTE)))


def _remove_connection(name):
    with _STATE_LOCK:
        conn = _CONNECTIONS.pop(name, None)
        if conn is not None:
            stale = [sid for sid, n in _SESSION_ROUTE.items() if n == name]
            for sid in stale:
                _SESSION_ROUTE.pop(sid, None)
    return conn


# Version warning: per (connection, session), deduplicated by the version already warned (visible to new sessions, no flooding within the same session)


def _version_warning_for(conn):
    if not conn.server_version:
        return None
    if conn.server_version == DEVELOPMENT_BASELINE_VERSION:
        return None
    major = (
        conn.server_version.split(".")[0]
        != DEVELOPMENT_BASELINE_VERSION.split(".")[0]
    )
    return {
        "server": conn.name,
        "baseline": DEVELOPMENT_BASELINE_VERSION,
        "current": conn.server_version,
        "severity": "high" if major else "low",
        "message": "opencode server (%s) version %s does not match the MCP development baseline %s; %s, behavior may differ."
        % (
            conn.name,
            conn.server_version,
            DEVELOPMENT_BASELINE_VERSION,
            "different major version, high compatibility risk" if major else "minor version difference",
        ),
    }


def _warn_for_result(conn, session_id, result):
    if session_id is None or not isinstance(result, dict):
        return result
    warning = _version_warning_for(conn)
    if not warning:
        return result
    with _STATE_LOCK:
        if conn.sessions_warned.get(session_id) == conn.server_version:
            return result
        conn.sessions_warned[session_id] = conn.server_version
        while len(conn.sessions_warned) > 2000:
            conn.sessions_warned.pop(next(iter(conn.sessions_warned)))
    result = dict(result)
    result["api_version_warning"] = warning
    return result


def _warn_choke(args, result):
    """Unified warning injection point for the tools/call success path: per (connection, session)."""
    if not isinstance(result, dict):
        return result
    session_id = args.get("session_id") or result.get("session_id")
    if not session_id:
        return result
    try:
        conn = _resolve_connection(dict(args, session_id=session_id))
    except OpenCodeError:
        return result
    return _warn_for_result(conn, session_id, result)


def unwrap(payload):
    """opencode responses are usually {"data": ...}; uniformly extract data."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# ---------------------------------------------------------------------------
# Concurrency and cancellation (MCP: notifications/cancelled + request thread pool)
# ---------------------------------------------------------------------------

# Set of request ids cancelled by the caller (written by the reader thread, read by poll threads)
_CANCELLED = set()
_STATE_LOCK = threading.Lock()
# stdout single-writer lock: multi-threaded responses must be written serially
_OUT_LOCK = threading.Lock()
# Request id currently handled by the worker thread (thread-local)
_CURRENT = threading.local()


def _request_cancelled(request_id):
    with _STATE_LOCK:
        return request_id in _CANCELLED


def _current_request_cancelled():
    request_id = getattr(_CURRENT, "request_id", None)
    return request_id is not None and _request_cancelled(request_id)


# ---------------------------------------------------------------------------
# HTTP (with connection context and failure classification)
# ---------------------------------------------------------------------------


def http_request(conn, method, path, body=None, query=None):
    """Perform a request against the given connection; on failure classify as availability / compatibility / other (dump the raw error)."""
    if not conn.is_local:
        # Re-validation at request time: connect_server validated the url at registration,
        # but a DNS name can rebind between then and now (public record -> 169.254.169.254).
        # Re-resolving per request narrows the rebinding window to this one in-flight
        # request without adding a network hop for the (already-validated) IP-literal case.
        # Full elimination would require pinning the validated IP literal for the connect.
        _validate_remote_url(conn.base_url)
    url = conn.base_url + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url = url + "?" + urllib.parse.urlencode(clean)

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    header = conn.auth_header()
    if header:
        req.add_header("Authorization", header)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    raw = b""
    try:
        with _build_opener().open(req, timeout=HTTP_TIMEOUT) as resp:
            length = resp.headers.get("Content-Length")
            if length:
                try:
                    check_response_size(int(length))
                except ValueError:
                    pass
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        # Outside the try on purpose: a cap abort is a local protection event and must
        # surface as [other] verbatim -- it is not a server failure and must not be
        # reclassified (or re-probed) by _classify_failure.
        check_response_size(len(raw))
    except OpenCodeError:
        # A cap abort (declared or actual body size) raised inside the try must propagate
        # verbatim as [other] -- it must not fall into the generic handler below, which
        # would re-probe the endpoint and reclassify it as a server failure.
        raise
    except urllib.error.HTTPError as exc:
        try:
            # Capped: a hostile endpoint may answer any tool call (e.g. get_messages)
            # with a 404/5xx carrying a multi-GB body; the error body is an error detail,
            # not the payload, so 64 KiB is far more than enough to keep.
            detail = exc.read(65536).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise _classify_failure(
            conn,
            method,
            path,
            "HTTP %s %s %s -> %s %s"
            % (exc.code, method, path, exc.reason, detail[:1500]),
            http_status=exc.code,
        )
    except Exception as exc:  # URLError / timeout, etc.
        raise _classify_failure(
            conn, method, path, "%s: %s" % (type(exc).__name__, exc)
        )

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise OpenCodeError(
            "[other] Response is not valid JSON (%s %s): %s" % (method, path, exc)
        )


def _classify_failure(conn, method, path, original, http_status=None):
    """After a failure, re-probe the version and classify as availability / compatibility / other accordingly.

    The primary criterion is the shape of the original failure (GET 404 = endpoint gone = compatibility; network layer = availability),
    the version re-probe corroborates and updates the connection's version record; other must preserve the full raw error for reporting.
    """
    probe = None
    probe_err = None
    try:
        probe = _raw_probe(conn, timeout=5.0)
    except Exception as exc:
        probe_err = exc
    if isinstance(probe, dict) and probe.get("version"):
        conn.server_version = probe["version"]
    ctx = "server=%s version=%s baseline=%s" % (
        conn.name,
        conn.server_version,
        DEVELOPMENT_BASELINE_VERSION,
    )
    if http_status in (401, 403):
        raise OpenCodeError(
            "[availability] %s authentication rejected (HTTP %s): wrong or expired password (%s). Original: %s"
            % (conn.name, http_status, ctx, original),
            kind="availability",
        )
    if http_status == 404 and method == "GET":
        raise OpenCodeError(
            "[compatibility] Endpoint gone (GET %s -> 404); the API looks reworked (%s). Original: %s"
            % (path, ctx, original),
            kind="compatibility",
        )
    if probe is None:
        raise OpenCodeError(
            "[availability] Service unreachable (%s); re-probing /api/info also failed: %s. Original: %s"
            % (conn.base_url, probe_err, original),
            kind="availability",
        )
    if not (isinstance(probe, dict) and probe.get("version")):
        raise OpenCodeError(
            "[compatibility] Re-probe of /api/info has no version field; the API looks reworked (%s). Original: %s"
            % (ctx, original),
            kind="compatibility",
        )
    raise OpenCodeError(
        "[other] Request failed (%s). Raw error: %s. Can be reported to the developer verbatim."
        % (ctx, original),
        kind="other",
    )


# ---------------------------------------------------------------------------
# Data shaping helpers
# ---------------------------------------------------------------------------

def _text_parts(message):
    """Get the text of all text parts in an assistant message."""
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if text:
                    out.append(text)
    return out


def _reasoning_parts(message):
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "reasoning":
                text = part.get("text")
                if text:
                    out.append(text)
    return out


def _tool_parts(message):
    out = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool":
                state = part.get("state") or {}
                out.append(
                    {
                        "name": part.get("name"),
                        "status": state.get("status"),
                    }
                )
    return out


def _message_text(message):
    """Get the readable text of any message."""
    if isinstance(message.get("text"), str):
        return message["text"]
    return "\n".join(_text_parts(message))


def _is_pending_form(item):
    """List items may carry state.status; only pending counts as outstanding; no state counts as pending."""
    state = item.get("state")
    if not isinstance(state, dict):
        return True
    return state.get("status") == "pending"


def _form_summary(item):
    fields = []
    for field in item.get("fields") or []:
        if not isinstance(field, dict):
            continue
        fields.append(
            {
                "key": field.get("key"),
                "title": field.get("title"),
                "type": field.get("type"),
                "required": field.get("required", False),
                "options": field.get("options"),
                "description": field.get("description"),
            }
        )
    return {
        "id": item.get("id"),
        "sessionID": item.get("sessionID"),
        "title": item.get("title"),
        "fields": fields,
    }


def _format_message(message):
    mtype = message.get("type")
    created = (message.get("time") or {}).get("created")
    result = {
        "id": message.get("id"),
        "type": mtype,
        "time": created,
    }
    if mtype == "assistant":
        result["agent"] = message.get("agent")
        result["model"] = message.get("model")
        result["text"] = "\n".join(_text_parts(message))
        reasoning = _reasoning_parts(message)
        if reasoning:
            result["reasoning"] = reasoning
        tools = _tool_parts(message)
        if tools:
            result["tools"] = tools
        result["completed"] = bool((message.get("time") or {}).get("completed"))
    else:
        result["text"] = _message_text(message)
        if mtype == "shell":
            result["command"] = message.get("command")
    return result


def fetch_messages(conn, session_id, limit=100):
    """Fetch the **latest** limit messages of a session, returned in ascending time order.

    In practice opencode's order=asc&limit returns the "earliest N" (verified on 200+ message sessions),
    which misaligns gate lookup / incremental cursors on long sessions; so we uniformly use order=desc to take the tail window and then reverse it.
    """
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/message" % urllib.parse.quote(session_id, safe=""),
        query={"order": "desc", "limit": limit},
    )
    data = unwrap(payload)
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        data = data["messages"]
    if not isinstance(data, list):
        return []
    data.reverse()
    return data


def fetch_permissions(conn, session_id):
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/permission" % urllib.parse.quote(session_id, safe=""),
    )
    data = unwrap(payload)
    return data if isinstance(data, list) else []


def fetch_forms(conn, session_id, pending_only=True):
    payload = http_request(
        conn,
        "GET",
        "/api/session/%s/form" % urllib.parse.quote(session_id, safe=""),
    )
    data = unwrap(payload)
    if not isinstance(data, list):
        return []
    if pending_only:
        data = [item for item in data if _is_pending_form(item)]
    return data


# ---------------------------------------------------------------------------
# Subtree (subagent) awareness
#
# A parent's own outcome is per-turn and can be succeeded/idle while child sessions are still
# working (background delegation), so `succeeded` is only accepted once the whole subtree is
# quiescent. Structure is resolved purely by reverse parentID closure; activity and pending
# interactions are re-read every poll. Everything is fail-closed: an unexpected shape, missing
# key, 404 or request failure means UNKNOWN and success is never declared on UNKNOWN.
# ---------------------------------------------------------------------------


def _capability_unsupported(conn, cap):
    return cap in conn.subtree_unsupported


def _mark_capability_unsupported(conn, cap, exc):
    """Record (once) that a server cannot support a subtree capability; returns True if newly marked."""
    with _STATE_LOCK:
        if cap in conn.subtree_unsupported:
            return False
        conn.subtree_unsupported.add(cap)
    log(
        "[opencode-mcp] subtree capability %s unsupported on %s, falling back to legacy: %s"
        % (cap, conn.name, exc)
    )
    return True


def _is_capability_error(exc):
    """A compatibility classification (404 GET / reworked API) means the endpoint genuinely does not exist."""
    return isinstance(exc, OpenCodeError) and exc.kind == "compatibility"


def _active_session_ids(conn):
    """Return (set_of_active_session_ids, verified).

    Server-wide map of sessions with a live foreground drain. A blocked-on-permission session still
    appears as running; an idle parent is absent. verified=False is fail-closed: a missing or odd
    payload must never be read as "nothing is active".
    """
    if _capability_unsupported(conn, CAP_SESSION_ACTIVE):
        return None, False
    try:
        payload = http_request(conn, "GET", "/api/session/active")
    except OpenCodeError as exc:
        if _is_capability_error(exc):
            _mark_capability_unsupported(conn, CAP_SESSION_ACTIVE, exc)
        return None, False
    # The measured shape is {"data": {session_id: {"type": "running"}}}; a payload without that
    # wrapper is UNKNOWN (fail-closed), never an empty active set.
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None, False
    active = set()
    for sid, value in payload["data"].items():
        if not isinstance(sid, str) or not isinstance(value, dict):
            return None, False
        active.add(sid)
    return active, True


def _subtree_ids(conn, root_id):
    """Reverse closure of parentID (direct children via ?parentID= only), with caps and a cycle guard.

    Returns {"root", "ids", "info", "truncated"} or None when the structure cannot be verified
    (fail-closed). Structure only: no activity is cached, no message content is inspected.
    """
    if not root_id:
        return None
    if _capability_unsupported(conn, CAP_SESSION_PARENTID):
        return None
    ids = [root_id]
    info = {root_id: None}
    depth = {root_id: 0}
    seen = {root_id}
    frontier = [root_id]
    truncated = False
    cursor = 0
    while cursor < len(frontier):
        current = frontier[cursor]
        cursor += 1
        cur_depth = depth[current]
        try:
            payload = http_request(
                conn, "GET", "/api/session", query={"parentID": current}
            )
        except OpenCodeError as exc:
            if _is_capability_error(exc):
                _mark_capability_unsupported(conn, CAP_SESSION_PARENTID, exc)
            return None
        data = unwrap(payload)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            data = data["sessions"]  # tolerate the sessions-wrapped variant list_sessions also accepts
        if not isinstance(data, list):
            return None
        for child in data:
            if not isinstance(child, dict):
                return None
            child_id = child.get("id")
            if not isinstance(child_id, str) or not child_id:
                return None
            if child_id in seen:
                continue  # cycle / duplicate guard
            if cur_depth + 1 > SUBTREE_MAX_DEPTH:
                truncated = True  # a deeper node exists but is outside the depth cap
                continue
            if len(seen) >= SUBTREE_MAX_NODES:
                truncated = True  # node cap hit
                break
            seen.add(child_id)
            ids.append(child_id)
            frontier.append(child_id)
            depth[child_id] = cur_depth + 1
            info[child_id] = child
            _route_session(child_id, conn)  # route replies for this child to the right server
        if truncated:
            break
    _route_session(root_id, conn)
    return {"root": root_id, "ids": ids, "info": info, "truncated": truncated}


def _subagent_entries(tree, active):
    """Payload-ready subagent list (subtree nodes excluding the root). active=None means unknown."""
    entries = []
    root = tree.get("root")
    for sid in tree.get("ids") or []:
        if sid == root:
            continue
        node = (tree.get("info") or {}).get(sid) or {}
        entries.append(
            {
                "session_id": sid,
                "agent": node.get("agent"),
                "model": node.get("model"),
                "title": node.get("title"),
                "outcome": node.get("outcome"),
                "active": (sid in active) if active is not None else None,
                "parentID": node.get("parentID"),
            }
        )
    return entries


def _fetch_global_pending(conn, path, cap):
    """Global (location-wide) pending list. Returns (items, status), status in ok/unsupported/unknown.

    The global list may contain requests from other callers/MCPs on a shared or remote server;
    callers must filter by exact subtree membership.
    """
    if _capability_unsupported(conn, cap):
        return None, "unsupported"
    try:
        payload = http_request(conn, "GET", path)
    except OpenCodeError as exc:
        if _is_capability_error(exc):
            _mark_capability_unsupported(conn, cap, exc)
            return None, "unsupported"
        return None, "unknown"
    # The measured shape is {"location": ..., "data": [...]}; a payload without that wrapper is
    # UNKNOWN (fail-closed), never "nothing pending".
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None, "unknown"
    for item in payload["data"]:
        if not isinstance(item, dict):
            return None, "unknown"
    return payload["data"], "ok"


def _session_pending(conn, sid, kind):
    """Strict per-session pending list for one node: (items, verified).

    The forgiving fetch_permissions/fetch_forms helpers coerce anything to [], which would turn a
    malformed or empty-bodied response into "nothing pending" and let a false success through, so
    this fallback path checks the shape itself and re-adds the owner id that per-session entries omit.
    """
    try:
        payload = http_request(
            conn,
            "GET",
            "/api/session/%s/%s" % (urllib.parse.quote(sid, safe=""), kind),
        )
    except OpenCodeError as exc:
        log("[opencode-mcp] per-session %s lookup failed" % kind, sid, exc)
        return None, False
    data = unwrap(payload)
    if not isinstance(data, list):
        return None, False
    items = []
    for item in data:
        if not isinstance(item, dict):
            return None, False
        if kind == "form" and not _is_pending_form(item):
            continue
        if not item.get("sessionID"):
            item = dict(item, sessionID=sid)
        items.append(item)
    return items, True


def _pending_interactions_tree(conn, session_ids):
    """Aggregate pending permissions/forms over a subtree, filtered by exact session-id membership.

    Global endpoints are preferred for the sweep, with per-session endpoints as the fallback.
    Returns {"permissions", "forms", "verified"}; verified=False is fail-closed.
    """
    idset = set(session_ids)
    verified = True

    perms, pstat = _fetch_global_pending(
        conn, "/api/permission/request", CAP_GLOBAL_PERMISSION
    )
    if pstat == "ok":
        perms = [p for p in perms if p.get("sessionID") in idset]
    else:
        perms = []
        if pstat == "unknown":
            verified = False
        else:
            for sid in session_ids:
                items, ok = _session_pending(conn, sid, "permission")
                if not ok:
                    verified = False
                    break
                perms.extend(p for p in items if p.get("sessionID") in idset)

    forms, fstat = _fetch_global_pending(conn, "/api/form", CAP_GLOBAL_FORM)
    if fstat == "ok":
        forms = [f for f in forms if f.get("sessionID") in idset]
    else:
        forms = []
        if fstat == "unknown":
            verified = False
        else:
            for sid in session_ids:
                items, ok = _session_pending(conn, sid, "form")
                if not ok:
                    verified = False
                    break
                forms.extend(f for f in items if f.get("sessionID") in idset)

    if not verified:
        return {"permissions": [], "forms": [], "verified": False}
    return {"permissions": perms, "forms": forms, "verified": True}


def _subtree_snapshot(conn, root_id):
    """Fresh full subtree state for gating/reporting (structure is never cached).

    verified=False -> UNKNOWN (fail-closed, the caller must not declare success).
    legacy=True -> the server cannot support verification at all; the caller may use the legacy path.
    """
    base = {
        "verified": False,
        "legacy": False,
        "truncated": False,
        "ids": [root_id],
        "subagents": [],
        "pending_subagents": None,
        "permissions": [],
        "forms": [],
    }
    tree = _subtree_ids(conn, root_id)
    if tree is None:
        if _capability_unsupported(conn, CAP_SESSION_PARENTID):
            base["legacy"] = True
        return base
    ids = tree["ids"]
    base["ids"] = ids
    base["truncated"] = tree["truncated"]

    active, active_ok = _active_session_ids(conn)
    if not active_ok:
        base["subagents"] = _subagent_entries(tree, None)
        if _capability_unsupported(conn, CAP_SESSION_ACTIVE):
            base["legacy"] = True
        return base
    if tree["truncated"]:
        # Never declare success on a truncated tree; still report what we saw for diagnostics.
        base["subagents"] = _subagent_entries(tree, active)
        base["pending_subagents"] = sum(
            1 for sid in ids if sid != root_id and sid in active
        )
        return base

    inter = _pending_interactions_tree(conn, ids)
    base["subagents"] = _subagent_entries(tree, active)
    if not inter["verified"]:
        return base
    base["verified"] = True
    base["permissions"] = inter["permissions"]
    base["forms"] = inter["forms"]
    base["pending_subagents"] = sum(
        1 for sid in ids if sid != root_id and sid in active
    )
    return base


def _subtree_quiescent(snap):
    """True only when verification succeeded and nothing in the subtree is live or waiting."""
    return bool(
        snap.get("verified")
        and not snap.get("truncated")
        and (snap.get("pending_subagents") or 0) == 0
        and not snap.get("permissions")
        and not snap.get("forms")
    )


def _attach_subtree(payload, snap):
    """Attach subtree fields to a payload without touching existing fields."""
    payload["subagents"] = snap.get("subagents") or []
    payload["pending_subagents"] = snap.get("pending_subagents")
    payload["subtree_truncated"] = bool(snap.get("truncated"))
    payload["subtree_verified"] = bool(snap.get("verified"))
    if not snap.get("verified"):
        payload.setdefault(
            "note",
            SUBTREE_UNSUPPORTED_NOTE if snap.get("legacy") else SUBTREE_UNVERIFIED_NOTE,
        )
    return payload


def _autoreply_subtree_permissions(conn, snap, decision, replied):
    """Answer every pending permission in the subtree by POSTing to each request's own session.

    `replied` carries the ids already answered in this wait, so a request that stays visible for
    more than one poll is not re-POSTed every second.
    """
    for req in snap.get("permissions") or []:
        rid = req.get("id")
        owner = req.get("sessionID")
        if not rid or not owner or rid in replied:
            continue
        replied.add(rid)
        try:
            http_request(
                conn,
                "POST",
                "/api/session/%s/permission/%s/reply"
                % (urllib.parse.quote(owner, safe=""), urllib.parse.quote(rid, safe="")),
                body={"decision": decision},
            )
        except OpenCodeError as exc:
            log("[opencode-mcp] subtree permission auto-reply failed", owner, rid, exc)


def _enrich_forms(conn, forms):
    """Defensive fallback: refetch per owning session when the global list lacks field detail.

    Measured on the development baseline: /api/form already returns `fields` and `sessionID`, and
    /api/session/{id}/form does the same, so the early return below is the path actually taken and
    the refetch is unreachable there. It is kept for other builds whose global list is thinner, and
    it re-adds the owner id because the per-session shape may omit it (this baseline happens to
    include it as well). Best-effort: on any failure the original list is returned unchanged.
    """
    if not forms:
        return forms
    if all(isinstance(f.get("fields"), list) and f.get("fields") for f in forms):
        return forms
    owners = sorted({f.get("sessionID") for f in forms if f.get("sessionID")})
    if not owners:
        return forms
    out = []
    for owner in owners:
        try:
            node_forms = fetch_forms(conn, owner, pending_only=True)
        except OpenCodeError:
            return forms
        for form in node_forms:
            if isinstance(form, dict) and not form.get("sessionID"):
                form = dict(form, sessionID=owner)  # per-session forms omit the owner id
            out.append(form)
    return out or forms


def _subtree_needs_payload(conn, root_id, snap, kind):
    """needs_permission/needs_form payload whose session_id is the request's actual owning session."""
    if kind == "permission":
        items = snap.get("permissions") or []
        owners = [p.get("sessionID") for p in items if p.get("sessionID")]
        return {
            "status": "needs_permission",
            "server": conn.name,
            "session_id": owners[0] if owners else root_id,
            "root_session_id": root_id,
            "requests": [
                {
                    "id": p.get("id"),
                    "sessionID": p.get("sessionID"),
                    "action": p.get("action"),
                    "resources": p.get("resources"),
                    "save": p.get("save"),
                }
                for p in items
            ],
            "subagents": snap.get("subagents") or [],
            "pending_subagents": snap.get("pending_subagents"),
            "subtree_truncated": bool(snap.get("truncated")),
            "subtree_verified": bool(snap.get("verified")),
            "note": "Reply with permission_reply, then call wait_session to keep waiting "
            "(the request may belong to a subagent session; use its sessionID).",
        }
    items = _enrich_forms(conn, snap.get("forms") or [])
    owners = [f.get("sessionID") for f in items if f.get("sessionID")]
    return {
        "status": "needs_form",
        "server": conn.name,
        "session_id": owners[0] if owners else root_id,
        "root_session_id": root_id,
        "forms": [_form_summary(f) for f in items],
        "subagents": snap.get("subagents") or [],
        "pending_subagents": snap.get("pending_subagents"),
        "subtree_truncated": bool(snap.get("truncated")),
        "subtree_verified": bool(snap.get("verified")),
        "note": "Reply with form_reply, then call wait_session to keep waiting "
        "(the form may belong to a subagent session; use its sessionID).",
    }


# ---------------------------------------------------------------------------
# Unified wait core (shared by chat / wait_session / compact)
# Terminal determination trusts only the authoritative field Session.outcome (succeeded/failed/interrupted) + the gate message
# timestamp; no message-shape inference (five historical rounds of bugs all came from shape heuristics, now fully removed).
# On top of that, `succeeded` is gated on full-subtree quiescence when wait_for_subagents is set.
# ---------------------------------------------------------------------------


def _build_result(messages):
    assistant_texts = []
    reasoning_texts = []
    tools_used = []
    for message in messages:
        if message.get("type") != "assistant":
            continue
        text = "\n".join(_text_parts(message))
        if text:
            assistant_texts.append(text)
        reasoning_texts.extend(_reasoning_parts(message))
        tools_used.extend(_tool_parts(message))
    result = {
        "assistant_text": "\n\n".join(assistant_texts),
        "tools_used": tools_used,
    }
    if reasoning_texts:
        result["reasoning"] = reasoning_texts
    return result


def _result_payload(
    conn,
    session_id,
    status,
    baseline=None,
    with_result=False,
    time_idle=None,
    subtree=None,
):
    """Terminal response body; last_message_id always provides the incremental cursor, and with_result attaches this round's new replies."""
    try:
        messages = fetch_messages(conn, session_id)
    except OpenCodeError:
        messages = []
    payload = {
        "status": status,
        "server": conn.name,
        "session_id": session_id,
        "time_idle": time_idle,
        "last_message_id": messages[-1].get("id") if messages else None,
    }
    if with_result:
        new = [
            m
            for m in messages
            if baseline is None or m.get("id") not in (baseline or set())
        ]
        payload.update(_build_result(new))
        if not any(m.get("type") == "assistant" for m in new):
            payload.setdefault(
                "note", "Session is already completed/idle; no new replies this round."
            )
    if subtree is not None:
        _attach_subtree(payload, subtree)
    return payload


def _run_until_terminal(
    conn,
    session_id,
    timeout_secs,
    auto_permission="manual",
    gate_message_id=None,
    gate_is_compaction=False,
    baseline=None,
    with_result=False,
    wait_for_subagents=False,
):
    """Poll the session until terminal / needs interaction / timeout / cancellation (the unified wait core).

    Returns (status, payload). status ∈ {succeeded, failed, interrupted,
    compaction_failed, needs_permission, needs_form, timeout, cancelled}

    wait_for_subagents: when set, `succeeded` additionally requires the whole subtree to be
    quiescent (no active node, no pending permission/form anywhere in the subtree). failed /
    interrupted are always returned immediately. auto_permission in once/always/reject answers
    pending permissions of every subtree node by POSTing to each request's owning session.
    """
    started = time.monotonic()
    gate_created = None
    replied_permissions = set()  # ids already auto-replied: never re-POST the same request each poll
    while True:
        if _current_request_cancelled():
            return "cancelled", {"status": "cancelled", "note": "The caller cancelled this request"}

        # a. Permission requests (the root session is always covered)
        try:
            permissions = fetch_permissions(conn, session_id)
        except OpenCodeError:
            permissions = []
        if permissions:
            if auto_permission in SUBTREE_AUTOREPLY:
                for req in permissions:
                    rid = req.get("id")
                    if not rid or rid in replied_permissions:
                        continue
                    replied_permissions.add(rid)
                    try:
                        http_request(
                            conn,
                            "POST",
                            "/api/session/%s/permission/%s/reply"
                            % (
                                urllib.parse.quote(session_id, safe=""),
                                urllib.parse.quote(rid, safe=""),
                            ),
                            body={"decision": auto_permission},
                        )
                    except OpenCodeError as exc:
                        log("[opencode-mcp] permission auto-reply failed", rid, exc)
            else:
                return "needs_permission", {
                    "status": "needs_permission",
                    "server": conn.name,
                    "session_id": session_id,
                    "root_session_id": session_id,
                    "requests": [
                        {
                            "id": p.get("id"),
                            "sessionID": session_id,
                            "action": p.get("action"),
                            "resources": p.get("resources"),
                            "save": p.get("save"),
                        }
                        for p in permissions
                    ],
                    "note": "Reply with permission_reply, then call wait_session to keep waiting.",
                }

        # b. Form requests
        try:
            forms = fetch_forms(conn, session_id, pending_only=True)
        except OpenCodeError:
            forms = []
        if forms:
            return "needs_form", {
                "status": "needs_form",
                "server": conn.name,
                "session_id": session_id,
                "root_session_id": session_id,
                "forms": [_form_summary(f) for f in forms],
                "note": "Reply with form_reply, then call wait_session to keep waiting.",
            }

        # c. Authoritative session state
        info = unwrap(
            http_request(
                conn,
                "GET",
                "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
            )
        )
        if not isinstance(info, dict):
            info = {}
        outcome = info.get("outcome")
        time_idle = (info.get("time") or {}).get("idle")

        # Gate message: cache the created timestamp; in the compact case accept its message terminal state directly
        if gate_message_id is not None and gate_created is None:
            try:
                gate_msgs = fetch_messages(conn, session_id)
            except OpenCodeError:
                gate_msgs = []
            for gm in gate_msgs:
                if gm.get("id") == gate_message_id:
                    gate_created = (gm.get("time") or {}).get("created")
                    if gate_is_compaction:
                        gstatus = gm.get("status")
                        if gstatus == "completed":
                            return "succeeded", _result_payload(
                                conn, session_id, "succeeded", baseline, with_result, time_idle
                            )
                        if gstatus == "failed":
                            return "compaction_failed", {
                                "status": "compaction_failed",
                                "server": conn.name,
                                "session_id": session_id,
                                "note": "Context compaction failed (compaction status=failed).",
                            }
                    break

        if outcome in ("succeeded", "failed", "interrupted"):
            gate_ok = gate_message_id is None or (
                gate_created is not None and (time_idle or 0) > gate_created
            )
            if gate_ok:
                # failed / interrupted are decided outcomes: return immediately, never delayed.
                # A terminal `succeeded` without subtree gating still reports subagent state.
                if outcome != "succeeded" or not wait_for_subagents:
                    payload = _result_payload(
                        conn, session_id, outcome, baseline, with_result, time_idle
                    )
                    if outcome == "succeeded":
                        snap = _subtree_snapshot(conn, session_id)
                        _attach_subtree(payload, snap)
                    return outcome, payload

                # succeeded + wait_for_subagents: the parent outcome is per-turn, so accept it
                # only when the entire subtree is quiescent. The structure and activity map are
                # read fresh here (decision-point refresh), not from an earlier snapshot.
                snap = _subtree_snapshot(conn, session_id)
                if snap["legacy"]:
                    # The server cannot support verification: fall back, but never claim it was verified.
                    payload = _result_payload(
                        conn, session_id, "succeeded", baseline, with_result, time_idle
                    )
                    _attach_subtree(payload, snap)
                    return "succeeded", payload
                if _subtree_quiescent(snap):
                    payload = _result_payload(
                        conn,
                        session_id,
                        "succeeded",
                        baseline,
                        with_result,
                        time_idle,
                        subtree=snap,
                    )
                    return "succeeded", payload

                # Not quiescent (or UNKNOWN). UNKNOWN must never become success: keep polling
                # until the timeout, then report a timeout diagnostic.
                if snap["verified"]:
                    if snap["permissions"]:
                        if auto_permission in SUBTREE_AUTOREPLY:
                            _autoreply_subtree_permissions(
                                conn, snap, auto_permission, replied_permissions
                            )
                        else:
                            return "needs_permission", _subtree_needs_payload(
                                conn, session_id, snap, "permission"
                            )
                    if snap["forms"]:  # forms are never auto-answered
                        return "needs_form", _subtree_needs_payload(
                            conn, session_id, snap, "form"
                        )
                # fall through: active subagents, truncated tree or UNKNOWN -> keep polling

        # d. Timeout (the diagnostics block includes the last message and this round's partial text)
        if time.monotonic() - started >= timeout_secs:
            try:
                msgs = fetch_messages(conn, session_id)
            except OpenCodeError:
                msgs = []
            last = msgs[-1] if msgs else None
            new = [
                m
                for m in msgs
                if baseline is None or m.get("id") not in (baseline or set())
            ]
            snap = _subtree_snapshot(conn, session_id)
            if snap.get("truncated"):
                note = (
                    "Wait timed out (%s seconds); the session is still generating. "
                    "The subagent subtree was truncated by the depth/node caps, so quiescence could not be confirmed."
                    % timeout_secs
                )
            elif not snap.get("verified") and not snap.get("legacy"):
                note = (
                    "Wait timed out (%s seconds); the session is still generating. "
                    "Subagent activity could not be verified (fail-closed): the terminal state was not accepted."
                    % timeout_secs
                )
            else:
                note = (
                    "Wait timed out (%s seconds); the session is still generating."
                    % timeout_secs
                )
            payload = {
                "status": "timeout",
                "server": conn.name,
                "session_id": session_id,
                "partial_text": _build_result(new)["assistant_text"],
                "diagnostics": {
                    "server": conn.name,
                    "outcome": outcome,
                    "last_message": (
                        {
                            "id": last.get("id"),
                            "type": last.get("type"),
                            "status": last.get("status"),
                            "completed": bool(
                                (last.get("time") or {}).get("completed")
                            ),
                        }
                        if isinstance(last, dict)
                        else None
                    ),
                    "pending_permissions": len(permissions),
                    "pending_forms": len(forms),
                    "active_subagents": [
                        {
                            "session_id": s.get("session_id"),
                            "agent": s.get("agent"),
                            "title": s.get("title"),
                            "outcome": s.get("outcome"),
                            "active": s.get("active"),
                        }
                        for s in (snap.get("subagents") or [])
                        if s.get("active")
                    ],
                    "pending_subtree_permissions": len(snap.get("permissions") or []),
                    "pending_subtree_forms": len(snap.get("forms") or []),
                    "pending_subagents": snap.get("pending_subagents"),
                    "subtree_verified": bool(snap.get("verified")),
                    "subtree_truncated": bool(snap.get("truncated")),
                    "suggested_actions": [
                        "get_messages to check current progress",
                        "pending_interactions to check pending interactions",
                        "wait_session to keep waiting",
                        "interrupt to stop generation",
                    ],
                },
                "note": note,
            }
            _attach_subtree(payload, snap)
            return "timeout", payload

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _arg_timeout(args, default=120):
    """Parse the optional timeout_secs parameter and clamp it to [1, 3600] seconds."""
    value = int(args.get("timeout_secs", default) or default)
    return max(1, min(value, 3600))


def _arg_bool(args, name, default=False):
    """Parse an optional boolean argument; an explicit value overrides the default."""
    value = args.get(name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def tool_create_session(args):
    conn = _resolve_connection(args)
    body = {}
    if args.get("title"):
        body["title"] = args["title"]
    if args.get("agent"):
        body["agent"] = args["agent"]
    model_id = args.get("model_id")
    if model_id:
        if "/" not in model_id:
            raise OpenCodeError(
                "model_id must be in 'providerID/modelID' format, got: %r" % model_id
            )
        provider_id, model = model_id.split("/", 1)
        # Model.Ref shape is {"id": ..., "providerID": ...} (observed: the "modelID" key is rejected with 400)
        body["model"] = {"providerID": provider_id, "id": model}

    location = args.get("location")
    if location is not None:
        if not isinstance(location, dict):
            raise OpenCodeError(
                "location must be an object of the form {\"directory\": \"/path/to/project\"}"
            )
        directory = location.get("directory")
        if not isinstance(directory, str) or not directory:
            raise OpenCodeError("location.directory is a required string")
        # Pass through in the Location.PublicRef shape from openapi.json: {directory}
        body["location"] = {"directory": directory}

    data = unwrap(http_request(conn, "POST", "/api/session", body=body))
    if not isinstance(data, dict):
        data = {}
    _route_session(data.get("id"), conn)
    return {
        "session_id": data.get("id"),
        "server": conn.name,
        "title": data.get("title"),
        "agent": data.get("agent"),
        "model": data.get("model"),
    }


def tool_delete_session(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    http_request(
        conn,
        "DELETE",
        "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
    )
    with _STATE_LOCK:
        _SESSION_ROUTE.pop(session_id, None)
    return {"ok": True, "server": conn.name, "session_id": session_id}


def tool_chat(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    # Remote connections default to manual (approval must stay with the caller), local defaults to once
    auto_permission = args.get("auto_permission") or conn.default_auto_permission()
    if auto_permission not in VALID_AUTO_PERMISSION:
        raise OpenCodeError(
            "auto_permission must be one of once/always/reject/manual, got: %r"
            % auto_permission
        )

    baseline = set(m.get("id") for m in fetch_messages(conn, session_id))

    text = args.get("text")
    if text is None or text == "":
        raise OpenCodeError("Missing required parameter text")
    body = {"text": text}

    delivery = args.get("delivery")
    if delivery is not None:
        if delivery not in ("steer", "queue"):
            raise OpenCodeError(
                "delivery must be steer / queue, got: %r" % delivery
            )
        body["delivery"] = delivery

    files = args.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise OpenCodeError("files must be an array, e.g. [{\"uri\": \"...\"}]")
        for idx, item in enumerate(files):
            if not isinstance(item, dict) or not item.get("uri"):
                raise OpenCodeError("files[%d] is missing the required field uri" % idx)
        if files:
            body["files"] = files

    prompt_payload = unwrap(
        http_request(
            conn,
            "POST",
            "/api/session/%s/prompt" % urllib.parse.quote(session_id, safe=""),
            body=body,
        )
    )
    gate_id = (
        prompt_payload.get("id") if isinstance(prompt_payload, dict) else None
    )

    _route_session(session_id, conn)
    wait_for_subagents = _arg_bool(args, "wait_for_subagents", default=False)
    status, payload = _run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission,
        gate_message_id=gate_id,
        baseline=baseline,
        with_result=True,
        wait_for_subagents=wait_for_subagents,
    )
    return payload


def tool_wait_session(args):
    """Wait for the session to reach a terminal or needs-interaction state (a pure state primitive; returns no message content)."""
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    _route_session(session_id, conn)
    wait_for_subagents = _arg_bool(args, "wait_for_subagents", default=True)
    status, payload = _run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission="manual",
        with_result=False,
        wait_for_subagents=wait_for_subagents,
    )
    if status == "succeeded" and "note" not in payload:
        payload["note"] = "Use get_messages(after_message_id=...) to fetch new replies."
    return payload


def tool_get_messages(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    limit = _limit_param(args, 50)
    after = args.get("after_message_id")
    note = None
    _route_session(session_id, conn)
    if after:
        # Incremental fetch: take at most the latest 200, drop after_message_id and everything before it
        messages = fetch_messages(conn, session_id, limit=200)
        idx = next(
            (i for i, m in enumerate(messages) if m.get("id") == after), -1
        )
        if idx >= 0:
            messages = messages[idx + 1 :]
        else:
            note = "after_message_id is not among the latest 200 messages; returned the full list instead."
        if len(messages) > limit:
            messages = messages[-limit:]
    else:
        messages = fetch_messages(conn, session_id, limit=limit)
    messages = sorted(
        messages, key=lambda m: (m.get("time") or {}).get("created") or 0
    )
    formatted = [_format_message(m) for m in messages]
    result = {
        "server": conn.name,
        "session_id": session_id,
        "count": len(formatted),
        "messages": formatted,
        "last_message_id": messages[-1].get("id") if messages else None,
    }
    if note:
        result["note"] = note
    return result


def tool_permission_reply(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    request_id = args.get("request_id")
    decision = args.get("decision")
    if not session_id or not request_id:
        raise OpenCodeError("Missing required parameter session_id / request_id")
    if decision not in ("once", "always", "reject"):
        raise OpenCodeError(
            "decision must be once / always / reject, got: %r" % decision
        )
    body = {"decision": decision}
    if args.get("message"):
        body["message"] = args["message"]
    http_request(
        conn,
        "POST",
        "/api/session/%s/permission/%s/reply"
        % (
            urllib.parse.quote(session_id, safe=""),
            urllib.parse.quote(request_id, safe=""),
        ),
        body=body,
    )
    return {"ok": True, "server": conn.name, "session_id": session_id, "request_id": request_id, "decision": decision}


def tool_form_reply(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    form_id = args.get("form_id")
    answer = args.get("answer")
    if not session_id or not form_id:
        raise OpenCodeError("Missing required parameter session_id / form_id")
    if not isinstance(answer, dict):
        raise OpenCodeError("answer must be an object, e.g. {\"fieldKey\": value}")
    http_request(
        conn,
        "POST",
        "/api/session/%s/form/%s/reply"
        % (
            urllib.parse.quote(session_id, safe=""),
            urllib.parse.quote(form_id, safe=""),
        ),
        body={"answer": answer},
    )
    return {"ok": True, "server": conn.name, "session_id": session_id, "form_id": form_id, "answer": answer}


def tool_interrupt(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    http_request(
        conn,
        "POST",
        "/api/session/%s/interrupt" % urllib.parse.quote(session_id, safe=""),
    )
    return {"ok": True, "server": conn.name, "session_id": session_id}


def tool_list_agents(args):
    conn = _resolve_connection(args)
    data = unwrap(http_request(conn, "GET", "/api/agent"))
    if not isinstance(data, list):
        data = []
    agents = []
    for agent in data:
        if not isinstance(agent, dict):
            continue
        agents.append(
            {
                "name": agent.get("name"),
                "mode": agent.get("mode"),
                "model": agent.get("model"),
            }
        )
    return {
        "server": conn.name,
        "count": len(agents),
        "agents": agents,
        "note": "model being null means the agent has no explicitly configured model (it falls back to the position default model at runtime). "
        "If a session should use a particular agent's model, the caller passes providerID/modelID to create_session's model_id.",
    }


def tool_pending_interactions(args):
    """Pending human interactions for the session and its whole subtree (consistent with wait_session)."""
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    _route_session(session_id, conn)
    snap = _subtree_snapshot(conn, session_id)
    if snap["verified"]:
        permissions = snap["permissions"]
        forms = [_form_summary(f) for f in _enrich_forms(conn, snap["forms"])]
    else:
        # Structure could not be verified: fall back to the root session's own view (legacy shape).
        permissions = fetch_permissions(conn, session_id)
        forms = [
            _form_summary(f)
            for f in fetch_forms(conn, session_id, pending_only=True)
        ]
    result = {
        "server": conn.name,
        "session_id": session_id,
        "root_session_id": session_id,
        "permissions": permissions,
        "forms": forms,
    }
    _attach_subtree(result, snap)
    return result


def _session_time(raw):
    """Get updated / idle from the time field of Session.Info."""
    info = raw.get("time") or {}
    result = {"updated": info.get("updated")}
    if "idle" in info:
        result["idle"] = info.get("idle")
    return result


def _limit_param(args, default):
    """Parse a caller-supplied `limit` argument: missing/None/"" -> default; non-numeric ->
    the default (a malformed argument is a client-side mistake, not a server failure -- do
    not surface it as an unclassified "Tool execution failed"); numeric -> clamped to 1..500."""
    value = args.get("limit")
    if value is None or value == "":
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 500))


def tool_list_sessions(args):
    conn = _resolve_connection(args)
    query = {
        "search": args.get("search"),
        "limit": _limit_param(args, 20),
        "order": args.get("order", "desc"),
        "directory": args.get("directory"),
        "cursor": args.get("cursor"),
    }
    payload = http_request(conn, "GET", "/api/session", query=query)
    data = unwrap(payload)
    sessions = []
    cursor = {}
    if isinstance(data, list):
        sessions = data
    elif isinstance(data, dict):
        sessions = data.get("sessions") or data.get("data") or []
        cursor = data.get("cursor") or {}
    out = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "agent": s.get("agent"),
                "model": s.get("model"),
                "parentID": s.get("parentID"),
                "time": _session_time(s),
            }
        )
    return {
        "server": conn.name,
        "count": len(out),
        "sessions": out,
        "cursor": {
            "previous": cursor.get("previous"),
            "next": cursor.get("next"),
        },
    }


def tool_compact(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    timeout_secs = _arg_timeout(args)
    auto_permission = args.get("auto_permission") or conn.default_auto_permission()

    baseline = set(m.get("id") for m in fetch_messages(conn, session_id))
    compact_payload = http_request(
        conn,
        "POST",
        "/api/session/%s/compact" % urllib.parse.quote(session_id, safe=""),
        body={},
    )
    gate_id = (
        compact_payload.get("data", {}).get("id")
        if isinstance(compact_payload, dict)
        else None
    )
    _route_session(session_id, conn)
    status, payload = _run_until_terminal(
        conn,
        session_id,
        timeout_secs,
        auto_permission,
        gate_message_id=gate_id,
        gate_is_compaction=True,
        baseline=baseline,
        with_result=True,
    )
    return payload


def tool_get_context(args):
    conn = _resolve_connection(args)
    session_id = args.get("session_id")
    if not session_id:
        raise OpenCodeError("Missing required parameter session_id")
    data = unwrap(
        http_request(
            conn,
            "GET",
            "/api/session/%s" % urllib.parse.quote(session_id, safe=""),
        )
    )
    if not isinstance(data, dict):
        data = {}
    return {
        "server": conn.name,
        "id": data.get("id"),
        "title": data.get("title"),
        "agent": data.get("agent"),
        "model": data.get("model"),
        "parentID": data.get("parentID"),
        "tokens": data.get("tokens"),
        "cost": data.get("cost"),
        "time": {
            "updated": (data.get("time") or {}).get("updated"),
            "idle": (data.get("time") or {}).get("idle"),
        },
        "revert": data.get("revert"),
    }

def tool_connect_server(args):
    """Register and validate a remote connection (creation-time hard gate). Valid only within this process; not persisted."""
    name = args.get("name")
    url = args.get("url")
    if not name or not url:
        raise OpenCodeError("Missing required parameter name / url")

    password = None
    source = None
    # Credential sources: for an LLM-registered (dynamic) connection, only a plaintext password
    # passed in the call itself is accepted. password_file / password_env are an LLM-directed
    # read of arbitrary host files / environment variables, and the value is exfiltrated over
    # the network in the Authorization header of the first request to the caller-chosen URL --
    # a one-call credential-exfiltration primitive if a prompt injection (or a malicious
    # client) reaches the tool. Human operators who need file/env credentials for a local
    # server use OPENCODE_URL / OPENCODE_PASSWORD in the process environment instead.
    if args.get("password_file"):
        raise OpenCodeError(
            "password_file is not accepted for dynamic remote connections: it would let the "
            "MCP caller read any host file and exfiltrate its contents over the network. "
            "Pass the password in the call (or use the OPENCODE_URL/OPENCODE_PASSWORD "
            "environment for a server the operator runs)"
        )
    if args.get("password_env"):
        raise OpenCodeError(
            "password_env is not accepted for dynamic remote connections: it would let the "
            "MCP caller read any environment variable and exfiltrate its contents over the "
            "network. Pass the password in the call (or use the OPENCODE_URL/OPENCODE_PASSWORD "
            "environment for a server the operator runs)"
        )
    if args.get("password"):
        password = args["password"]
        source = "plaintext"

    conn = _register_connection(name, url, password, dynamic=True)
    result = conn.describe()
    result["password_source"] = source or "none"
    if source == "plaintext":
        result["note"] = (
            "A plaintext password was registered in this process (it also appears in the "
            "MCP request stream). Prefer short-lived or per-connection passwords for "
            "dynamically connected servers."
        )
    warning = _version_warning_for(conn)
    if warning:
        result["api_version_warning"] = warning
    return result


def tool_list_servers(args):
    _local_connection()  # Ensure the local connection is ready (spawn or direct connect)
    with _STATE_LOCK:
        conns = list(_CONNECTIONS.values())
    return {
        "count": len(conns),
        "servers": [c.describe() for c in conns],
    }


def tool_disconnect_server(args):
    name = args.get("name")
    if not name:
        raise OpenCodeError("Missing required parameter name")
    if name == DEFAULT_LOCAL_NAME:
        raise OpenCodeError("The local connection cannot be removed")
    conn = _remove_connection(name)
    if conn is None:
        raise OpenCodeError("Connection does not exist: %s (see list_servers)" % name)
    if conn.spawned_proc is not None:
        try:
            conn.spawned_proc.kill()
        except Exception:
            pass
    return {"ok": True, "removed": name}




# ---------------------------------------------------------------------------
# Tool catalog (schema + descriptions)
# ---------------------------------------------------------------------------

# Shared parameter schemas (read-only reuse; for serialization, never mutated)
_SHARED_SERVER_PARAM = {
    "type": "string",
    "description": "(optional) target connection name, defaults to local; a call with a session_id is auto-routed to the connection that created it",
}
_SHARED_SESSION_ID_PARAM = {"type": "string", "description": "Session ID (ses_...)"}
_SHARED_TIMEOUT_PARAM = {
    "type": "integer",
    "description": "Maximum wait in seconds, default 120",
    "default": 120,
}


TOOLS = [
    {
        "name": "create_session",
        "description": (
            "Create a new conversation session on the local opencode. Returns session_id (ses_...), "
            "which later tools such as chat / get_messages use. Optionally specify a title, agent, model, "
            "and location (to create the session at a given directory/project location)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "title": {"type": "string", "description": "Session title (optional)"},
                "agent": {"type": "string", "description": "Name of the agent to use (optional)"},
                "model_id": {
                    "type": "string",
                    "description": "Model in providerID/modelID format, e.g. \"anthropic/claude-sonnet-4\" (optional)",
                },
                "location": {
                    "type": "object",
                    "description": "Session location (optional), used to create the session in a given directory/project. Passed through in the opencode Location.PublicRef shape.",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Absolute path of the working directory (required)",
                        }
                    },
                    "required": ["directory"],
                    "additionalProperties": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "chat",
        "description": (
            "Send a prompt to the given session and wait for opencode to reply. Internally polls messages, "
            "permission requests and form requests. With auto_permission=once/always/reject it answers permission requests automatically; "
            "with manual it returns immediately on a permission request, and you must call permission_reply + wait_session again. "
            "On a form request it returns needs_form, and you must call form_reply + wait_session. "
            "Optional delivery: steer=steer directly while running (interrupts the current generation direction), "
            "queue=queue it to take effect after this round ends; if omitted the field is not sent. "
            "Optional files: an array of files attached to the prompt, each {uri (required), name?, description?}. "
            "Returns status: succeeded (success, with assistant_text/tools_used/reasoning), "
            "failed (failure), interrupted (interrupted), "
            "needs_permission (waiting for authorization, with a requests list), needs_form (waiting for form input), "
            "timeout (timed out, with partial_text and diagnostics). "
            "Terminal payloads also report subagent state (subagents / pending_subagents / subtree_truncated / subtree_verified)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "text": {"type": "string", "description": "Prompt text to send"},
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
                "wait_for_subagents": {
                    "type": "boolean",
                    "description": "When true, a succeeded status additionally requires the whole subagent subtree to be quiescent (no active child, no pending permission/form) and auto_permission answers pending permissions of every subtree node. Default false (report subagent state without gating).",
                    "default": False,
                },
                "auto_permission": {
                    "type": "string",
                    "enum": ["once", "always", "reject", "manual"],
                    "description": "How to handle permission requests. Local connections default to once (allow this time); remote connections default to manual (approval must stay with the caller). You may explicitly set once/always/reject/manual.",
                    "default": "once",
                },
                "delivery": {
                    "type": "string",
                    "enum": ["steer", "queue"],
                    "description": "Delivery mode (optional). steer=steer directly while running (interrupts the current generation direction), queue=queue it to take effect after this round ends; if omitted the field is not sent.",
                },
                "files": {
                    "type": "array",
                    "description": "Array of files to send with the prompt (optional).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string", "description": "File URI (required)"},
                            "name": {"type": "string", "description": "File name (optional)"},
                            "description": {"type": "string", "description": "File description (optional)"},
                        },
                        "required": ["uri"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["session_id", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_session",
        "description": (
            "Wait for the given session to reach a terminal or needs-interaction state (a pure state primitive; returns no message content). "
            "Terminal status: succeeded (this round ended successfully) / failed (failure) / interrupted (interrupted), "
            "taken from the authoritative session field outcome; blocking states: needs_permission / needs_form (call again after replying); "
            "timeout means it was still generating when the wait timed out. Returns last_message_id as the get_messages incremental cursor, "
            "to be used with get_messages(after_message_id=...) to fetch new replies. "
            "By default a succeeded status is only returned once the whole subagent subtree is quiescent; the payload reports subagents / pending_subagents / subtree_truncated / subtree_verified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
                "wait_for_subagents": {
                    "type": "boolean",
                    "description": "When true (default), succeeded additionally requires the whole subagent subtree to be quiescent (no active child session, no pending permission/form anywhere in the subtree); when false, the legacy behaviour is used but subagent state is still reported.",
                    "default": True,
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_messages",
        "description": (
            "Fetch session message records in ascending time order, formatting user/assistant text, tool-call summaries and timestamps. "
            "Supports incremental fetch: pass after_message_id (the last_message_id returned previously, or any message id), "
            "and only messages after it are returned; the response includes last_message_id for the next cursor. "
            "Typical combination: wait_session until terminal, then use this to fetch new replies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return, default 50",
                    "default": 50,
                },
                "after_message_id": {
                    "type": "string",
                    "description": "Incremental cursor (optional): only return new messages after this one",
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "permission_reply",
        "description": (
            "Answer a permission request. decision=once allows this time only, always allows always and saves it, "
            "reject denies it. Optional message is an attached note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "request_id": {"type": "string", "description": "Permission request ID (per_...)"},
                "decision": {
                    "type": "string",
                    "enum": ["once", "always", "reject"],
                    "description": "Authorization decision",
                },
                "message": {"type": "string", "description": "Optional explanatory message"},
            },
            "required": ["session_id", "request_id", "decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "form_reply",
        "description": (
            "Submit the answer for a form. answer is an object whose keys are field keys; values may be "
            "string / number / boolean / string[]."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "form_id": {"type": "string", "description": "Form ID (frm_...)"},
                "answer": {
                    "type": "object",
                    "description": "Answer object, e.g. {\"name\": \"foo\", \"count\": 3}",
                    "additionalProperties": True,
                },
            },
            "required": ["session_id", "form_id", "answer"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_agents",
        "description": (
            "List all agents of the local opencode and their resolved default models (read-only). "
            "model being null means it is not explicitly configured (it falls back to the position default model). "
            "If a session should match a particular agent's model, the caller passes that model in providerID/modelID "
            "format to create_session's model_id; this tool only provides information and does not pin the model for the caller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "interrupt",
        "description": "Interrupt the generation currently in progress in the given session. Useful to cancel a long-running task after chat returns timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pending_interactions",
        "description": (
            "Query the human interactions currently pending in the given session, returning lists of permissions (permission requests) and forms. "
            "Use it to learn, without blocking, whether the session is waiting for authorization or form input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_sessions",
        "description": (
            "Enumerate / search existing sessions; supports keywords, ordering, directory filtering and cursor pagination. "
            "Useful for finding past topics; once you have a session_id, use it with chat to resume the previous conversation (the session_id is the resume handle)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "search": {"type": "string", "description": "Keyword to search by title/content (optional)"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results, default 20",
                    "default": 20,
                },
                "order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "description": "Order by update time, default desc (newest first)",
                    "default": "desc",
                },
                "directory": {"type": "string", "description": "Filter by working directory (optional)"},
                "cursor": {"type": "string", "description": "Pagination cursor, taken from the cursor.next returned previously (optional)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "compact",
        "description": (
            "Compact the context of the given session, wait for the compaction to finish and return the result. "
            "Returns status: succeeded (compaction complete), compaction_failed (compaction failed), "
            "timeout (timed out). Useful to proactively trim when the context nears its limit; you can keep chatting afterwards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM,
                "timeout_secs": _SHARED_TIMEOUT_PARAM,
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_context",
        "description": (
            "View the context usage (tokens / cost) and metadata of the given session, "
            "to be used with compact to decide whether compaction is needed. tokens / cost default to null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": _SHARED_SESSION_ID_PARAM
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_session",
        "description": (
            "Delete the given session. Warning: this operation is irreversible and cascades to all of its child sessions "
            "(observed: after deleting the parent, accessing a child returns 404). Confirm these sessions are no longer needed before deleting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": _SHARED_SERVER_PARAM,
                "session_id": {"type": "string", "description": "ID of the session to delete (ses_...)"}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "connect_server",
        "description": (
            "Register and validate a remote opencode connection (valid only within this process; not persisted). "
            "Connecting performs the creation-time check: unreachable = availability error; no version = compatibility error; a version differing from the baseline returns a warning. "
            "The url must be a public http(s) endpoint: private / loopback / link-local / reserved addresses and non-http(s) "
            "schemes are rejected (use the OPENCODE_URL / OPENCODE_PASSWORD environment variables to point this MCP at a "
            "server the operator runs). "
            "Credentials: only a plaintext `password` passed in this call is accepted (it is sent in the Authorization "
            "header to the url and also remains in the MCP request stream; prefer short-lived or per-connection passwords). "
            "password_file / password_env are not supported for dynamic connections. "
            "When no password is given, no Authorization is sent (some remotes use an empty username/password). "
            "Returns {name, url, version, baseline, baseline_check}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Connection alias (handle); cannot be local"},
                "url": {
                    "type": "string",
                    "description": "Public http(s) endpoint, e.g. https://host:4096. Private / loopback / link-local / reserved hosts and other schemes are rejected; for a locally run server use the OPENCODE_URL / OPENCODE_PASSWORD environment variables instead.",
                },
                "password": {"type": "string", "description": "(optional) plaintext password; it is sent to the url and also remains in the MCP request stream — prefer short-lived or per-connection passwords"},
            },
            "required": ["name", "url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_servers",
        "description": (
            "List all current connections (local + dynamic remotes): name, address, source, version, and baseline check status. "
            "Ensures the local connection is ready (spawning the local serve if necessary)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "disconnect_server",
        "description": "Remove a dynamically registered remote connection (local cannot be removed). Its session routing is cleared as well.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Connection alias"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]




HANDLERS = {
    "create_session": tool_create_session,
    "chat": tool_chat,
    "wait_session": tool_wait_session,
    "get_messages": tool_get_messages,
    "permission_reply": tool_permission_reply,
    "form_reply": tool_form_reply,
    "list_agents": tool_list_agents,
    "interrupt": tool_interrupt,
    "pending_interactions": tool_pending_interactions,
    "list_sessions": tool_list_sessions,
    "compact": tool_compact,
    "get_context": tool_get_context,
    "delete_session": tool_delete_session,
    "connect_server": tool_connect_server,
    "list_servers": tool_list_servers,
    "disconnect_server": tool_disconnect_server,
}


# ---------------------------------------------------------------------------
# MCP JSON-RPC handling
# ---------------------------------------------------------------------------

def _tool_result(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(message):
    return {
        "content": [{"type": "text", "text": str(message)}],
        "isError": True,
    }


def handle_message(message):
    """Handle a single JSON-RPC message; returns a response dict or None (notifications need no response)."""
    if not isinstance(message, dict):
        return None

    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        requested = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        handler = HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_error("Unknown tool: %s" % name),
            }
        try:
            result = handler(arguments)
            result = _warn_choke(arguments, result)
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_result(result)}
        except OpenCodeError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_error(str(exc))}
        except Exception as exc:  # Any exception becomes a tool error so the server never crashes
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_error("Tool execution failed (%s): %s" % (name, exc)),
            }

    # Notifications (no id) are always ignored
    if msg_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": "Method not found: %s" % method},
    }


def write_message(response):
    with _OUT_LOCK:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _handle_request(message):
    """Handle a single request in a worker thread; cancelled requests are no longer written back."""
    msg_id = message.get("id")
    _CURRENT.request_id = msg_id
    try:
        try:
            response = handle_message(message)
        except Exception as exc:  # Catch-all so no exception terminates the process
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": "Internal error: %s" % exc},
            }
        if response is None:
            return
        if _request_cancelled(msg_id):
            log("[opencode-mcp] request cancelled, discarding response:", msg_id)
            return
        write_message(response)
    finally:
        with _STATE_LOCK:
            _CANCELLED.discard(msg_id)
        _CURRENT.request_id = None


def main():
    _install_signal_handlers()  # Own the spawned serve's lifetime on host-kill paths too
    try:
        workers = int(os.environ.get("OPENCODE_MCP_WORKERS") or "4")
    except ValueError:
        workers = 4
    workers = max(1, min(workers, MAX_WORKERS))
    log(
        "[opencode-mcp] started, workers=%d, waiting for JSON-RPC on stdin" % workers
    )
    executor = ThreadPoolExecutor(max_workers=workers)
    # The reader thread only parses and dispatches, so cancellation notifications arrive immediately.
    # Read from sys.stdin.buffer so the line cap counts BYTES (text mode would count characters,
    # letting a multibyte line reach several MiB in bytes before the cap triggers).
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        if len(raw) > MAX_STDIN_LINE:
            log(
                "[opencode-mcp] stdin line exceeds %d bytes; dropping it" % MAX_STDIN_LINE
            )
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception as exc:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: %s" % exc},
                }
            )
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        msg_id = message.get("id")

        # Cancellation notification: handled immediately in the reader thread, not sent to the thread pool
        if method == "notifications/cancelled":
            params = message.get("params")
            cancelled_id = params.get("requestId") if isinstance(params, dict) else None
            if cancelled_id is not None:
                with _STATE_LOCK:
                    _CANCELLED.add(cancelled_id)
                log("[opencode-mcp] received cancel request:", cancelled_id)
            continue

        # All other notifications (no id) are ignored
        if msg_id is None:
            continue

        executor.submit(_handle_request, message)


if __name__ == "__main__":
    main()
