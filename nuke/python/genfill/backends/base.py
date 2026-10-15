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

USER_AGENT = "GenFill-Nuke/0.8"


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


class BackendError(RuntimeError):
    """Raised with a message suitable for showing an artist."""


class Cancelled(BackendError):
    pass


class GenerationRequest(object):
    def __init__(self, image_path, mask_path, mask_alpha_path, prompt, width, height,
                 seed=0, count=3, negative_prompt="", model="", quality="auto"):
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

    def read(self, attr):
        with open(getattr(self, attr), "rb") as fh:
            return fh.read()


class ModelSpec(object):
    """One entry of a backend's model catalogue.

    ``kind``: ``inpaint`` takes the mask; ``edit`` re-renders the crop from the image (GenFill
    comps it back through the selection); ``erase`` removes the masked content (empty prompt).
    """

    def __init__(self, id, label, kind="inpaint", erase=None, max_count=4, note="", **extra):
        self.id = id
        self.label = label
        self.kind = kind
        self.erase = erase          # model id to use for an empty prompt (None = this one)
        self.max_count = max_count  # images per request
        self.note = note
        self.extra = extra

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

    def model_ids(self):
        """Model ids for the model version menu."""
        return [m.id for m in self.catalogue] or list(self.models)

    def model_spec(self, model=""):
        """The catalogue entry for ``model`` (default model if empty, ad-hoc spec if unknown)."""
        model = (model or self.default_model).strip()
        for m in self.catalogue:
            if m.id == model:
                return m
        return ModelSpec(model, model, kind="edit") if model else None

    def model_size(self, width, height, model=""):
        """Resolution to send for a crop of ``width`` x ``height``."""
        raise NotImplementedError

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
