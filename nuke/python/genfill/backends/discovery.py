"""Live model discovery: provider catalogues fetched at runtime, cached for 24 hours.

A backend that can list its models (``Backend.discoverable``) implements
``Backend.discover(api_key)`` and returns :class:`ModelSpec` entries built with the same
``extra`` keys as its curated catalogue, so ``generate`` runs them through its generic path.
This module stores what was found in ``~/.nuke/genfill/models_cache.json``::

    {"<backend id>": {"fetched": "2026-10-15T12:00:00Z", "models": [{"id": ..., ...}]}}

and merges it under the curated list: curated entries always win (labels, parameters), a
discovered entry only adds an id the curated list does not have.

Discovery never runs on the Generate path and never opens a browser. The panel starts it on a
worker thread (:func:`start`, :func:`maybe_start`) and polls :func:`revision` and
:func:`state` from its timer. Not signed in, offline or a provider error keeps the cached (or
curated) list and is only logged. The file holds model metadata only, never keys or tokens.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import threading
import time

from .base import ModelSpec

TTL_SECONDS = 24 * 3600
CACHE_NAME = "models_cache.json"

_lock = threading.RLock()
_cache = None          # backend id -> {"fetched": iso, "models": [dict]}, loaded lazily
_specs = {}            # backend id -> [ModelSpec] (deserialized cache entries)
_jobs = {}             # backend id -> running worker thread
_attempted = {}        # backend id -> time.time() of the last automatic attempt (this session)
_status = {}           # backend id -> {"ok": bool, "message": str, "new": int, "when": float}
_revision = [0]        # bumped whenever a cache entry or a worker state changes


def cache_path():
    """Location of the cache file (next to config.json)."""
    from .. import config
    return os.path.join(config.CONFIG_DIR, CACHE_NAME)


# --------------------------------------------------------------------------
# (de)serialization
# --------------------------------------------------------------------------

def spec_to_dict(spec):
    """A JSON-safe dict for one :class:`ModelSpec`."""
    return {"id": spec.id, "label": spec.label, "kind": spec.kind, "erase": spec.erase,
            "max_count": spec.max_count, "note": spec.note, "extra": _jsonable(spec.extra)}


def spec_from_dict(data):
    """The :class:`ModelSpec` for a cache entry (marked ``discovered``), or ``None``."""
    if not isinstance(data, dict) or not data.get("id"):
        return None
    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    try:
        spec = ModelSpec(str(data["id"]), str(data.get("label") or data["id"]),
                         kind=str(data.get("kind") or "edit"), erase=data.get("erase"),
                         max_count=int(data.get("max_count") or 1),
                         note=str(data.get("note") or ""), **extra)
    except (TypeError, ValueError):
        return None
    spec.discovered = True
    return spec


def _jsonable(value):
    if isinstance(value, dict):
        return dict((str(k), _jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


# --------------------------------------------------------------------------
# cache file
# --------------------------------------------------------------------------

def _load():
    """The cache dict, read from disk once per session (callers hold ``_lock``)."""
    global _cache
    if _cache is None:
        try:
            with open(cache_path(), "r") as fh:
                data = json.load(fh)
            _cache = data if isinstance(data, dict) else {}
        except (IOError, OSError, ValueError):
            _cache = {}
    return _cache


def _save(data):
    """Write the cache atomically (temp file + rename), like ``config._save``."""
    path = cache_path()
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder, 0o700)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def reset():
    """Forget the in-memory state (tests, or after the cache file was edited by hand)."""
    global _cache
    with _lock:
        _cache = None
        _specs.clear()
        _attempted.clear()
        _status.clear()
        _revision[0] += 1


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_iso(text):
    try:
        return float(calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def cached(backend_id):
    """Discovered specs for a backend from the cache (any age); ``[]`` if none."""
    with _lock:
        if backend_id not in _specs:
            entry = _load().get(backend_id) or {}
            models = entry.get("models") if isinstance(entry, dict) else None
            specs = [spec_from_dict(m) for m in models or []]
            _specs[backend_id] = [s for s in specs if s is not None]
        return list(_specs[backend_id])


def fetched_at(backend_id):
    """``time.time()`` of the last successful fetch for a backend, 0 if never."""
    with _lock:
        entry = _load().get(backend_id) or {}
        return _parse_iso(entry.get("fetched")) if isinstance(entry, dict) else 0.0


def is_fresh(backend_id, ttl=TTL_SECONDS, now=None):
    """True if the cached list is younger than ``ttl`` seconds."""
    now = time.time() if now is None else now
    fetched = fetched_at(backend_id)
    return bool(fetched) and 0 <= now - fetched < ttl


def store(backend_id, specs, now=None):
    """Replace a backend's cache entry with ``specs`` and write the file."""
    now = time.time() if now is None else now
    with _lock:
        data = dict(_load())
        data[backend_id] = {"fetched": _iso(now), "models": [spec_to_dict(s) for s in specs]}
        _save(data)
        _load().clear()
        _load().update(data)
        _specs.pop(backend_id, None)
        _revision[0] += 1


def merge(curated, discovered):
    """Curated entries first (they always win), then discovered ids the curated list lacks.

    A discovered entry whose label is already taken gets its id appended, so every menu
    label maps back to one id.
    """
    out = list(curated)
    seen = set(m.id for m in out)
    labels = set(m.label for m in out)
    for m in discovered:
        if m.id not in seen:
            if m.label in labels:
                m.label = "%s (%s)" % (m.label, m.id)
            seen.add(m.id)
            labels.add(m.label)
            out.append(m)
    return out


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def refresh(backend, api_key="", force=False):
    """Fetch ``backend``'s live list and cache it, unless the cache is fresh (``force``).

    Never raises: not signed in, offline or a provider error keeps the cache and is logged.

    Returns:
        The number of discovered models that are not curated, or ``None`` if nothing was
        fetched.
    """
    if not getattr(backend, "discoverable", False):
        return None
    if not force and is_fresh(backend.id):
        return None
    try:
        specs = backend.discover(api_key) or []
    except Exception as exc:   # discovery is best effort; never disturb the artist
        _set_status(backend.id, False, _short(exc))
        _log("info", "Model discovery for %s skipped: %s", backend.id, exc)
        return None
    curated = set(m.id for m in backend.catalogue)
    new = len([s for s in specs if s.id not in curated])
    try:
        store(backend.id, specs)
    except (IOError, OSError) as exc:
        _set_status(backend.id, False, "Could not write %s: %s" % (CACHE_NAME, exc))
        _log("warning", "Could not write the model cache: %s", exc)
        return None
    _set_status(backend.id, True, "%d model%s found, %d not in GenFill's list"
                % (len(specs), "s" * (len(specs) != 1), new), new)
    _log("info", "Model discovery for %s: %d models, %d new", backend.id, len(specs), new)
    return new


def start(backend, api_key="", force=True):
    """Run :func:`refresh` on a daemon thread; False if one is already running."""
    if not getattr(backend, "discoverable", False):
        return False
    with _lock:
        job = _jobs.get(backend.id)
        if job is not None and job.is_alive():
            return False

        def run():
            try:
                refresh(backend, api_key, force)
            finally:
                with _lock:
                    _jobs.pop(backend.id, None)
                    _revision[0] += 1

        job = threading.Thread(target=run, name="GenFill model discovery (%s)" % backend.id)
        job.daemon = True
        _jobs[backend.id] = job
        _revision[0] += 1
    job.start()
    return True


def maybe_start(backend, api_key="", now=None):
    """Automatic discovery: at most once per TTL per backend, only when the cache is stale."""
    if not getattr(backend, "discoverable", False):
        return False
    now = time.time() if now is None else now
    with _lock:
        if is_fresh(backend.id, now=now):
            return False
        last = _attempted.get(backend.id)
        if last is not None and now - last < TTL_SECONDS:
            return False
        _attempted[backend.id] = now
    return start(backend, api_key, force=False)


def busy(backend_id):
    with _lock:
        job = _jobs.get(backend_id)
        return job is not None and job.is_alive()


def wait(backend_id, timeout=None):
    """Block until a running discovery finishes (tests and tools; never the UI thread)."""
    with _lock:
        job = _jobs.get(backend_id)
    if job is not None:
        job.join(timeout)


def revision():
    """Counter that changes whenever cached lists or worker states change."""
    return _revision[0]


def state(backend_id):
    """``{"busy", "ok", "message", "new", "when", "fetched"}`` for the panel."""
    with _lock:
        st = dict(_status.get(backend_id) or {})
    st["busy"] = busy(backend_id)
    st["fetched"] = fetched_at(backend_id)
    return st


def _set_status(backend_id, ok, message, new=0):
    with _lock:
        _status[backend_id] = {"ok": ok, "message": message, "new": new, "when": time.time()}
        _revision[0] += 1


def _short(exc):
    return (str(exc) or exc.__class__.__name__).strip()[:300]


def _log(level, msg, *args):
    try:
        from .. import log as gflog
        getattr(gflog, level)(msg, *args)
    except Exception:
        pass


# --------------------------------------------------------------------------
# helpers shared by the backends' discover()
# --------------------------------------------------------------------------

def parse_toon(text):
    """Parse the indented list text some MCP tools return instead of JSON.

    Handles the shape of Magnific's ``retouch_models_list``::

        models[2]:
          - slug: retouch-auto
            beta: false
            retouchModes[2]: replace,erase

    Returns:
        A dict of top-level keys; a ``key[n]:`` line followed by ``- `` items becomes a list
        of dicts, an inline ``key[n]: a,b`` a list of strings.
    """
    out = {}
    current_list = None
    item = None
    for raw in str(text or "").splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            key, value = _split(line)
            if key is None:
                continue
            if value == "" and key.endswith("]"):
                current_list = out.setdefault(_bare(key), [])
                item = None
            else:
                out[_bare(key)] = _scalar(value, key)
                current_list = None
            continue
        if current_list is None:
            continue
        if line.startswith("- "):
            item = {}
            current_list.append(item)
            line = line[2:].strip()
        if item is None:
            continue
        key, value = _split(line)
        if key is not None:
            item[_bare(key)] = _scalar(value, key)
    return out


def _split(line):
    if ":" not in line:
        return None, None
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _bare(key):
    return key.split("[", 1)[0]


def _scalar(value, key=""):
    if key.endswith("]"):
        return [v.strip() for v in value.split(",") if v.strip()] if value else []
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def ratio_list(values):
    """The ``"w:h"`` entries of a provider's aspect-ratio list (drops ``auto`` and junk)."""
    out = []
    for v in values or []:
        parts = str(v).split(":")
        if len(parts) != 2:
            continue
        try:
            if float(parts[0]) > 0 and float(parts[1]) > 0:
                out.append(str(v))
        except ValueError:
            continue
    return out


def smallest_resolution(values):
    """The cheapest entry of a resolution list such as ``["2k", "1.5k", "4k"]``, or None."""
    best, best_n = None, None
    for v in values or []:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(k|p|sq)?\s*$", str(v).lower())
        if not m:
            continue
        n = float(m.group(1))
        if m.group(2) != "k":
            n /= 1000.0   # "768p", "768sq": pixels, not thousands
        if best_n is None or n < best_n:
            best, best_n = str(v), n
    return best


def quality_map(options):
    """GenFill quality (low/medium/high) -> the same value where the provider offers it."""
    opts = set(str(o) for o in options or [])
    found = dict((q, q) for q in ("low", "medium", "high") if q in opts)
    return found or None
