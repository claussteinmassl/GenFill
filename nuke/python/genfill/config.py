"""User settings: API keys, sign-in tokens, cache location and naming patterns.

API keys are **never** stored in the .nk script (scripts get shared and
archived). They come from, in order:

1. An environment variable (e.g. ``STABILITY_API_KEY``, ``OPENAI_API_KEY``),
   which is how a studio would deploy them.
2. ``~/.nuke/genfill/config.json``, written by the node's *API Keys & Accounts...* button.

Cache folder and names follow the same rule: an environment variable set by the pipeline
(``GENFILL_CACHE``, ``GENFILL_NAME``, ``GENFILL_VARIATION_NAME``, see :mod:`genfill.naming`)
wins over ``config.json`` (``cache_root``, ``name_pattern``, ``variation_pattern``). They are
read on every Generate, so a pipeline can change them per shot without restarting Nuke.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".nuke", "genfill")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

_lock = threading.RLock()


def load():
    try:
        with open(CONFIG_PATH, "r") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def save(data):
    with _lock:
        _save(data)


def _save(data):
    # The file holds keys and tokens: private from the first byte, never world-readable.
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, 0o700)
    tmp = "%s.%d.tmp" % (CONFIG_PATH, os.getpid())   # two Nuke sessions never share one
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def update(fn):
    """Read-modify-write the config under a lock: ``fn(data)`` edits ``data`` in place."""
    with _lock:
        data = load()
        fn(data)
        _save(data)
        return data


def get_oauth(provider_id):
    """Stored OAuth state (client registration, endpoints, tokens) for a provider."""
    return copy.deepcopy(load().get("oauth", {}).get(provider_id) or {})


def set_oauth(provider_id, value):
    def edit(data):
        section = data.setdefault("oauth", {})
        if value:
            section[provider_id] = value
        else:
            section.pop(provider_id, None)
    update(edit)


def get_api_key(backend_id, env_var=None):
    if env_var and os.environ.get(env_var):
        return os.environ[env_var].strip()
    return (load().get("api_keys", {}).get(backend_id) or "").strip()


def key_source(backend_id, env_var=None):
    """Human readable description of where a key would come from."""
    if env_var and os.environ.get(env_var):
        return "environment variable %s" % env_var
    if load().get("api_keys", {}).get(backend_id):
        return CONFIG_PATH
    return None


def set_api_key(backend_id, key):
    def edit(data):
        keys = data.setdefault("api_keys", {})
        if key:
            keys[backend_id] = key.strip()
        else:
            keys.pop(backend_id, None)
    update(edit)


def _env(name):
    return (os.environ.get(name) or "").strip()


def cache_root_pattern():
    """Cache root pattern from the pipeline (``GENFILL_CACHE``), or ``""``."""
    return _env("GENFILL_CACHE")


def default_cache_root():
    """Fallback cache root when the script is unsaved and no override is set."""
    root = (load().get("cache_root") or "").strip()
    if root:
        return root
    return os.path.join(tempfile.gettempdir(), "genfill_cache")


def name_pattern():
    """Generation folder pattern: ``GENFILL_NAME``, then config.json, then the default."""
    from .naming import DEFAULT_NAME
    return _env("GENFILL_NAME") or (load().get("name_pattern") or "").strip() or DEFAULT_NAME


def variation_pattern():
    """Variation file pattern: ``GENFILL_VARIATION_NAME``, then config.json, then the default."""
    from .naming import DEFAULT_VARIATION
    return (_env("GENFILL_VARIATION_NAME") or (load().get("variation_pattern") or "").strip()
            or DEFAULT_VARIATION)
