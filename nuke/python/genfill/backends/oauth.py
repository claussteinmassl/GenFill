"""OAuth 2.1 sign-in for remote MCP servers (stdlib only).

Follows the MCP authorization spec:

1. Discovery: the server's 401 names its protected-resource metadata (RFC 9728), which names
   the authorization server; its metadata (RFC 8414 / OpenID discovery) gives the endpoints.
2. Dynamic client registration (RFC 7591) as a public client with a loopback redirect
   (``http://127.0.0.1:<port>/callback``, RFC 8252).
3. Authorization code + PKCE S256 in the artist's browser; a one-shot local HTTP server
   receives the code.
4. Tokens (and the registration) are kept in ``~/.nuke/genfill/config.json`` and refreshed
   silently, so the browser only opens the first time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import webbrowser

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlencode, urlsplit
except ImportError:  # pragma: no cover
    raise

from .base import BackendError, Cancelled, header, http_request
from .. import config

CLIENT_NAME = "GenFill for Nuke"
CLIENT_URI = "https://github.com/claussteinmassl/GenFill"
# Loopback ports tried for the redirect. A registration remembers its port, because some
# servers compare redirect URIs exactly.
PORTS = (47321, 47322, 47323, 47324, 47325, 47326, 47327, 47328)
LOGIN_TIMEOUT = 300
EXPIRY_MARGIN = 60

DONE_PAGE = (u"<!doctype html><meta charset='utf-8'><title>GenFill</title>"
             u"<body style='font-family:sans-serif;background:#222;color:#ddd;padding:40px'>"
             u"<h2>%s</h2><p>%s</p></body>")


class LoginRequired(BackendError):
    """No usable token and an interactive sign-in isn't allowed here."""


class TokenRejected(BackendError):
    """The token endpoint refused the grant (e.g. an expired or revoked refresh token)."""


def parse_www_authenticate(value):
    """``Bearer realm="x", resource_metadata="..."`` -> ``{"realm": "x", ...}``."""
    out = {}
    if not value:
        return out
    text = value.strip()
    if text.lower().startswith("bearer"):
        text = text[6:]
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " ,":
            i += 1
        eq = text.find("=", i)
        if eq < 0:
            break
        key = text[i:eq].strip().lower()
        i = eq + 1
        if i < n and text[i] == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            out[key] = "".join(buf)
            i = j + 1
        else:
            j = text.find(",", i)
            j = n if j < 0 else j
            out[key] = text[i:j].strip()
            i = j
    return out


def pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _origin(url):
    p = urlsplit(url)
    return "%s://%s" % (p.scheme, p.netloc)


def _path(url):
    path = urlsplit(url).path.rstrip("/")
    return path


def _get_json(url):
    status, _, body = http_request("GET", url, None, {"Accept": "application/json"}, timeout=30)
    if status != 200:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _token_error(status, body):
    try:
        data = json.loads(body.decode("utf-8"))
        return "%s %s" % (data.get("error", ""), data.get("error_description", "")).strip()
    except (ValueError, AttributeError):
        return "HTTP %s" % status


class OAuthProvider(object):
    """Sign-in state for one MCP server, persisted under ``oauth.<provider_id>``."""

    def __init__(self, provider_id, server_url, label=None, scopes=None,
                 open_browser=None, ports=PORTS):
        self.id = provider_id
        self.server_url = server_url
        self.label = label or provider_id
        self.default_scopes = scopes
        self.open_browser = open_browser or webbrowser.open
        self.ports = ports
        self._lock = threading.Lock()

    # ---- stored state ------------------------------------------------------
    def _load(self):
        return config.get_oauth(self.id)

    def _store(self, st):
        config.set_oauth(self.id, st)

    def signed_in(self):
        tokens = self._load().get("tokens") or {}
        return bool(tokens.get("refresh_token") or
                    (tokens.get("access_token") and not self._expired(tokens)))

    def sign_out(self):
        self._store(None)

    def invalidate(self):
        """Forget the access token (the server rejected it); the refresh token stays."""
        st = self._load()
        if st.get("tokens"):
            st["tokens"].pop("access_token", None)
            self._store(st)

    @staticmethod
    def _expired(tokens):
        exp = tokens.get("expires_at")
        return bool(exp) and time.time() > float(exp) - EXPIRY_MARGIN

    # ---- public entry point --------------------------------------------------
    def access_token(self, interactive=False, progress=None, cancelled=None, challenge=None):
        """A valid access token, refreshing or (if ``interactive``) signing in as needed."""
        with self._lock:
            st = self._load()
            tokens = st.get("tokens") or {}
            if tokens.get("access_token") and not self._expired(tokens):
                return tokens["access_token"]
            if tokens.get("refresh_token"):
                tok = self._refresh(st)
                if tok:
                    return tok
            if not interactive:
                raise LoginRequired("Not signed in to %s. Sign in with API Keys & Accounts..."
                                    % self.label)
            return self._login(progress, cancelled, challenge)

    def login(self, progress=None, cancelled=None):
        """Force a browser sign-in (the Sign in button)."""
        with self._lock:
            return self._login(progress, cancelled, None)

    # ---- discovery -----------------------------------------------------------
    def discover(self, challenge=None):
        """Find the authorization server's endpoints. Returns the stored state dict."""
        st = self._load()
        if st.get("endpoints") and not challenge:
            return st
        if not challenge:
            challenge = self._probe_challenge()
        params = parse_www_authenticate(challenge) if challenge else {}
        prm = None
        if params.get("resource_metadata"):
            prm = _get_json(params["resource_metadata"])
        if prm is None:
            origin, path = _origin(self.server_url), _path(self.server_url)
            for url in ([origin + "/.well-known/oauth-protected-resource" + path] if path else []) \
                    + [origin + "/.well-known/oauth-protected-resource"]:
                prm = _get_json(url)
                if prm:
                    break
        prm = prm or {}
        servers = prm.get("authorization_servers") or [_origin(self.server_url)]
        issuer = servers[0].rstrip("/")
        meta = None
        origin, path = _origin(issuer), _path(issuer)
        candidates = []
        if path:
            candidates += [origin + "/.well-known/oauth-authorization-server" + path,
                           origin + "/.well-known/openid-configuration" + path,
                           issuer + "/.well-known/openid-configuration"]
        else:
            candidates += [origin + "/.well-known/oauth-authorization-server",
                           origin + "/.well-known/openid-configuration"]
        for url in candidates:
            meta = _get_json(url)
            if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                break
            meta = None
        if meta is None:
            # 2025-03-26 servers without metadata: default endpoints on the server origin.
            meta = {"authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": issuer + "/token",
                    "registration_endpoint": issuer + "/register"}
        scope = params.get("scope") or self.default_scopes
        if not scope and prm.get("scopes_supported"):
            scope = " ".join(prm["scopes_supported"])
        if scope is not None and "offline_access" in (meta.get("scopes_supported") or []) \
                and "offline_access" not in scope.split():
            scope = (scope + " offline_access").strip()
        endpoints = {k: meta.get(k) for k in ("authorization_endpoint", "token_endpoint",
                                               "registration_endpoint")}
        if endpoints != st.get("endpoints"):
            st.pop("client", None)   # a different authorization server needs a new client
        st["endpoints"] = endpoints
        st["resource"] = prm.get("resource") or self.server_url
        st["scope"] = scope or ""
        st["auth_methods"] = meta.get("token_endpoint_auth_methods_supported") or []
        self._store(st)
        return st

    def _probe_challenge(self):
        """Ask the MCP server, unauthenticated, where its auth metadata lives (its 401)."""
        body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "GenFill", "version": "0"}}}).encode("utf-8")
        try:
            status, headers, _ = http_request("POST", self.server_url, body, {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}, timeout=30)
        except BackendError:
            return None
        if status == 401:
            return header(headers, "WWW-Authenticate")
        return None

    # ---- registration ----------------------------------------------------------
    def _register(self, st, redirect_uri):
        reg = st["endpoints"].get("registration_endpoint")
        if not reg:
            raise BackendError("%s doesn't support automatic app registration, so GenFill "
                               "can't sign in." % self.label)
        methods = st.get("auth_methods") or ["none"]
        method = "none" if "none" in methods else methods[0]
        payload = {
            "client_name": CLIENT_NAME,
            "client_uri": CLIENT_URI,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if st.get("scope"):
            payload["scope"] = st["scope"]
        status, _, body = http_request("POST", reg, json.dumps(payload).encode("utf-8"),
                                       {"Content-Type": "application/json",
                                        "Accept": "application/json"}, timeout=30)
        if status not in (200, 201):
            raise BackendError("Registering GenFill with %s failed: %s"
                               % (self.label, _token_error(status, body)))
        data = json.loads(body.decode("utf-8"))
        st["client"] = {"client_id": data["client_id"],
                        "client_secret": data.get("client_secret"),
                        "auth_method": data.get("token_endpoint_auth_method", method),
                        "redirect_uri": redirect_uri}
        self._store(st)
        return st["client"]

    # ---- browser sign-in ---------------------------------------------------------
    def _bind(self, preferred):
        ports = ([preferred] if preferred else []) + [p for p in self.ports if p != preferred]
        for port in ports:
            try:
                return _CallbackServer(("127.0.0.1", port))
            except (OSError, socket.error):
                continue
        return _CallbackServer(("127.0.0.1", 0))

    def _login(self, progress, cancelled, challenge):
        st = self.discover(challenge)
        client = st.get("client") or {}
        preferred = None
        if client.get("redirect_uri"):
            preferred = urlsplit(client["redirect_uri"]).port
        server = self._bind(preferred)
        try:
            redirect_uri = "http://127.0.0.1:%d/callback" % server.server_address[1]
            if client.get("redirect_uri") != redirect_uri:
                client = self._register(st, redirect_uri)
            verifier, code_challenge = pkce_pair()
            state = secrets.token_urlsafe(24)
            query = {
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": state,
                "resource": st["resource"],
            }
            if st.get("scope"):
                query["scope"] = st["scope"]
            auth_url = st["endpoints"]["authorization_endpoint"]
            url = auth_url + ("&" if "?" in auth_url else "?") + urlencode(query)
            if progress:
                progress(0.02, "Sign in to %s in your browser..." % self.label)
            self.open_browser(url)
            params = server.wait(LOGIN_TIMEOUT, cancelled)
        finally:
            server.server_close()
        if params.get("state") != state:
            raise BackendError("Sign-in to %s failed: the browser answer didn't match. "
                               "Try again." % self.label)
        if params.get("error"):
            raise BackendError("Sign-in to %s was refused: %s %s" % (
                self.label, params.get("error"), params.get("error_description", "")))
        if not params.get("code"):
            raise BackendError("Sign-in to %s returned no authorization code." % self.label)
        form = {
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": st["resource"],
        }
        return self._token_request(st, form, "Sign-in")

    def _refresh(self, st):
        form = {"grant_type": "refresh_token",
                "refresh_token": st["tokens"]["refresh_token"],
                "resource": st.get("resource") or self.server_url}
        try:
            return self._token_request(st, form, "Refresh")
        except TokenRejected:
            st = self._load()
            st.pop("tokens", None)
            self._store(st)
            return None

    def _token_request(self, st, form, what):
        client = st.get("client") or {}
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json"}
        form = dict(form)
        if client.get("client_secret") and client.get("auth_method") == "client_secret_basic":
            raw = ("%s:%s" % (client["client_id"], client["client_secret"])).encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            form["client_id"] = client.get("client_id", "")
            if client.get("client_secret"):
                form["client_secret"] = client["client_secret"]
        status, _, body = http_request("POST", st["endpoints"]["token_endpoint"],
                                       urlencode(form).encode("ascii"), headers, timeout=30)
        if status != 200:
            cls = TokenRejected if status in (400, 401) else BackendError
            raise cls("%s with %s failed: %s" % (what, self.label, _token_error(status, body)))
        try:
            data = json.loads(body.decode("utf-8"))
            access = data["access_token"]
        except (ValueError, KeyError):
            raise BackendError("%s with %s returned no access token." % (what, self.label))
        old = st.get("tokens") or {}
        tokens = {"access_token": access,
                  "refresh_token": data.get("refresh_token") or old.get("refresh_token"),
                  "token_type": data.get("token_type", "Bearer")}
        if data.get("expires_in"):
            tokens["expires_at"] = time.time() + float(data["expires_in"])
        st["tokens"] = tokens
        self._store(st)
        return access


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        parts = urlsplit(self.path)
        if parts.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        ok = "code" in params and "error" not in params
        page = DONE_PAGE % (
            ("Signed in" if ok else "Sign-in failed"),
            ("You can close this tab and go back to Nuke." if ok
             else "GenFill couldn't sign in: %s" % params.get("error", "unknown error")))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.result = params

    def log_message(self, *args):
        pass


class _CallbackServer(HTTPServer):
    # Rebind right after the previous sign-in (TIME_WAIT). On Windows SO_REUSEADDR would let
    # two servers share a port, and Windows rebinds TIME_WAIT ports anyway.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address):
        HTTPServer.__init__(self, address, _Handler)
        self.result = None
        self.timeout = 0.5

    def wait(self, timeout, cancelled=None):
        deadline = time.time() + timeout
        while self.result is None:
            if cancelled and cancelled():
                raise Cancelled("Cancelled")
            if time.time() > deadline:
                raise BackendError("Sign-in timed out. Try again and finish it in the browser.")
            self.handle_request()
        return self.result
