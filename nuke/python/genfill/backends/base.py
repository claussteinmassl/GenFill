"""Backend interface plus small stdlib-only HTTP helpers.

A backend turns a :class:`GenerationRequest` (a crop of the plate, a mask and
a prompt) into a list of PNG images (bytes), one per variation. Backends know
nothing about Nuke; that keeps them testable and lets a future local backend
(ComfyUI, diffusers) drop in alongside the cloud ones.
"""
from __future__ import annotations

import json
import socket
import uuid

try:  # Python 3
    from urllib import request as _urlrequest
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover - Python 2 Nuke is not supported
    raise

USER_AGENT = "GenFill-Nuke/0.9"


# Prompts for models that re-render the whole crop (GenFill comps the result back through the
# selection, so they only have to get the selected area right).
EDIT_EMPTY_PROMPT = ("Remove whatever is inside the masked area and fill it so it seamlessly "
                     "matches the surrounding image, as a clean plate. Keep lighting, grain, "
                     "perspective and focus consistent. Change nothing outside the mask.")
EDIT_PROMPT_SUFFIX = (" Only change the masked area; match the lighting, grain, perspective and "
                      "focus of the surrounding photo.")
# Same for reference-edit models that never see the mask: describe the edit in words.
REF_EMPTY_PROMPT = ("Remove the object in the marked area and fill it so it seamlessly matches "
                    "the surroundings, as a clean plate. Keep everything else exactly the same: "
                    "framing, lighting, grain, perspective and focus.")
REF_PROMPT_SUFFIX = (" Keep everything else in the photo exactly the same: framing, lighting, "
                     "grain, perspective and focus.")


# --------------------------------------------------------------------------
# Processing resolution (the gf_resolution knob)
# --------------------------------------------------------------------------

RESOLUTIONS = ("auto", "1K", "2K", "4K")
TIERS = ("1K", "2K", "4K")                      # smallest first
TIER_PIXELS = {"1K": 1024 * 1024, "2K": 2048 * 2048, "4K": 4096 * 4096}
NATIVE_SHARE = 0.8      # a tier "holds" a crop that fits 80% of its pixels (see sent_tier)


def normalize_resolution(value):
    """``"auto"`` or one of :data:`TIERS` for a knob or provider value (``"2k"`` -> ``"2K"``)."""
    text = str(value or "").strip().upper()
    return text if text in TIER_PIXELS else "auto"


def resolution_budget(resolution, default_budget):
    """Pixel budget of a resolution tier; ``default_budget`` for ``auto``."""
    return TIER_PIXELS.get(normalize_resolution(resolution), default_budget)


def tiers_from(values):
    """The GenFill tiers in a provider's resolution list (``["1k", "1.5k", "2k"]`` -> 1K, 2K).

    Only exact 1K/2K/4K entries count (case-insensitive, also ``auto_2K``); in-between sizes
    such as ``1.5k`` stay available as the model's ``auto`` default.
    """
    found = set()
    for v in values or []:
        text = str(v).strip().upper()
        if text.startswith("AUTO_"):
            text = text[len("AUTO_"):]
        if text in TIER_PIXELS:
            found.add(text)
    return tuple(t for t in TIERS if t in found)


def clamp_resolution(resolution, supported):
    """``(tier, note)``: the tier a model runs for a requested resolution.

    Args:
        resolution: Knob value (``auto``, ``1K``, ``2K``, ``4K``).
        supported: The model's tiers; ``None`` = any size, ``()`` = fixed size.

    Returns:
        ``("auto", "")`` for auto, ``("auto", "fixed size")`` for a fixed-size model, else the
        requested tier or the nearest one the model offers with a note ("model max 2K").
    """
    res = normalize_resolution(resolution)
    if res == "auto":
        return "auto", ""
    if supported is None:
        return res, ""
    supported = [t for t in TIERS if t in supported]
    if not supported:
        return "auto", "fixed size"
    if res in supported:
        return res, ""
    if TIERS.index(res) > TIERS.index(supported[-1]):
        return supported[-1], "model max %s" % supported[-1]
    if TIERS.index(res) < TIERS.index(supported[0]):
        return supported[0], "model min %s" % supported[0]
    below = [t for t in supported if TIERS.index(t) < TIERS.index(res)]
    return below[-1], "model offers %s" % ", ".join(supported)


def tier_budget(width, height, tier, default_budget, min_pixels=None, max_pixels=None):
    """Pixel budget for a crop of ``width`` x ``height`` (plate pixels) at ``tier``.

    ``auto`` keeps ``default_budget``. A tier never upscales the crop: a context region with
    fewer pixels than the tier is sent at its own size. ``max_pixels`` / ``min_pixels`` are the
    model's limits (``min_pixels`` wins: some APIs refuse small images).
    """
    if normalize_resolution(tier) == "auto":
        return default_budget
    budget = min(TIER_PIXELS[normalize_resolution(tier)], int(width) * int(height))
    if max_pixels:
        budget = min(budget, int(max_pixels))
    if min_pixels:
        budget = max(budget, int(min_pixels))
    return budget


def sent_tier(tier, width, height, supported=None):
    """The tier to ask a provider for when ``width`` x ``height`` is sent at ``tier``.

    Usually ``tier`` itself; for a crop smaller than the tier (sent at its own size) the
    smallest supported tier that holds it, so the provider does not render (and bill) more
    pixels than the crop has.
    """
    tier = normalize_resolution(tier)
    if tier == "auto":
        return tier
    pixels = int(width) * int(height) * NATIVE_SHARE
    options = [t for t in TIERS if (supported is None or t in supported)
               and TIERS.index(t) <= TIERS.index(tier)]
    for t in options:
        if TIER_PIXELS[t] >= pixels:
            return t
    return tier


class SizePlan(object):
    """What the panel and the log say about the size sent to a model.

    ``width``/``height`` are sent; ``tier`` is the tier the model runs (``auto`` for auto and
    fixed-size models); ``note`` explains a clamp ("model max 2K"); ``native`` is True when
    the crop is smaller than the tier and goes at its own size.
    """

    def __init__(self, width, height, requested, tier, note="", native=False):
        self.width, self.height = int(width), int(height)
        self.requested = normalize_resolution(requested)
        self.tier = tier
        self.note = note
        self.native = native

    def text(self):
        """``"sends 2048 x 1152 (2K)"`` and friends (\u00d7 as the times sign)."""
        size = u"%d \u00d7 %d" % (self.width, self.height)
        if self.requested == "auto":
            what = "auto"
        elif self.tier == "auto":
            what = self.note or "fixed size"
        elif self.native:
            what = "crop is smaller than %s" % self.tier
            if self.note:
                what += ", " + self.note
        else:
            what = self.tier + (", " + self.note if self.note else "")
        return u"sends %s (%s)" % (size, what)

    def __repr__(self):
        return "SizePlan(%dx%d, %s -> %s %r)" % (self.width, self.height, self.requested,
                                                 self.tier, self.note)


class BackendError(RuntimeError):
    """Raised with a message suitable for showing an artist."""


class Cancelled(BackendError):
    pass


class GenerationRequest(object):
    def __init__(self, image_path, mask_path, mask_alpha_path, prompt, width, height,
                 seed=0, count=3, negative_prompt="", model="", quality="auto",
                 resolution="auto"):
        self.image_path = image_path          # sRGB 8-bit RGB PNG
        self.mask_path = mask_path            # white = fill, black = keep (RGB)
        self.mask_alpha_path = mask_alpha_path  # RGBA, alpha 0 = fill (OpenAI style)
        self.prompt = prompt or ""
        self.width = int(width)
        self.height = int(height)
        self.seed = int(seed)
        self.count = int(count)
        self.negative_prompt = negative_prompt or ""
        self.model = model or ""
        self.quality = quality or "auto"
        self.resolution = normalize_resolution(resolution)   # requested: auto, 1K, 2K, 4K

    def read(self, attr):
        with open(getattr(self, attr), "rb") as fh:
            return fh.read()


class ModelSpec(object):
    """One entry of a backend's model catalogue.

    ``kind``: ``inpaint`` takes the mask; ``edit`` re-renders the crop from the image (GenFill
    comps it back through the selection); ``erase`` removes the masked content (empty prompt).

    Shared ``extra`` keys: ``resolutions`` (tuple of the processing tiers the model supports,
    "1K"/"2K"/"4K"; ``()`` = fixed size; missing = the backend's ``default_resolutions``) and
    ``max_pixels`` / ``min_pixels`` (the model's pixel limits for a tier).
    """

    def __init__(self, id, label, kind="inpaint", erase=None, max_count=4, note="", **extra):
        self.id = id
        self.label = label
        self.kind = kind
        self.erase = erase          # model id to use for an empty prompt (None = this one)
        self.max_count = max_count  # images per request
        self.note = note
        self.extra = extra
        self.discovered = False     # True: found by live discovery, not in the curated list

    def __repr__(self):
        return "ModelSpec(%r)" % self.id


class Backend(object):
    id = "base"
    label = "Base"
    env_var = None          # environment variable holding the API key
    needs_key = True
    default_model = ""
    models = ()             # suggestions shown in the UI
    description = ""
    float_io = False        # True: takes 32-bit float TIFF in, returns float TIFF
    result_ext = ".png"     # default; the engine names PNG/JPEG results by their content
    auth = "key"            # "key", "oauth" (browser sign-in) or "key_or_oauth"
    catalogue = ()          # ModelSpec entries, shown in the model version menu
    discoverable = False    # True: discover() lists the provider's models live

    def discover(self, api_key=""):
        """Fetch the provider's current model list (see :mod:`discovery`).

        Runs on a worker thread, never on the Generate path, and must never open a browser.
        Entries use the same ``ModelSpec.extra`` keys as the curated catalogue so ``generate``
        handles them. Raises :class:`BackendError` (or anything else) when the list can't be
        fetched; the caller then keeps the cached list.
        """
        return []

    def all_models(self):
        """The curated catalogue followed by cached discovered models it doesn't list."""
        if not self.discoverable:
            return list(self.catalogue)
        from . import discovery
        return discovery.merge(self.catalogue, discovery.cached(self.id))

    def model_ids(self):
        """Model ids for the model version menu."""
        return [m.id for m in self.all_models()] or list(self.models)

    def model_spec(self, model=""):
        """The catalogue entry for ``model`` (default model if empty, ad-hoc spec if unknown).

        Curated entries win over discovered ones with the same id.
        """
        model = (model or self.default_model).strip()
        for m in self.all_models():
            if m.id == model:
                return m
        return self.adhoc_spec(model) if model else None

    def adhoc_spec(self, model):
        """Spec for an id that is neither curated nor discovered (typed by the artist)."""
        return ModelSpec(model, model, kind="edit")

    # ``resolutions`` of models whose catalogue entry doesn't say (None = any size).
    default_resolutions = TIERS

    def resolutions(self, model=""):
        """The tiers ``model`` supports (``None`` = any, ``()`` = fixed size)."""
        spec = self.model_spec(model)
        if spec is not None and "resolutions" in spec.extra:
            value = spec.extra["resolutions"]
            return None if value is None else tuple(value)
        return self.default_resolutions

    def pixel_limits(self, model=""):
        """``(min_pixels, max_pixels)`` of ``model`` for a tier (None = no limit)."""
        spec = self.model_spec(model)
        x = spec.extra if spec is not None else {}
        return x.get("min_pixels"), x.get("max_pixels")

    def resolution_plan(self, model="", resolution="auto"):
        """``(tier, note)``: the tier ``model`` runs for ``resolution`` (see clamp_resolution)."""
        tier, note = clamp_resolution(resolution, self.resolutions(model))
        _, max_pixels = self.pixel_limits(model)
        if tier != "auto" and not note and max_pixels and TIER_PIXELS[tier] > max_pixels:
            note = "model max %.1f MP" % (max_pixels / 1e6)
        return tier, note

    def tier_budget(self, width, height, model, resolution, default_budget):
        """Pixel budget for ``model_size``: ``default_budget`` for auto, else the tier's."""
        tier, _ = self.resolution_plan(model, resolution)
        min_pixels, max_pixels = self.pixel_limits(model)
        return tier_budget(width, height, tier, default_budget, min_pixels, max_pixels)

    def model_size(self, width, height, model="", resolution="auto"):
        """Resolution to send for a crop of ``width`` x ``height`` (plate pixels).

        ``resolution`` is the processing tier (``auto`` = the backend's own size, ``1K``,
        ``2K``, ``4K``): the tier's pixel budget in the crop's aspect, clamped to the model's
        limits, never more pixels than the crop has.
        """
        raise NotImplementedError

    def size_plan(self, width, height, model="", resolution="auto"):
        """:class:`SizePlan` of what ``model_size`` sends, for the panel and the log."""
        mw, mh = self.model_size(width, height, model, resolution)
        tier, note = self.resolution_plan(model, resolution)
        native = (tier != "auto"
                  and int(width) * int(height) < TIER_PIXELS[tier] * NATIVE_SHARE
                  and mw * mh < TIER_PIXELS[tier] * NATIVE_SHARE)
        return SizePlan(mw, mh, resolution, tier, note, native)

    def request_tier(self, request, model=None):
        """The tier to ask the provider for in ``generate`` (``auto``: send nothing new)."""
        model = request.model if model is None else model
        tier, _ = self.resolution_plan(model, request.resolution)
        return sent_tier(tier, request.width, request.height, self.resolutions(model))

    def generate(self, request, api_key, progress=None, cancelled=None):
        """Return a list of PNG bytes, one per variation."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def encode_multipart(fields, files):
    """Encode form ``fields`` {name: str} and ``files`` {name: (filename, bytes, ctype)}."""
    boundary = "----GenFill%s" % uuid.uuid4().hex
    crlf = b"\r\n"
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(b"--" + boundary.encode())
        parts.append(('Content-Disposition: form-data; name="%s"' % name).encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    for name, (filename, data, ctype) in files.items():
        parts.append(b"--" + boundary.encode())
        parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"'
                      % (name, filename)).encode())
        parts.append(("Content-Type: %s" % ctype).encode())
        parts.append(b"")
        parts.append(data)
    parts.append(b"--" + boundary.encode() + b"--")
    parts.append(b"")
    return crlf.join(parts), "multipart/form-data; boundary=%s" % boundary


def _error_message(status, body):
    text = body.decode("utf-8", "replace") if body else ""
    try:
        data = json.loads(text)
    except ValueError:
        return "HTTP %s: %s" % (status, text[:300] or "no details")
    # OpenAI: {"error": {"message": ...}}; Stability: {"name":..., "errors": [...]}
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return "HTTP %s: %s" % (status, err["message"])
        if data.get("errors"):
            return "HTTP %s: %s" % (status, "; ".join(str(e) for e in data["errors"]))
        if data.get("message"):
            return "HTTP %s: %s" % (status, data["message"])
    return "HTTP %s: %s" % (status, text[:300])


def header(headers, name, default=None):
    """Case-insensitive lookup in a response headers dict."""
    low = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == low:
            return v
    return default


def http_request(method, url, body=None, headers=None, timeout=180):
    """Send a request and return ``(status, headers_dict, body_bytes)`` for any HTTP status.

    Only network failures raise (:class:`BackendError`); callers decide what an
    error status means.
    """
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    req = _urlrequest.Request(url, data=body, headers=hdrs, method=method)
    try:
        resp = _urlrequest.urlopen(req, timeout=timeout)
        return resp.getcode(), dict(resp.headers), resp.read()
    except HTTPError as exc:
        data = exc.read() if hasattr(exc, "read") else b""
        return exc.code, dict(exc.headers or {}), data
    except (URLError, socket.timeout, OSError) as exc:
        raise BackendError("Network error talking to %s: %s" % (_host(url), exc))


def _host(url):
    parts = url.split("/")
    return parts[2] if len(parts) > 2 else url


def raise_for_status(status, body):
    """Turn an HTTP error status into a :class:`BackendError` with a readable message."""
    if status < 400:
        return
    if status in (401, 403):
        raise BackendError("Authentication failed (%s). Check your API key. %s"
                           % (status, _error_message(status, body)))
    if status == 402:
        raise BackendError("Out of credits. " + _error_message(status, body))
    if status == 429:
        raise BackendError("Rate limited or out of credits. " + _error_message(status, body))
    raise BackendError(_error_message(status, body))


def http_post(url, body, headers, timeout=180):
    """POST and return ``(status, headers_dict, body_bytes)``; raises BackendError."""
    status, hdrs, data = http_request("POST", url, body, headers, timeout)
    raise_for_status(status, data)
    return status, hdrs, data


def http_json(method, url, payload=None, headers=None, timeout=180):
    """Send JSON (if ``payload`` is not None) and decode a JSON answer; raises BackendError."""
    hdrs = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    status, _, data = http_request(method, url, body, hdrs, timeout)
    raise_for_status(status, data)
    try:
        return json.loads(data.decode("utf-8")) if data else {}
    except ValueError:
        raise BackendError("%s returned an unexpected response." % _host(url))


def download(url, headers=None, timeout=300):
    """GET a result file and return its bytes; raises BackendError."""
    status, _, data = http_request("GET", url, None, headers, timeout)
    if status >= 400:
        raise BackendError("Could not download the result from %s (HTTP %s)."
                           % (_host(url), status))
    return data


def sniff_image(data):
    """File extension for image bytes (``.png``, ``.jpg``, ``.webp``, ``.tif``) or ``None``."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    return None


def check_image(data, provider):
    """Make sure ``data`` is a PNG or JPEG (both read by Nuke and the panel)."""
    ext = sniff_image(data)
    if ext in (".png", ".jpg"):
        return data
    if ext == ".webp":
        raise BackendError("%s returned a WebP image, which Nuke can't read. "
                           "Pick another model." % provider)
    raise BackendError("%s returned something that isn't an image." % provider)
