"""A local fake MCP server with OAuth (discovery, DCR, PKCE, refresh) for tests."""
import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit


class FakeMcpServer(object):
    """Serves ``/mcp`` plus the OAuth endpoints on 127.0.0.1.

    ``tools`` maps a tool name to ``fn(arguments) -> result dict`` (an MCP tools/call result).
    ``sse=True`` answers requests as text/event-stream.
    ``api_key`` (if set) is accepted in ``X-API-Key`` instead of a bearer token.
    """

    def __init__(self, tools=None, sse=False, api_key=None, token_ttl=3600):
        self.tools = dict(tools or {})
        self.sse = sse
        self.api_key = api_key
        self.token_ttl = token_ttl
        self.clients = {}
        self.codes = {}
        self.access_tokens = set()
        self.refresh_tokens = set()
        self.calls = []          # (tool name, arguments)
        self.requests = []       # (method, path, headers)
        self.sessions = set()
        self.registrations = 0
        self.token_requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.url = self.base + "/mcp"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def browser(self, url):
        """Stands in for webbrowser.open: 'approves' and follows the redirect to the app."""
        import urllib.request
        threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(),
                         daemon=True).start()
        return True


def _make_handler(srv):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body=b"", ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            p = urlsplit(self.path)
            srv.requests.append(("GET", p.path, dict(self.headers)))
            if p.path == "/.well-known/oauth-protected-resource/mcp":
                return self._send(200, {"resource": srv.url,
                                        "authorization_servers": [srv.base + "/auth"],
                                        "scopes_supported": ["mcp"]})
            if p.path == "/.well-known/oauth-authorization-server/auth":
                return self._send(200, {
                    "issuer": srv.base + "/auth",
                    "authorization_endpoint": srv.base + "/auth/authorize",
                    "token_endpoint": srv.base + "/auth/token",
                    "registration_endpoint": srv.base + "/auth/register",
                    "scopes_supported": ["mcp", "offline_access"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"]})
            if p.path == "/auth/authorize":
                q = {k: v[0] for k, v in parse_qs(p.query).items()}
                client = srv.clients.get(q.get("client_id"))
                if not client or q.get("redirect_uri") not in client["redirect_uris"]:
                    return self._send(400, {"error": "invalid_request"})
                code = "code-%d" % len(srv.codes)
                srv.codes[code] = q
                loc = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]})
                return self._send(302, b"", headers={"Location": loc})
            if p.path == "/files/result.png":
                return self._send(200, b"\x89PNG\r\n\x1a\nfake", ctype="image/png")
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            p = urlsplit(self.path)
            body = self._body()
            srv.requests.append(("POST", p.path, dict(self.headers)))
            if p.path == "/auth/register":
                data = json.loads(body)
                srv.registrations += 1
                cid = "client-%d" % srv.registrations
                srv.clients[cid] = data
                return self._send(201, dict(data, client_id=cid))
            if p.path == "/auth/token":
                form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                srv.token_requests.append(form)
                if form.get("grant_type") == "authorization_code":
                    q = srv.codes.pop(form.get("code"), None)
                    if not q:
                        return self._send(400, {"error": "invalid_grant"})
                    digest = base64.urlsafe_b64encode(hashlib.sha256(
                        form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                    if digest != q["code_challenge"] or form.get("resource") != srv.url:
                        return self._send(400, {"error": "invalid_grant"})
                elif form.get("grant_type") == "refresh_token":
                    if form.get("refresh_token") not in srv.refresh_tokens:
                        return self._send(400, {"error": "invalid_grant"})
                else:
                    return self._send(400, {"error": "unsupported_grant_type"})
                n = len(srv.token_requests)
                at, rt = "at-%d" % n, "rt-%d" % n
                srv.access_tokens.add(at)
                srv.refresh_tokens.add(rt)
                return self._send(200, {"access_token": at, "refresh_token": rt,
                                        "token_type": "Bearer", "expires_in": srv.token_ttl})
            if p.path == "/mcp":
                return self._mcp(body)
            return self._send(404, {"error": "not found"})

        def _authorized(self):
            if srv.api_key and self.headers.get("X-API-Key") == srv.api_key:
                return True
            auth = self.headers.get("Authorization") or ""
            return auth.startswith("Bearer ") and auth[7:] in srv.access_tokens

        def _mcp(self, body):
            if not self._authorized():
                meta = srv.base + "/.well-known/oauth-protected-resource/mcp"
                return self._send(401, {"error": "unauthorized"}, headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="%s"' % meta})
            msg = json.loads(body)
            method = msg.get("method")
            headers = {}
            if method == "initialize":
                sid = "s%d" % (len(srv.sessions) + 1)
                srv.sessions.add(sid)
                headers["Mcp-Session-Id"] = sid
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake", "version": "1"}}
            else:
                if self.headers.get("Mcp-Session-Id") not in srv.sessions:
                    return self._send(404, {"error": "no session"})
                if "id" not in msg:
                    return self._send(202, b"")
                if method == "tools/list":
                    result = {"tools": [{"name": n} for n in srv.tools]}
                elif method == "tools/call":
                    name = msg["params"]["name"]
                    args = msg["params"].get("arguments") or {}
                    srv.calls.append((name, args))
                    if name not in srv.tools:
                        return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "error": {
                            "code": -32602, "message": "Unknown tool %s" % name}})
                    result = srv.tools[name](args)
                else:
                    result = {}
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
            if srv.sse:
                note = {"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progress": 1}}
                text = ("event: message\ndata: %s\n\n: keepalive\n\ndata: %s\n\n"
                        % (json.dumps(note), json.dumps(reply)))
                return self._send(200, text.encode(), ctype="text/event-stream",
                                  headers=headers)
            return self._send(200, reply, headers=headers)

    return H


def text_result(obj):
    """An MCP tools/call result carrying ``obj`` as JSON text."""
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


class FakeComfyServer(object):
    """A minimal ComfyUI: upload, prompt queue, history, view, queue, interrupt, system_stats.

    ``fail_prompt``: dict answered with HTTP 400 on ``POST /prompt``.
    ``exec_error``: ``True`` (every prompt) or a set of prompt numbers that end in an error.
    ``hold``: history stays empty (the job never finishes).
    ``polls_before_done``: empty ``/history`` answers per prompt before it completes.
    ``requests`` holds ``(method, path, headers, body)``; ``prompts`` the queued payloads.
    """

    def __init__(self, fail_prompt=None, exec_error=False, hold=False, polls_before_done=1):
        self.fail_prompt = fail_prompt
        self.exec_error = exec_error
        self.hold = hold
        self.polls_before_done = polls_before_done
        self.requests = []
        self.prompts = []        # parsed POST /prompt bodies
        self.uploads = []        # (field names, filename)
        self.polls = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def find(self, method, path):
        return [r for r in self.requests if r[0] == method and r[1] == path]

    def _failed(self, number):
        return self.exec_error is True or (bool(self.exec_error) and number in self.exec_error)

    def _history(self, pid):
        number = int(pid[1:])
        self.polls[pid] = self.polls.get(pid, 0) + 1
        if self.hold or self.polls[pid] <= self.polls_before_done:
            return {}
        graph = self.prompts[number - 1]["prompt"]
        out = next((nid for nid, n in graph.items()
                    if (n.get("_meta") or {}).get("title") == "GenFill Output"), "5")
        if self._failed(number):
            return {pid: {"outputs": {}, "status": {"status_str": "error", "completed": False,
                                                    "messages": [["execution_error", {
                                                        "node_type": "KSampler",
                                                        "exception_message": "out of memory"}]]}}}
        return {pid: {"status": {"status_str": "success", "completed": True, "messages": []},
                      "outputs": {out: {"images": [{"filename": "r%d.png" % number,
                                                    "subfolder": "", "type": "output"}]}}}}

    def _handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                p = urlsplit(self.path)
                srv.requests.append(("GET", p.path, dict(self.headers), b""))
                if p.path.startswith("/history/"):
                    return self._send(200, srv._history(p.path.rsplit("/", 1)[1]))
                if p.path == "/view":
                    from genfill import pngio
                    img = pngio.Image(4, 4, 3)
                    for row in img.rows:
                        row[:] = bytes(bytearray([200] * 12))
                    return self._send(200, pngio.encode(img), "image/png")
                if p.path == "/queue":
                    ids = ["p%d" % (i + 1) for i in range(len(srv.prompts))]
                    entries = [[i, pid, {}, {}, []] for i, pid in enumerate(ids)]
                    return self._send(200, {"queue_running": entries[:1],
                                            "queue_pending": entries[1:]})
                if p.path == "/system_stats":
                    return self._send(200, {"system": {"comfyui_version": "0.3.99"},
                                            "devices": [{"name": "cuda:0 Fake GPU"}]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                p = urlsplit(self.path)
                body = self._body()
                srv.requests.append(("POST", p.path, dict(self.headers), body))
                if p.path == "/upload/image":
                    text = body.decode("latin-1")
                    fields = re.findall(r'name="([^"]+)"', text)
                    fname = re.search(r'filename="([^"]+)"', text).group(1)
                    srv.uploads.append((fields, fname))
                    return self._send(200, {"name": fname, "subfolder": "genfill",
                                            "type": "input"})
                if p.path == "/prompt":
                    if srv.fail_prompt is not None:
                        return self._send(400, srv.fail_prompt)
                    srv.prompts.append(json.loads(body.decode("utf-8")))
                    n = len(srv.prompts)
                    return self._send(200, {"prompt_id": "p%d" % n, "number": n,
                                            "node_errors": {}})
                if p.path in ("/queue", "/interrupt"):
                    return self._send(200, {})
                self._send(404, {"error": "not found"})

        return H
