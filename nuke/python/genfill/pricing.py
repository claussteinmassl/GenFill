"""Cost preview and credit balance for the panel.

Backends answer two questions without ever generating anything (see
:meth:`backends.base.Backend.estimate` and :meth:`backends.base.Backend.balance`):

* what the next Generate would cost (:class:`Quote`), from a provider's preflight tool where
  one exists (Magnific ``simulate_cost``, Higgsfield ``get_cost``, Comfy ``estimate_credits``,
  fal's pricing API) or from the static tables in this module;
* how many credits are left (:class:`Balance`), only for providers whose account has a balance
  a normal key or sign-in can read (Magnific, Higgsfield, Stability).

The panel takes a plain :func:`snapshot` of the settings on the main thread and calls
:func:`request` / :func:`lookup` from its timer; quotes and balances are fetched on worker
threads here and cached (quotes 5 minutes per settings key, balances 60 seconds per backend).
Nothing in this module touches Qt or Nuke.
"""
from __future__ import annotations

import math
import threading
import time

QUOTE_TTL = 300.0
BALANCE_TTL = 60.0
ERROR_TTL = 30.0          # failed quotes / balances are retried sooner (e.g. after a sign-in)

FREE = "free"
CREDITS = "credits"
USD = "USD"


class Quote(object):
    """What a generation is expected to cost.

    Attributes:
        amount: Total for ``count`` images in ``unit``; ``None`` = no figure (see ``note``).
        unit: ``"credits"``, ``"USD"`` or ``"free"``.
        exact: True when the provider quoted this exact request (no "≈").
        note: Short text shown instead of (or after) the figure, e.g. "free (your GPU)".
        count: Number of images the total covers.
    """

    def __init__(self, amount, unit=CREDITS, exact=False, note="", count=1):
        self.amount = None if amount is None else float(amount)
        self.unit = unit
        self.exact = bool(exact)
        self.note = note or ""
        self.count = int(count or 1)

    def text(self):
        """``"≈ 30 credits"``, ``"≈ $0.12 for 3"``, ``"free (your GPU)"``, ``"cost unknown"``."""
        if self.unit == FREE:
            return self.note or "free"
        if self.amount is None:
            return self.note or "cost unknown"
        if self.unit == USD:
            figure = "$%s" % _money(self.amount)
        else:
            figure = "%s credit%s" % (_number(self.amount), "" if self.amount == 1 else "s")
        text = (u"" if self.exact else u"≈ ") + figure
        if self.count > 1:
            text += " for %d" % self.count
        return text

    def __repr__(self):
        return "Quote(%r, %r, exact=%r, count=%r)" % (self.amount, self.unit, self.exact,
                                                       self.count)


class Balance(object):
    """Credits left on the account (``plan`` is the subscription name, may be empty)."""

    def __init__(self, credits, plan="", unit=CREDITS):
        self.credits = float(credits)
        self.plan = plan or ""
        self.unit = unit

    def text(self):
        """``"balance 243,317 (Premium+)"``."""
        text = "balance %s" % (_number(self.credits) if self.unit != USD
                               else "$" + _money(self.credits))
        return text + (" (%s)" % self.plan if self.plan else "")

    def __repr__(self):
        return "Balance(%r, %r)" % (self.credits, self.plan)


def _number(value):
    """``243317`` -> ``"243,317"``; ``0.25`` -> ``"0.25"``."""
    if abs(value - round(value)) < 1e-9:
        return "{:,}".format(int(round(value)))
    return "{:,.2f}".format(value).rstrip("0").rstrip(".")


def _money(value):
    return "%.3f" % value if 0 < value < 0.01 else "%.2f" % value


def per_image(amount, count, unit=CREDITS, exact=True, note=""):
    """A :class:`Quote` for ``count`` images at ``amount`` each (``None`` stays unknown)."""
    if amount is None:
        return Quote(None, unit, False, note, count)
    return Quote(float(amount) * count, unit, exact, note, count)


def megapixels(width, height):
    """Megapixels as image APIs bill them (1024 x 1024 = 1 MP)."""
    return int(width) * int(height) / float(1024 * 1024)


# --------------------------------------------------------------------------
# Static tables
# --------------------------------------------------------------------------

# OpenAI image token prices, USD per 1M tokens: (image input, image output). Research §2.
OPENAI_TOKEN_PRICES = {
    "gpt-image-2": (8.0, 30.0),
    "gpt-image-2.5-flare": (8.0, 30.0),
    "gpt-image-2.5-sunburst": (8.0, 30.0),
    "gpt-image-1.5": (8.0, 32.0),
    "gpt-image-1-mini": (2.5, 8.0),
    "gpt-image-1": (10.0, 40.0),
}
# Output tokens of a 1024 x 1024 image per quality (OpenAI's image guide); other sizes scale
# with the pixel count. ``auto`` is estimated as high (the usual pick for edits).
OPENAI_OUTPUT_TOKENS = {"low": 272, "medium": 1056, "high": 4160}
OPENAI_FIDELITY_TOKENS = 4160     # extra input tokens with input_fidelity "high" (1 MP)


def _image_input_tokens(width, height):
    """Tile-based input tokens: fit 2048, short side 768, 85 + 170 per 512 px tile."""
    w, h = float(width), float(height)
    scale = min(1.0, 2048.0 / max(w, h))
    w, h = w * scale, h * scale
    scale = min(1.0, 768.0 / min(w, h))
    w, h = w * scale, h * scale
    return 85 + 170 * int(math.ceil(w / 512.0)) * int(math.ceil(h / 512.0))


def openai_estimate(model, width, height, quality, count, fidelity=False):
    """``≈`` USD for an OpenAI image edit: the input image once plus ``count`` outputs.

    Returns ``None`` for a model without a known token price.
    """
    prices = OPENAI_TOKEN_PRICES.get((model or "").strip())
    if prices is None:
        return None
    in_price, out_price = prices
    scale = int(width) * int(height) / float(1024 * 1024)
    out_tokens = OPENAI_OUTPUT_TOKENS.get((quality or "auto").lower(),
                                          OPENAI_OUTPUT_TOKENS["high"]) * scale
    in_tokens = 2 * _image_input_tokens(width, height)          # image + mask
    if fidelity:
        in_tokens += OPENAI_FIDELITY_TOKENS * scale
    total = (in_tokens * in_price + count * out_tokens * out_price) / 1e6
    return Quote(total, USD, False, count=count)


STABILITY_CREDITS_PER_CALL = 5        # Stable Image Edit inpaint / erase; 1 credit = $0.01


def _fal_flat(price):
    return lambda w, h, q: price


def _fal_mp(first, extra):
    """``first`` for the first megapixel, ``extra`` per further (started) megapixel."""
    return lambda w, h, q: first + extra * max(0, int(math.ceil(megapixels(w, h))) - 1)


def _fal_ideogram45(w, h, q):
    return {"low": 0.03, "medium": 0.06}.get(q)


def _fal_tiered(low, high):
    """``low`` up to about 1 MP, ``high`` above (1K vs 2K)."""
    return lambda w, h, q: low if megapixels(w, h) <= 1.3 else high


# USD per image from fal's keyless price notes (research §2), used when the pricing API with
# the artist's key does not answer. ``fn(width, height, quality) -> price or None``.
FAL_PRICES = {
    "fal-ai/nano-banana-pro/edit": _fal_flat(0.15),
    "fal-ai/nano-banana-2/edit": _fal_flat(0.08),
    "fal-ai/nano-banana/edit": _fal_flat(0.039),
    "ideogram/v4.5/edit": _fal_ideogram45,
    "fal-ai/ideogram/v3/edit": _fal_flat(0.09),         # GenFill sends rendering_speed QUALITY
    "fal-ai/flux-pro/v1/erase": _fal_mp(0.03, 0.004),
    "fal-ai/finegrain-eraser/mask": _fal_flat(0.04),     # mode "standard"
    "alibaba/qwen-image-3/edit": _fal_tiered(0.04, 0.075),
    "fal-ai/flux-2-pro/edit": _fal_mp(0.03, 0.015),
    "xai/grok-imagine-image/v2.0/edit": _fal_tiered(0.04, 0.08),
}
# fal endpoints priced per token like OpenAI's API: endpoint -> OpenAI model id.
FAL_TOKEN_MODELS = {
    "openai/gpt-image-2/edit": "gpt-image-2",
    "openai/gpt-image-2.5/flare/edit": "gpt-image-2.5-flare",
    "openai/gpt-image-2.5/sunburst/edit": "gpt-image-2.5-sunburst",
    "fal-ai/gpt-image-1.5/edit": "gpt-image-1.5",
}


def fal_static(endpoint, width, height, quality, count):
    """``≈`` USD from :data:`FAL_PRICES` / :data:`FAL_TOKEN_MODELS`, or ``None``."""
    if endpoint in FAL_TOKEN_MODELS:
        return openai_estimate(FAL_TOKEN_MODELS[endpoint], width, height,
                               quality if quality in ("low", "medium", "high") else "high",
                               count)
    fn = FAL_PRICES.get(endpoint)
    price = fn(width, height, (quality or "auto").lower()) if fn else None
    if price is None:
        return None
    return per_image(price, count, USD, exact=False)


def fal_pricing(info, endpoint, width, height, count):
    """Total USD from a fal pricing API answer, or ``None`` if its shape is not understood.

    Expected (unverified) shape: ``{"prices": [{"endpoint_id", "unit_price", "unit",
    "currency"}]}``; also accepts a bare list or a single price object, ``price`` for
    ``unit_price`` and units naming images or megapixels.
    """
    entries = info.get("prices") if isinstance(info, dict) else info
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        entries = [info] if isinstance(info, dict) else []
    entry = None
    for e in entries:
        if isinstance(e, dict) and e.get("endpoint_id") in (None, endpoint):
            entry = e
            if e.get("endpoint_id") == endpoint:
                break
    if entry is None:
        return None
    price = entry.get("unit_price", entry.get("price"))
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    currency = str(entry.get("currency") or "USD").upper()
    if currency != "USD":
        return None
    unit = str(entry.get("unit") or "").lower()
    if "megapixel" in unit or unit in ("mp", "mps"):
        each = price * max(1, int(math.ceil(megapixels(width, height))))
    elif "image" in unit or unit in ("request", "requests", "call"):
        each = price
    else:
        return None
    return Quote(each * count, USD, False, count=count)


# --------------------------------------------------------------------------
# Worker + cache (used by the panel)
# --------------------------------------------------------------------------

_lock = threading.RLock()
_quotes = {}       # settings key -> {"quote", "error", "login", "when"}
_balances = {}     # backend id -> {"balance", "error", "when"}
_running = set()   # settings keys being fetched
_balance_running = set()


def snapshot(backend, model, quality, resolution, count, width, height, prompt, api_key):
    """Plain dict of everything a quote depends on (take it on the main thread)."""
    return {"backend": backend, "model": (model or "").strip(), "quality": quality or "auto",
            "resolution": resolution or "auto", "count": max(1, int(count or 1)),
            "width": int(width), "height": int(height), "prompt": prompt or "",
            "api_key": api_key or ""}


def key(snap):
    """Cache key of a snapshot: the settings that change the price (not the prompt text)."""
    b = snap["backend"]
    return (getattr(b, "id", b), snap["model"], snap["quality"], snap["resolution"],
            snap["count"], snap["width"], snap["height"], bool(snap["prompt"].strip()))


def _fresh(entry, ttl, now):
    if not entry:
        return False
    limit = ERROR_TTL if entry.get("error") else ttl
    return now - entry["when"] < limit


def request(snap, now=None, threaded=True):
    """Fetch quote and balance for ``snap`` unless fresh in the cache or already running."""
    now = time.time() if now is None else now
    backend = snap["backend"]
    k = key(snap)
    jobs = []
    with _lock:
        if k not in _running and not _fresh(_quotes.get(k), QUOTE_TTL, now):
            _running.add(k)
            jobs.append((_fetch_quote, (snap, k)))
        if (backend.id not in _balance_running
                and not _fresh(_balances.get(backend.id), BALANCE_TTL, now)):
            _balance_running.add(backend.id)
            jobs.append((_fetch_balance, (backend, snap["api_key"])))
    for fn, args in jobs:
        if threaded:
            t = threading.Thread(target=fn, args=args, name="GenFill cost (%s)" % backend.id)
            t.daemon = True
            t.start()
        else:
            fn(*args)
    return bool(jobs)


def _fetch_quote(snap, k):
    from .backends.base import GenerationRequest
    from .backends.oauth import LoginRequired
    entry = {"quote": None, "error": "", "login": False}
    try:
        req = GenerationRequest(None, None, None, snap["prompt"], snap["width"], snap["height"],
                                count=snap["count"], model=snap["model"],
                                quality=snap["quality"], resolution=snap["resolution"])
        entry["quote"] = snap["backend"].estimate(req, snap["api_key"])
    except LoginRequired as exc:
        entry.update(error=str(exc) or "not signed in", login=True)
    except Exception as exc:   # never let a quote break anything
        entry["error"] = str(exc) or exc.__class__.__name__
        _log("Cost estimate for %s failed: %s", k[0], entry["error"])
    entry["when"] = time.time()
    with _lock:
        _quotes[k] = entry
        _running.discard(k)


def _fetch_balance(backend, api_key):
    entry = {"balance": None, "error": ""}
    try:
        entry["balance"] = backend.balance(api_key)
    except Exception as exc:   # hidden in the panel; logged at debug level
        entry["error"] = str(exc) or exc.__class__.__name__
    entry["when"] = time.time()
    with _lock:
        _balances[backend.id] = entry
        _balance_running.discard(backend.id)


def forget_balance(backend_id):
    """Drop the cached balance (after a generation spent credits)."""
    with _lock:
        _balances.pop(backend_id, None)


def reset():
    with _lock:
        _quotes.clear()
        _balances.clear()


def lookup(snap):
    """``(text, tooltip)`` for the panel's cost line from the cache (never blocks)."""
    k = key(snap)
    with _lock:
        q = _quotes.get(k)
        busy = k in _running
        b = _balances.get(snap["backend"].id)
    if q is None:
        return (u"estimating cost…" if busy else ""), ""
    if q.get("login"):
        text, tip = "sign in to see cost", q["error"]
    elif q.get("error"):
        text, tip = "cost unknown", q["error"]
    elif q.get("quote") is None:
        text, tip = "cost unknown", "This model has no known price."
    else:
        quote = q["quote"]
        text = quote.text()
        tip = quote.note if quote.note and quote.note != text else ""
        if not quote.exact and quote.unit != FREE and quote.amount is not None:
            tip = (tip + "\n" if tip else "") + "Estimate; the provider bills the actual usage."
    if b and b.get("balance") is not None:
        text += u" · " + b["balance"].text()
    return text, tip


def _log(msg, *args):
    try:
        from . import log as gflog
        gflog.debug(msg, *args)
    except Exception:
        pass
