"""Minimal MCP client over Streamable HTTP (stdlib only).

Enough of the Model Context Protocol for GenFill: ``initialize``, ``tools/list`` and
``tools/call`` against a remote server, with JSON or SSE responses, session ids, and an
auth hook that can refresh or sign in again when the server answers 401.
"""
from __future__ import annotations

import itertools
import json
import socket

try:
    from urllib import request as _urlrequest
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover
    raise

from .base import USER_AGENT, BackendError, Cancelled, _error_message, _host

PROTOCOL_VERSION = "2025-06-18"


class ApiKeyAuth(object):
    """Sends a fixed header, e.g. ``X-API-Key: comfyui-...``."""

    def __init__(self, header, key, label="the server"):
        self.header = header
        self.key = key
        self.label = label

    def headers(self, ctx):
        return {self.header: self.key}

    def unauthorized(self, challenge, ctx):
        raise BackendError("%s rejected the API key. Check it in API Keys & Accounts..."
                           % self.label)


class OAuthAuth(object):
    """Bearer token from an :class:`oauth.OAuthProvider`."""

    def __init__(self, provider):
        self.provider = provider

    def headers(self, ctx):
        token = self.provider.access_token(interactive=ctx.get("interactive", False),
                                           progress=ctx.get("progress"),
                                           cancelled=ctx.get("cancelled"))
        return {"Authorization": "Bearer %s" % token}

    def unauthorized(self, challenge, ctx):
        self.provider.invalidate()
        if challenge:
            self.provider.discover(challenge)
        return True


def parse_sse(lines):
    """Yield JSON messages from SSE ``lines`` (bytes, with or without line endings)."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                text = "\n".join(data)
                data = []
                try:
                    yield json.loads(text)
                except ValueError:
                    pass
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
    if data:
        try:
            yield json.loads("\n".join(data))
        except ValueError:
            pass


def tool_result(result, tool=""):
    """Decode a ``tools/call`` result: structured content, else JSON text, else text."""
    result = result or {}
    texts = [c.get("text", "") for c in result.get("content") or []
             if isinstance(c, dict) and c.get("type") == "text"]
    if result.get("isError"):
        msg = " ".join(t.strip() for t in texts if t.strip()) or "unknown error"
        raise BackendError("%s failed: %s" % (tool or "Tool", msg[:600]))
    sc = result.get("structuredContent")
    if sc is not None:
        return sc
    for t in texts:
        try:
            return json.loads(t)
        except ValueError:
            continue
    return "\n".join(texts)


class McpClient(object):
    def __init__(self, url, auth=None, label=None, interactive=True, progress=None,
                 cancelled=None, timeout=120):
        self.url = url
        self.auth = auth
        self.label = label or _host(url)
        self.ctx = {"interactive": interactive, "progress": progress, "cancelled": cancelled}
        self.timeout = timeout
        self.session_id = None
        self.protocol_version = None
        self.server_info = {}
        self._ids = itertools.count(1)
        self._ready = False

    # ---- public -------------------------------------------------------------
    def connect(self):
        if self._ready:
            return self
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "GenFill", "version": _version()},
        })
        self.protocol_version = (result or {}).get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = (result or {}).get("serverInfo") or {}
        self._rpc("notifications/initialized", None, notify=True)
        self._ready = True
        return self

    def list_tools(self):
        self.connect()
        tools, cursor = [], None
        while True:
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name, arguments=None, timeout=None):
        """Call a tool and return its decoded result (see :func:`tool_result`)."""
        self.connect()
        self._check_cancel()
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}},
                        timeout=timeout)
        return tool_result(res, name)

    # ---- transport ----------------------------------------------------------
    def _check_cancel(self):
        cancelled = self.ctx.get("cancelled")
        if cancelled and cancelled():
            raise Cancelled("Cancelled")

    def _headers(self):
        h = {"User-Agent": USER_AGENT,
             "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        if self.auth is not None:
            h.update(self.auth.headers(self.ctx))
        return h

    def _rpc(self, method, params, notify=False, timeout=None, _retry=True):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        msg_id = None
        if not notify:
            msg_id = next(self._ids)
            msg["id"] = msg_id
        body = json.dumps(msg).encode("utf-8")
        req = _urlrequest.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            resp = _urlrequest.urlopen(req, timeout=timeout or self.timeout)
        except HTTPError as exc:
            data = exc.read() if hasattr(exc, "read") else b""
            hdrs = exc.headers or {}
            if exc.code == 401 and self.auth is not None and _retry:
                challenge = hdrs.get("WWW-Authenticate")
                if self.auth.unauthorized(challenge, self.ctx):
                    return self._rpc(method, params, notify, timeout, _retry=False)
            if exc.code == 401:
                raise BackendError("%s: not authorized. Sign in again with API Keys & "
                                   "Accounts..." % self.label)
            if exc.code == 404 and self.session_id and method != "initialize" and _retry:
                # Session expired on the server: start a new one and retry once.
                self.session_id, self._ready = None, False
                self.connect()
                return self._rpc(method, params, notify, timeout, _retry=False)
            if exc.code == 402:
                raise BackendError("%s: out of credits. %s"
                                   % (self.label, _error_message(exc.code, data)))
            if exc.code == 429:
                raise BackendError("%s: rate limited. %s"
                                   % (self.label, _error_message(exc.code, data)))
            raise BackendError("%s: %s" % (self.label, _error_message(exc.code, data)))
        except (URLError, socket.timeout, OSError) as exc:
            raise BackendError("Network error talking to %s: %s" % (self.label, exc))
        try:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            if notify:
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                reply = None
                for message in parse_sse(iter(resp.readline, b"")):
                    if isinstance(message, dict) and message.get("id") == msg_id and (
                            "result" in message or "error" in message):
                        reply = message
                        break
            else:
                raw = resp.read()
                reply = json.loads(raw.decode("utf-8")) if raw else None
                if isinstance(reply, list):
                    reply = next((m for m in reply if m.get("id") == msg_id), None)
        except (ValueError, socket.timeout, OSError) as exc:
            raise BackendError("%s sent an unreadable answer: %s" % (self.label, exc))
        finally:
            resp.close()
        if not isinstance(reply, dict):
            raise BackendError("%s sent no answer to %s." % (self.label, method))
        if "error" in reply:
            err = reply["error"] or {}
            raise BackendError("%s: %s" % (self.label, err.get("message") or err))
        return reply.get("result")


def _version():
    try:
        from .. import __version__
        return __version__
    except Exception:  # pragma: no cover
        return "0"
