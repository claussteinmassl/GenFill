"""Shared plumbing for cloud backends that talk to a remote MCP server.

A subclass sets ``server_url`` (and ``api_key_header`` if the server also takes a key) and
implements ``generate`` with :meth:`McpBackend.client`. Sign-in happens on demand inside the
generation thread: the first call opens the browser, later calls reuse the stored tokens.

Paid-results policy (all cloud backends, fal.ai included): every image a provider produced
and GenFill could download is returned, even if sibling variations failed; ``generate``
raises only when there is none (:func:`collect` + :func:`finish`). A failed submission keeps
the jobs already queued. When GenFill gives up after submitting (error, timeout or cancel), it
cancels the jobs that are still running wherever the provider offers a cancel.
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

    def quiet_client(self, api_key=""):
        """A non-interactive client for quotes and balances; LoginRequired if not signed in."""
        from .oauth import LoginRequired
        if not (api_key and self.api_key_header) and not self.provider().signed_in():
            raise LoginRequired("Not signed in to %s (API Keys & Accounts...)."
                                % self.label.split(" (")[0])
        return self.client(api_key, interactive=False)


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


def pause(seconds, cancelled=None):
    """Sleep ``seconds`` in short steps, raising :class:`Cancelled` if asked to stop."""
    for _ in range(max(1, int(round(seconds / 0.25)))):
        if cancelled and cancelled():
            raise Cancelled("Cancelled")
        time.sleep(0.25)


def collect(items, fn, cancelled=None, errors=None):
    """``fn(item)`` for every item, keeping what succeeds.

    Args:
        items: Inputs, e.g. result URLs.
        fn: Turns one item into a result; may raise :class:`BackendError`.
        cancelled: Checked before every item; :class:`Cancelled` always propagates.
        errors: Optional list that collects the messages of failed items.

    Returns:
        ``(results, errors)``.
    """
    results, errors = [], ([] if errors is None else errors)
    for item in items:
        if cancelled and cancelled():
            raise Cancelled("Cancelled")
        try:
            results.append(fn(item))
        except Cancelled:
            raise
        except BackendError as exc:
            errors.append(str(exc))
    return results, errors


def finish(results, errors, provider):
    """Return ``results`` if there are any, else raise one message naming every failure."""
    if results:
        if errors:
            try:
                from .. import log as gflog
                gflog.warning("%s: %d of %d variations failed: %s", provider, len(errors),
                              len(errors) + len(results), "; ".join(_unique(errors)))
            except Exception:
                pass
        return results
    unique = _unique(errors)
    if not unique:
        raise BackendError("%s returned no images." % provider)
    raise BackendError(unique[0] if len(unique) == 1 else "; ".join(unique)[:800])


def _unique(messages):
    out = []
    for m in messages:
        if m not in out:
            out.append(m)
    return out


def progress_step(progress, frac, msg):
    if progress:
        progress(frac, msg)
