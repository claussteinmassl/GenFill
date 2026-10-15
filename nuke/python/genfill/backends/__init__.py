"""Registry of generation backends.

To add one: subclass :class:`base.Backend`, then append an instance to
``_BACKENDS``. Order here is the order shown in the node's model menu.
"""
from __future__ import annotations

from .base import Backend, BackendError, Cancelled, GenerationRequest
from .comfy_cloud import ComfyCloudBackend
from .fal import FalBackend
from .higgsfield import HiggsfieldBackend
from .local import LocalBackend
from .magnific import MagnificBackend
from .mock import MockBackend
from .openai_images import OpenAIBackend
from .stability import StabilityBackend

_BACKENDS = [LocalBackend(), StabilityBackend(), OpenAIBackend(), FalBackend(), MagnificBackend(),
             HiggsfieldBackend(), ComfyCloudBackend(), MockBackend()]

__all__ = ["Backend", "BackendError", "Cancelled", "GenerationRequest", "all_backends", "get",
           "key_backends", "account_backends"]


def all_backends():
    return list(_BACKENDS)


def ids():
    return [b.id for b in _BACKENDS]


def get(backend_id):
    for b in _BACKENDS:
        if b.id == backend_id:
            return b
    raise KeyError("Unknown GenFill backend %r" % backend_id)


def key_backends():
    """Backends that get an API-key row (required, or optional next to a sign-in)."""
    return [b for b in _BACKENDS if b.needs_key or b.auth == "key_or_oauth"]


def account_backends():
    """Backends that get an account row (browser sign-in)."""
    return [b for b in _BACKENDS if b.auth in ("oauth", "key_or_oauth")]
