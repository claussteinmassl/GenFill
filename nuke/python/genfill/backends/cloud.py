"""Shared plumbing for cloud backends that talk to a remote MCP server.

A subclass sets ``server_url`` (and ``api_key_header`` if the server also takes a key) and
implements ``generate`` with :meth:`McpBackend.client`. Sign-in happens on demand inside the
generation thread: the first call opens the browser, later calls reuse the stored tokens.
"""
from __future__ import annotations

import time

from .base import (REF_EMPTY_PROMPT, REF_PROMPT_SUFFIX, Backend, BackendError, Cancelled,
                   http_request, raise_for_status)
from .mcp import ApiKeyAuth, McpClient, OAuthAuth
from .oauth import OAuthProvider


class McpBackend(Backend):
    server_url = ""
    auth = "oauth"
    needs_key = False
    api_key_header = None    # e.g. "X-API-Key": a key, if set, is used instead of OAuth
    oauth_scopes = None      # None = what the server asks for
    tool_timeout = 90        # seconds per MCP request (long-poll tools wait ~25 s)

    _providers = {}

    def provider(self):
        """The OAuth sign-in state for this backend (one per backend id)."""
        p = McpBackend._providers.get(self.id)
        if p is None or p.server_url != self.server_url:
            p = OAuthProvider(self.id, self.server_url, label=self.label.split(" (")[0],
                              scopes=self.oauth_scopes)
            McpBackend._providers[self.id] = p
        return p

    def signed_in(self):
        return self.provider().signed_in()

    def client(self, api_key="", progress=None, cancelled=None, interactive=True):
        if api_key and self.api_key_header:
            auth = ApiKeyAuth(self.api_key_header, api_key, self.label)
        else:
            auth = OAuthAuth(self.provider())
        return McpClient(self.server_url, auth, label=self.label.split(" (")[0],
                         interactive=interactive, progress=progress, cancelled=cancelled,
                         timeout=self.tool_timeout)


def ref_prompt(prompt):
    """Prompt for a reference-edit model that never sees the mask."""
    prompt = (prompt or "").strip()
    return REF_EMPTY_PROMPT if not prompt else prompt + REF_PROMPT_SUFFIX


def put_bytes(url, data, content_type, headers=None, timeout=300):
    """Upload ``data`` to a presigned URL with HTTP PUT; returns the response body."""
    hdrs = {"Content-Type": content_type}
    hdrs.update(headers or {})
    status, _, body = http_request("PUT", url, data, hdrs, timeout)
    raise_for_status(status, body)
    return body


def poll(fn, cancelled=None, timeout=900, interval=2.0, what="the result"):
    """Call ``fn()`` until it returns something other than ``None``.

    ``fn`` may itself block (a long-poll tool), in which case ``interval`` can be 0.
    """
    deadline = time.time() + timeout
    while True:
        if cancelled and cancelled():
            raise Cancelled("Cancelled")
        value = fn()
        if value is not None:
            return value
        if time.time() > deadline:
            raise BackendError("Timed out waiting for %s." % what)
        if interval:
            end = time.time() + interval
            while time.time() < end:
                if cancelled and cancelled():
                    raise Cancelled("Cancelled")
                time.sleep(min(0.25, interval))


def progress_step(progress, frac, msg):
    if progress:
        progress(frac, msg)
