"""fal.ai image models through the REST queue API (``queue.fal.run``).

One generic request builder drives every endpoint; the per-endpoint differences (image and
mask field names, prompt field, how many images one call returns, seed and size support) live
in the ``ModelSpec.extra`` of the catalogue below.

Flow per Generate click: upload the crop and the mask once to the fal CDN (v3 CDN, then the
storage initiate/PUT flow, then data URIs as a last resort), submit one queue request per
batch of variations, poll every ``status_url``, fetch every ``response_url`` and download the
result images. An empty prompt switches an inpaint model to its removal endpoint.

Paid results follow the shared policy in ``cloud``: a failed request or download only drops
that variation; when GenFill gives up (error, timeout, cancel) it PUTs ``cancel_url`` for every
request still running; transient poll errors (5xx, 429, network) are retried a few times.
"""
from __future__ import annotations

import base64
import json
import math
import random
import re
import time

from . import base, layout
from .base import Backend, BackendError, Cancelled, ModelSpec, tiers_from
from .cloud import collect, finish, ref_prompt
from .. import pngio, pricing, region

QUEUE_ROOT = "https://queue.fal.run"
REST_ROOT = "https://rest.fal.ai"
CDN_UPLOAD_URL = "https://v3.fal.media/files/upload"

POLL_INTERVAL = 1.0
TIMEOUT = 900.0          # give up on a request after 15 minutes
MASK_THRESHOLD = 32      # binarize the soft mask a little generously (slightly dilated)
MAX_SEED = 2147483646
STATUS_RETRIES = 3       # tolerated consecutive transient errors (5xx, 429, network) per job

# Mask models that require a prompt get this when the artist leaves it empty and the model has
# no removal endpoint of its own.
REMOVAL_PROMPT = ("empty background, seamlessly continue the surrounding scene and texture, "
                  "no objects, no people, no text")
MASK_NOTE = ("Image 2 is a black and white mask of image 1: only change the white area. ")

DEFAULT_MODEL = "fal-ai/flux-pro/v1/fill"
ERASE_MODEL = "fal-ai/flux-pro/v1/erase"

# ModelSpec extra keys (all optional; defaults in parentheses):
#   image_field ("image_urls")  "image_url" (string) or "image_urls" (list)
#   mask_field (None)           field name for the mask URL, None = the model takes no mask
#   mask_invert (False)         the model wants black = edit, white = keep (GenFill: white = fill)
#   mask_as_image (False)       reference edits: append the mask as the second input image
#   prompt_field ("prompt")     "prompt", "instruction" or None (no prompt at all)
#   prompt_required (True)      False: an empty prompt is omitted instead of replaced
#   negative_field (None)       field for the negative prompt
#   count_field (None)          "num_images"; None = one image per request (N requests)
#   seed_field (None), seed_max (None)
#   size (None)                 "obj": image_size {"width", "height"} from the request;
#                               "wh": top-level width/height
#   aspect_field (None)         field fed with the crop's aspect ratio ("w:h"), snapped to
#   aspect_ratios (None)        ... the nearest of these when the model has an enum
#   output_format (True)        send output_format "png"
#   quality_field (None)        field fed from the Quality setting (low/medium/high)
#   budget / multiple           pixel budget and grid for model_size (auto resolution)
#   fixed ({})                  constant fields merged into the body
#   resolutions (_FILL)         tiers of the Resolution setting the model can run
#   resolution_field (None)     field that takes the tier ("resolution", "image_size") ...
#   resolution_values ({})      ... with these values per tier ({"2K": "2K"}); overrides fixed
#   min_pixels / max_pixels     pixel limits of a tier size; tier_max_side: longest side
#   layout (None)               region prompt style (layout.STYLES): "flux3" / "ideogram" put
#                               the boxes into the prompt, "flux3_fields" sends bounding_boxes
#   expansion_field (None)      boolean prompt-expansion field, set False when Ideogram or
#                               native (flux3_fields) boxes go
_ALL = ("1K", "2K", "4K")
_FILL = ("1K", "2K")     # mask models answer at the input size; 4 MP is the safe ceiling
_MASK = dict(image_field="image_url", mask_field="mask_url", count_field="num_images",
             seed_field="seed")
_REF = dict(image_field="image_urls", mask_as_image=True)
_GPT_EDIT = dict(image_field="image_urls", mask_field="mask_url", count_field="num_images",
                 size="obj", quality_field="quality", budget=1536 * 1024,
                 fixed={"quality": "high"}, resolutions=_ALL, min_pixels=655360,
                 max_pixels=3840 * 2160, tier_max_side=3840)


def _res(field, values):
    """Extra keys for a model that takes the tier as ``field``: ``{"1K": "1K", ...}``."""
    return dict(resolution_field=field, resolution_values=dict(values),
                resolutions=tuple(t for t in _ALL if t in values))


_NB_RES = _res("resolution", {"1K": "1K", "2K": "2K", "4K": "4K"})

CATALOGUE = (
    ModelSpec("fal-ai/flux-pro/v1/fill", "FLUX.1 Fill [pro]", kind="inpaint", erase=ERASE_MODEL,
              max_count=4, note="Default. True mask inpainting at input size.",
              fixed={"safety_tolerance": "5"}, **_MASK),
    ModelSpec(ERASE_MODEL, "FLUX Erase", kind="erase", max_count=1,
              note="Object removal (default for an empty prompt); one image per request.",
              image_field="image_url", mask_field="mask_url", prompt_field=None,
              fixed={"dilate_pixels": 10}),
    ModelSpec("fal-ai/ideogram/v3/edit", "Ideogram v3 Edit", kind="inpaint", max_count=8,
              note="Mask fill, good with text and graphics.", output_format=False,
              fixed={"rendering_speed": "QUALITY", "expand_prompt": False}, **_MASK),
    ModelSpec("ideogram/v4.5/edit", "Ideogram 4.5 Edit", kind="inpaint", max_count=8,
              note="Newest Ideogram mask fill, good with text and graphics.",
              output_format=False, mask_invert=True, quality_field="quality",
              max_pixels=2048 * 2048, layout="ideogram", **_MASK),
    ModelSpec("openai/gpt-image-2/edit", "GPT Image 2 (fal)", kind="inpaint", max_count=4,
              note="Strong instruction following; mask edit.", **_GPT_EDIT),
    ModelSpec("openai/gpt-image-2.5/flare/edit", "GPT Image 2.5 Flare (fal)", kind="inpaint",
              max_count=4, note="Newest GPT Image, faster tier; mask edit.", **_GPT_EDIT),
    ModelSpec("openai/gpt-image-2.5/sunburst/edit", "GPT Image 2.5 Sunburst (fal)",
              kind="inpaint", max_count=4, note="Newest GPT Image, higher quality; mask edit.",
              **_GPT_EDIT),
    ModelSpec("fal-ai/qwen-image-edit/inpaint", "Qwen Image Edit Inpaint", kind="inpaint",
              max_count=4, note="Cheap open-model mask inpaint.", size="obj",
              negative_field="negative_prompt", fixed={"strength": 0.93}, **_MASK),
    ModelSpec("fal-ai/flux-lora-fill", "FLUX.1 Fill [dev]", kind="inpaint", max_count=4,
              note="Cheap; prompt optional.", prompt_required=False,
              fixed={"resize_to_original": True, "paste_back": True}, **_MASK),
    ModelSpec("fal-ai/onereward", "OneReward (fill and removal)", kind="inpaint", max_count=4,
              note="Mask fill and removal with seeds; prompt optional.", prompt_required=False,
              **_MASK),
    ModelSpec("fal-ai/bria/genfill", "Bria GenFill", kind="inpaint", max_count=4,
              note="Commercially safe (licensed training data).", output_format=False,
              negative_field="negative_prompt", **_MASK),
    ModelSpec("fal-ai/finegrain-eraser/mask", "Finegrain Eraser", kind="erase", max_count=1,
              note="Premium removal incl. shadows and reflections; one image per request.",
              image_field="image_url", mask_field="mask_url", prompt_field=None,
              seed_field="seed", seed_max=999, output_format=False, fixed={"mode": "standard"}),
    ModelSpec("google/nano-banana-2.1/edit", "Nano Banana 2.1 (reference edit)", kind="edit",
              max_count=4, note="Re-renders the crop; no mask input.", count_field="num_images",
              seed_field="seed", fixed={"resolution": "1K", "aspect_ratio": "auto"},
              **dict(_REF, **_NB_RES)),
    ModelSpec("fal-ai/nano-banana-2/edit", "Nano Banana 2 (reference edit)", kind="edit",
              max_count=4, note="Re-renders the crop; no mask input.", count_field="num_images",
              seed_field="seed", fixed={"resolution": "1K", "aspect_ratio": "auto"},
              **dict(_REF, **_NB_RES)),
    ModelSpec("fal-ai/nano-banana-pro/edit", "Nano Banana Pro (reference edit)", kind="edit",
              max_count=4, note="Re-renders the crop; no mask input.", count_field="num_images",
              seed_field="seed", fixed={"resolution": "1K", "aspect_ratio": "auto"},
              **dict(_REF, **_NB_RES)),
    ModelSpec("blackforestlabs/flux-3/edit-image", "FLUX.3 Edit (reference edit)", kind="edit",
              max_count=1, note="Newest BFL edit model; one image per request.",
              fixed={"resolution": "1k", "aspect_ratio": "auto"}, layout="flux3",
              **dict(_REF, **_res("resolution", {"1K": "1k", "2K": "2k", "4K": "4k"}))),
    ModelSpec("blackforestlabs/flux-3/klein/edit-image", "FLUX.3 Klein Edit (reference edit)",
              kind="edit", max_count=4, note="Fast BFL edit model; region boxes as a native "
              "field.", count_field="num_images", seed_field="seed", size="obj",
              resolutions=_ALL, max_pixels=4096 * 4096, tier_max_side=4096,
              layout="flux3_fields", expansion_field="enable_prompt_expansion", **_REF),
    ModelSpec("bytedance/seedream/v5/pro/edit", "Seedream 5 Pro Edit (reference edit)",
              kind="edit", max_count=6, note="Region-precise reference edit.",
              count_field="num_images", fixed={"image_size": "auto_1K"},
              **dict(_REF, **_res("image_size", {"1K": "auto_1K", "2K": "auto_2K"}))),
    ModelSpec("bytedance/seedream/v5/flash/edit", "Seedream 5 Flash Edit (reference edit)",
              kind="edit", max_count=6, note="Fast Seedream reference edit.",
              count_field="num_images", fixed={"image_size": "auto_1K"},
              **dict(_REF, **_res("image_size", {"1K": "auto_1K", "2K": "auto_2K"}))),
    ModelSpec("bytedance/seedream/v5/lite/edit", "Seedream 5 Lite Edit (reference edit)",
              kind="edit", max_count=6, note="Renders at 2K or more (its minimum size).",
              count_field="num_images", output_format=False,
              fixed={"image_size": "auto_2K"},
              **dict(_REF, **_res("image_size", {"2K": "auto_2K", "4K": "auto_4K"}))),
    ModelSpec("xai/grok-imagine-image/v2.0/edit", "Grok Imagine 2.0 Edit (reference edit)",
              kind="edit", max_count=4, note="xAI reference edit; no mask input.",
              count_field="num_images", fixed={"resolution": "1k", "aspect_ratio": "auto"},
              **dict(_REF, **_res("resolution", {"1K": "1k", "2K": "2k"}))),
)


class FalBackend(Backend):
    id = "fal"
    label = "fal.ai"
    env_var = "FAL_KEY"
    default_model = DEFAULT_MODEL
    catalogue = CATALOGUE
    description = ("Many image models behind one key: mask inpainting, removal and "
                   "reference edits.")
    discoverable = True
    default_resolutions = _FILL

    def discover(self, api_key=""):
        """Live catalogue from fal's public model API (no key needed; sent if set).

        Lists the image-to-image category (most used first), keeps edit/inpaint endpoints that
        GenFill doesn't curate, then reads their OpenAPI input schemas in batches of ten.
        """
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = "Key %s" % api_key
        listed, cursor = [], None
        for page in range(MODELS_PAGES):
            url = "%s?category=image-to-image&limit=100" % MODELS_API
            if cursor:
                url += "&cursor=%s" % _quote(cursor)
            info = _models_get(url, headers)
            listed += [m for m in info.get("models") or [] if isinstance(m, dict)]
            cursor = info.get("next_cursor")
            if not info.get("has_more") or not cursor:
                break
            time.sleep(MODELS_PAUSE)
        curated = set(m.id for m in self.catalogue)
        ids = [m["endpoint_id"] for m in listed
               if _candidate(m) and m["endpoint_id"] not in curated]
        ids = ids[:MODELS_CANDIDATES]
        expanded = []
        for i in range(0, len(ids), 10):
            time.sleep(MODELS_PAUSE)
            query = "&".join("endpoint_id=%s" % _quote(e) for e in ids[i:i + 10])
            try:
                info = _models_get("%s?expand=openapi-3.0&%s" % (MODELS_API, query), headers)
            except BackendError as exc:
                if not expanded:
                    raise
                _log_warning("fal.ai model discovery stopped early: %s", exc)
                break
            expanded += [m for m in info.get("models") or [] if isinstance(m, dict)]
        return [s for s in (spec_from_openapi(m) for m in expanded) if s is not None]

    # ------------------------------------------------------------------ sizes

    def model_size(self, width, height, model="", resolution="auto"):
        x = self.model_spec(model).extra if self.model_spec(model) else {}
        tier, _ = self.resolution_plan(model, resolution)
        multiple = x.get("multiple", 16)
        if tier == "auto":
            return region.scale_to_pixel_budget(width, height, x.get("budget", 1024 * 1024),
                                                multiple, min_side=256, max_side=2560,
                                                max_aspect=3.0)
        budget = self.tier_budget(width, height, model, resolution, x.get("budget", 1024 * 1024))
        w, h = region.scale_to_pixel_budget(width, height, budget, multiple, min_side=256,
                                            max_side=x.get("tier_max_side"), max_aspect=3.0)
        return region.clamp_pixels(w, h, multiple, x.get("min_pixels"), x.get("max_pixels"))

    # ------------------------------------------------------------------- cost

    def estimate(self, request, api_key=""):
        """USD for ``count`` images: fal's pricing API with the artist's key, else the
        keyless price notes in :mod:`pricing`; None (cost unknown) for unpriced models.
        fal has no balance API for normal keys."""
        spec = self.model_spec(request.model)
        if not request.prompt.strip() and spec.erase and not self.sends_boxes(request, spec.id):
            spec = self.model_spec(spec.erase)
        count = max(1, int(request.count))
        if api_key:
            try:
                status, _, data = base.http_request(
                    "GET", "%s?endpoint_id=%s" % (PRICING_API, _quote(spec.id)), None,
                    {"Authorization": "Key %s" % api_key, "Accept": "application/json"}, 20)
                info = _json_or_none(data) if status < 400 else None
                quote = pricing.fal_pricing(info, spec.id, request.width, request.height,
                                            count) if info is not None else None
                if quote is not None:
                    return quote
            except BackendError:
                pass
        return pricing.fal_static(spec.id, request.width, request.height,
                                  request.quality if request.quality in ("low", "medium", "high")
                                  else (spec.extra.get("fixed") or {}).get("quality", "auto"),
                                  count)

    # --------------------------------------------------------------- generate

    def generate(self, request, api_key, progress=None, cancelled=None):
        if not api_key:
            raise BackendError("No fal.ai API key set (FAL_KEY or API Keys & Accounts...).")
        spec = self.model_spec(request.model)
        prompt = request.prompt.strip()
        if not prompt and spec.erase and not self.sends_boxes(request, spec.id):
            spec = self.model_spec(spec.erase)
        headers = {"Authorization": "Key %s" % api_key}

        def check():
            if cancelled and cancelled():
                raise Cancelled("Cancelled")

        check()
        if progress:
            progress(0.02, "Uploading the plate to fal.ai...")
        urls = self._upload_inputs(request, spec, headers, check)

        plan = _plan(spec, request)
        jobs, errors = [], []
        try:
            for i, (n, seed) in enumerate(plan):
                check()
                if progress:
                    progress(0.1 + 0.1 * i / len(plan), "Submitting request %d of %d to %s..."
                             % (i + 1, len(plan), spec.id))
                body = build_payload(spec, request, prompt, urls, n, seed,
                                     tier=self.request_tier(request, spec.id),
                                     style=self.layout_style(spec.id))
                try:
                    jobs.append(self._submit(spec.id, body, headers))
                except Cancelled:
                    raise
                except BackendError as exc:
                    if not jobs:
                        raise
                    errors.append(str(exc))   # keep the requests already queued
                    break
            results = self._wait_all(jobs, headers, progress, check, errors)
        except Exception:   # Cancelled or anything unexpected: stop paying for what still runs
            self._cancel(jobs, headers)
            raise
        if not results and not errors:
            raise BackendError("fal.ai's content filter blocked every result. "
                               "Try a different prompt.")
        return finish(results, errors, "fal.ai")[:request.count]

    # ----------------------------------------------------------------- inputs

    def _upload_inputs(self, request, spec, headers, check):
        mask = None
        if spec.extra.get("mask_field") or spec.extra.get("mask_as_image"):
            mask = _binarize(request.read("mask_path"), spec.extra.get("mask_invert", False))
        uploader = _Uploader(headers)
        urls = {"image": uploader.upload("genfill_plate.png", request.read("image_path"))}
        check()
        if mask is not None:
            urls["mask"] = uploader.upload("genfill_mask.png", mask)
        return urls

    # ------------------------------------------------------------------ queue

    def _submit(self, endpoint, body, headers):
        hdrs = dict(headers)
        hdrs.update({"Content-Type": "application/json", "Accept": "application/json"})
        status, _, data = base.http_request("POST", "%s/%s" % (QUEUE_ROOT, endpoint),
                                            json.dumps(body).encode("utf-8"), hdrs, 120)
        _raise_fal(status, data)
        info = _json(data)
        rid = info.get("request_id")
        if not rid:
            raise BackendError("fal.ai did not accept the request (no request id).")
        urls = _queue_urls(endpoint, rid)
        return {"id": rid, "status_url": info.get("status_url") or urls[0],
                "response_url": info.get("response_url") or urls[1],
                "cancel_url": info.get("cancel_url") or urls[2], "done": False, "fails": 0,
                "images": []}

    def _wait_all(self, jobs, headers, progress, check, errors):
        """Poll every job until all are done; returns the images of the successful ones.

        A failed job adds its message to ``errors``; on the overall timeout the jobs still
        running are cancelled and the finished images are returned.
        """
        start = time.monotonic()
        while not all(j["done"] for j in jobs):
            check()
            for job in jobs:
                if not job["done"]:
                    self._poll(job, headers, errors)
            finished = sum(1 for j in jobs if j["done"])
            if progress:
                progress(0.2 + 0.7 * finished / len(jobs), "Waiting for fal.ai (%d of %d done)..."
                         % (finished, len(jobs)))
            if all(j["done"] for j in jobs):
                break
            if time.monotonic() - start > TIMEOUT:
                self._cancel(jobs, headers)
                errors.append("fal.ai took too long (over %d minutes). Try again or pick "
                              "a faster model." % int(TIMEOUT // 60))
                break
            time.sleep(POLL_INTERVAL)
        results = []
        for job in jobs:
            results.extend(job["images"])
        return results

    def _poll(self, job, headers, errors):
        """One status check; a job that fails for good is marked done with its error."""
        try:
            self._poll_once(job, headers, errors)
            job["fails"] = 0
        except Cancelled:
            raise
        except _Transient as exc:
            job["fails"] += 1
            if job["fails"] > STATUS_RETRIES:
                job["done"] = True
                errors.append(str(exc))
        except BackendError as exc:
            job["done"] = True
            errors.append(str(exc))

    def _poll_once(self, job, headers, errors):
        url = job["status_url"]
        url += ("&" if "?" in url else "?") + "logs=0"
        status, _, data = _get(url, headers, 60)
        _raise_fal(status, data)
        info = _json(data)
        if info.get("status") != "COMPLETED":
            return
        if info.get("error"):
            raise BackendError(_failed_text(info.get("error"), info.get("error_type")))
        status, _, data = _get(job["response_url"], headers, 120)
        _raise_fal(status, data)
        job["images"] = _fetch_images(_json(data), errors)
        job["done"] = True

    def _cancel(self, jobs, headers):
        for job in jobs:
            if job["done"]:
                continue
            try:
                base.http_request("PUT", job["cancel_url"], None, headers, 15)
            except BackendError:
                pass


# --------------------------------------------------------------------------
# Discovery (public model API)
# --------------------------------------------------------------------------

MODELS_API = "https://api.fal.ai/v1/models"
PRICING_API = "https://api.fal.ai/v1/models/pricing"
MODELS_PAGES = 2          # most used first; two pages of 100 cover the relevant edit models
MODELS_CANDIDATES = 30    # OpenAPI lookups (10 per call; the keyless API rate-limits)
MODELS_PAUSE = 1.0
_CANDIDATE = re.compile(r"edit|inpaint|fill|eras|remov")
_NOT_A_FILL = re.compile(r"lora|gallery|background|image-editing/|image-apps|try-on|angles|"
                         r"upscal|relight|restor|restyle|coloriz|reseason|sketch|blend|expand|"
                         r"reframe|rewrite|text-removal|bbox|by_text|product")
# Boolean prompt-expansion fields (turned off when Ideogram or native region boxes are sent).
_EXPANSION = ("enable_prompt_expansion", "expand_prompt")
# Input fields GenFill fills; an endpoint that requires anything else is skipped.
_FILLED = ("prompt", "image_url", "image_urls", "mask_url", "mask_image_url")


def _candidate(model):
    eid = str(model.get("endpoint_id") or "")
    status = str((model.get("metadata") or {}).get("status") or "active")
    return bool(eid) and status == "active" and bool(_CANDIDATE.search(eid)) \
        and not _NOT_A_FILL.search(eid)


def _input_schema(openapi):
    """Properties and required list of an endpoint's POST body (``({}, [])`` if unknown)."""
    if not isinstance(openapi, dict):
        return {}, []
    schemas = (openapi.get("components") or {}).get("schemas") or {}
    for path, item in (openapi.get("paths") or {}).items():
        post = item.get("post") if isinstance(item, dict) else None
        if not post or "/requests" in path:
            continue
        schema = (((post.get("requestBody") or {}).get("content") or {})
                  .get("application/json") or {}).get("schema") or {}
        ref = schema.get("$ref")
        if ref:
            schema = schemas.get(ref.split("/")[-1]) or {}
        return schema.get("properties") or {}, list(schema.get("required") or [])
    return {}, []


def _enum(prop):
    """Every enum value of a schema property, including those inside ``anyOf``."""
    values = list(prop.get("enum") or []) if isinstance(prop, dict) else []
    for sub in (prop.get("anyOf") or []) if isinstance(prop, dict) else []:
        if isinstance(sub, dict):
            values += list(sub.get("enum") or [])
    return [str(v) for v in values]


def _near(colour, verbs):
    """``colour`` followed by one of ``verbs`` within five words of the same clause."""
    return re.compile(r"\b%s\b(?:[ \t]+[\w()]+){0,5}?[ \t]+(?:%s)" % (colour, verbs))


_CHANGE = r"edit|inpaint|fill|replac|chang|regenerat|remov|repaint"
_KEEP = r"preserv|kept|keep|unchanged|untouched|protect"
_BLACK_EDITS, _WHITE_EDITS = _near("black", _CHANGE), _near("white", _CHANGE)
_BLACK_KEEPS, _WHITE_KEEPS = _near("black", _KEEP), _near("white", _KEEP)


def _mask_inverted(prop):
    """True if the schema says black marks the area to change (GenFill: white = fill).

    "Black edits, white preserves" and "white areas are preserved, black areas are filled"
    are inverted; anything that says white changes or black is kept, or says nothing, is not.
    """
    text = str((prop or {}).get("description") or "").lower()
    inverted = _BLACK_EDITS.search(text) or _WHITE_KEEPS.search(text)
    normal = _WHITE_EDITS.search(text) or _BLACK_KEEPS.search(text)
    return bool(inverted) and not normal


def _accepts_size_object(prop, schemas):
    """True if a property (e.g. ``image_size``) takes ``{"width", "height"}``."""
    if not isinstance(prop, dict):
        return False
    for sub in [prop] + list(prop.get("anyOf") or []) + list(prop.get("oneOf") or []):
        if not isinstance(sub, dict):
            continue
        if sub.get("$ref"):
            name = sub["$ref"].split("/")[-1]
            if name not in schemas and name.endswith("ImageSize"):
                return True   # fal's shared {width, height} schema, not included
            sub = schemas.get(name) or {}
        props = sub.get("properties") or {}
        if "width" in props and "height" in props:
            return True
    return False


def _size_extra(props, schemas, masked):
    """Extra keys that make the model answer in the crop's aspect, or ``None`` if it can't.

    Mask models return the input geometry by themselves; they only get ``image_size: auto``
    when the schema offers it (custom sizes can conflict with a mask, e.g. Ideogram 4.5) or
    else the exact size. Reference edits need an exact size (``image_size`` object, or
    ``width``/``height``) or an aspect ratio (free ``"w:h"`` text or the nearest enum value).
    """
    size = props.get("image_size")
    if masked:
        if "auto" in _enum(size):
            return {"fixed": {"image_size": "auto"}}
        return {"size": "obj"} if _accepts_size_object(size, schemas) else {}
    if _accepts_size_object(size, schemas):
        return {"size": "obj"}
    if "width" in props and "height" in props:
        return {"size": "wh"}
    aspect = props.get("aspect_ratio")
    if isinstance(aspect, dict):
        ratios = _ratios(_enum(aspect))
        if ratios:
            return {"aspect_field": "aspect_ratio", "aspect_ratios": ratios}
        subs = [aspect] + [a for a in aspect.get("anyOf") or [] if isinstance(a, dict)]
        free = any(a.get("type", "string") == "string" and not a.get("enum")
                   and not a.get("anyOf") for a in subs)
        if free:
            return {"aspect_field": "aspect_ratio"}   # free "w:h" text
    return None


def _resolution_extra(props, extra, masked):
    """Resolution-setting keys for a discovered model (see the catalogue key list).

    A ``resolution`` enum (or ``image_size`` ``auto_2K``-style presets) carries the tier; an
    exact size (``image_size`` object, ``width``/``height``) takes any tier; a mask model
    answers at the input size (up to 2K); an aspect-ratio-only model has a fixed size.
    """
    for field in ("resolution", "image_size"):
        values = _enum(props.get(field))
        mapping = {}
        for v in values:
            tier = tiers_from([v])
            if tier and tier[0] not in mapping and (field == "resolution"
                                                    or v.lower().startswith("auto_")):
                mapping[tier[0]] = v
        if mapping:
            return _res(field, mapping)
    if extra.get("size") in ("obj", "wh") and not masked:
        return {"resolutions": _ALL}
    return {"resolutions": _FILL if masked else ()}


def _ratios(values):
    out = []
    for v in values:
        parts = v.split(":")
        try:
            if len(parts) == 2 and float(parts[0]) > 0 and float(parts[1]) > 0:
                out.append(v)
        except ValueError:
            pass
    return out


def aspect_value(spec, width, height):
    """The ``aspect_ratio`` to send: nearest enum value, or the crop's own reduced ratio."""
    ratios = spec.extra.get("aspect_ratios")
    if ratios:
        return region.closest_size(width, height, region.aspect_sizes(ratios, 1024 * 1024))[2]
    w, h = int(width), int(height)
    g = math.gcd(w, h) or 1
    return "%d:%d" % (w // g, h // g)


def spec_from_openapi(model):
    """A :class:`ModelSpec` for one expanded model, or ``None`` if GenFill can't drive it.

    Maps the input schema onto the curated ``extra`` keys: ``image_url``/``image_urls`` input,
    ``mask_url`` (or ``mask_image_url``) makes it a mask model, plus prompt, negative prompt,
    ``num_images``, ``seed``, ``output_format`` and ``quality`` when the schema has them.
    """
    eid = str(model.get("endpoint_id") or "")
    props, required = _input_schema(model.get("openapi"))
    schemas = ((model.get("openapi") or {}).get("components") or {}).get("schemas") or {}
    if not eid or not props or any(r not in _FILLED for r in required):
        return None
    if "image_url" in props:
        image_field = "image_url"
    elif "image_urls" in props:
        image_field = "image_urls"
    else:
        return None
    mask_field = next((f for f in ("mask_url", "mask_image_url") if f in props), None)
    has_prompt = "prompt" in props
    if not mask_field and not has_prompt:
        return None   # a reference edit needs an instruction
    meta = model.get("metadata") or {}
    label = str(meta.get("display_name") or eid).strip()
    extra = {"image_field": image_field}
    if mask_field:
        kind = "inpaint" if has_prompt else "erase"
        extra["mask_field"] = mask_field
        if _mask_inverted(props[mask_field]):
            extra["mask_invert"] = True
    else:
        kind = "edit"
        label += " (reference edit)"
        if image_field == "image_urls":
            extra["mask_as_image"] = True
    if has_prompt:
        extra["prompt_required"] = "prompt" in required
    else:
        extra["prompt_field"] = None
    if "negative_prompt" in props:
        extra["negative_field"] = "negative_prompt"
    expansion = next((f for f in _EXPANSION if (props.get(f) or {}).get("type") == "boolean"),
                     None)
    if expansion:
        extra["expansion_field"] = expansion
    max_count = 1
    count = props.get("num_images")
    if isinstance(count, dict):
        extra["count_field"] = "num_images"
        try:
            max_count = max(1, min(8, int(count.get("maximum") or 4)))
        except (TypeError, ValueError):
            max_count = 4
    if "seed" in props:
        extra["seed_field"] = "seed"
    formats = _enum(props.get("output_format"))
    extra["output_format"] = "output_format" in props and (not formats or "png" in formats)
    qualities = _enum(props.get("quality"))
    if all(q in qualities for q in ("low", "medium", "high")):
        extra["quality_field"] = "quality"
    sizing = _size_extra(props, schemas, bool(mask_field))
    if sizing is None:
        return None   # the model picks its own size: a stretched, paid result
    fixed = dict(extra.get("fixed") or {}, **sizing.pop("fixed", {}))
    if fixed:
        extra["fixed"] = fixed
    extra.update(sizing)
    extra.update(_resolution_extra(props, extra, bool(mask_field)))
    note = str(meta.get("description") or "").strip()
    return ModelSpec(eid, label, kind=kind, max_count=max_count, note=note[:160], **extra)


def _models_get(url, headers):
    status, _, data = base.http_request("GET", url, None, headers, 60)
    if status == 429:
        raise BackendError("fal.ai's model list is rate limited; try again in a minute.")
    _raise_fal(status, data)
    return _json(data)


def _quote(text):
    from urllib.parse import quote
    return quote(str(text), safe="/")


def _log_warning(msg, *args):
    try:
        from .. import log as gflog
        gflog.warning(msg, *args)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------

def _plan(spec, request):
    """``[(images_in_this_call, seed), ...]`` for the requested number of variations."""
    x = spec.extra
    seed = request.seed or random.randint(1, MAX_SEED - request.count)
    if x.get("count_field"):
        sizes, left = [], request.count
        while left > 0:
            sizes.append(min(left, spec.max_count))
            left -= sizes[-1]
    else:
        sizes = [1] * request.count
    plan = []
    for k, n in enumerate(sizes):
        s = seed + k
        s = s % (x["seed_max"] + 1) if x.get("seed_max") else s % (MAX_SEED + 1)
        plan.append((n, s))
    return plan


def build_payload(spec, request, prompt, urls, n, seed, tier="auto", style=None):
    """Request body for one queue call, read from the catalogue entry's ``extra``.

    ``tier`` is the resolution tier to ask for (``auto`` keeps the catalogue's own value);
    ``style`` the layout style that carries ``request.boxes`` (None: boxes are not sent).
    """
    x = spec.extra
    boxes = layout.sends(style, request.boxes)
    image = urls["image"]
    if x.get("image_field", "image_urls") == "image_urls":
        images = [image]
        if x.get("mask_as_image") and urls.get("mask"):
            images.append(urls["mask"])
        body = {"image_urls": images}
    else:
        body = {"image_url": image}
    if x.get("mask_field"):
        body[x["mask_field"]] = urls["mask"]
    pfield = x.get("prompt_field", "prompt")
    fields = {}
    if spec.kind == "edit":
        text = ref_prompt(prompt, boxes)
        if x.get("mask_as_image") and urls.get("mask"):
            text = MASK_NOTE + text
        body["prompt"], fields = layout.apply(style, text, request.boxes, request.width,
                                              request.height)
    elif pfield:
        if prompt or boxes:
            body[pfield], fields = layout.apply(style, prompt, request.boxes, request.width,
                                                request.height)
        elif x.get("prompt_required", True):
            body[pfield] = REMOVAL_PROMPT
    if x.get("negative_field") and request.negative_prompt:
        body[x["negative_field"]] = request.negative_prompt
    if x.get("count_field"):
        body[x["count_field"]] = n
    if x.get("seed_field"):
        body[x["seed_field"]] = seed
    if x.get("size") == "obj":
        body["image_size"] = {"width": request.width, "height": request.height}
    elif x.get("size") == "wh":
        body["width"], body["height"] = request.width, request.height
    if x.get("aspect_field"):
        body[x["aspect_field"]] = aspect_value(spec, request.width, request.height)
    if x.get("output_format", True):
        body["output_format"] = "png"
    body.update(x.get("fixed", {}))
    value = (x.get("resolution_values") or {}).get(tier)
    if x.get("resolution_field") and value:
        body[x["resolution_field"]] = value
    if x.get("quality_field") and request.quality in ("low", "medium", "high"):
        body[x["quality_field"]] = request.quality
    body.update(fields)
    if boxes and style in ("ideogram", "flux3_fields") and x.get("expansion_field"):
        body[x["expansion_field"]] = False    # a rewritten prompt may lose the boxes / <obj_N>
    return body


def _binarize(mask_png, invert=False):
    """Opaque binary mask PNG: white = fill, or black = fill with ``invert``."""
    img = pngio.read(mask_png)
    on, off = (0, 255) if invert else (255, 0)
    table = bytes(bytearray(on if v >= MASK_THRESHOLD else off for v in range(256)))
    for i, row in enumerate(img.rows):
        img.rows[i] = bytearray(bytes(row).translate(table))
    return pngio.encode(img)


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------

class _Uploader(object):
    """CDN upload with fallbacks; reuses one CDN token for both files."""

    def __init__(self, headers):
        self.headers = headers
        self._token = None
        self._token_failed = False

    def upload(self, name, data):
        url = self._cdn_v3(name, data) or self._initiate(name, data)
        return url or "data:image/png;base64,%s" % base64.b64encode(data).decode("ascii")

    def _cdn_v3(self, name, data):
        if self._token is None and not self._token_failed:
            hdrs = dict(self.headers)
            hdrs.update({"Accept": "application/json", "Content-Type": "application/json"})
            status, _, body = base.http_request(
                "POST", REST_ROOT + "/storage/auth/token?storage_type=fal-cdn-v3", b"{}", hdrs, 60)
            if status in (401, 403):
                _raise_fal(status, body)
            info = _json_or_none(body) if status < 300 else None
            if info and info.get("token"):
                self._token = "%s %s" % (info.get("token_type") or "Bearer", info["token"])
            else:
                self._token_failed = True
        if not self._token:
            return None
        status, _, body = base.http_request("POST", CDN_UPLOAD_URL, data, {
            "Authorization": self._token, "Content-Type": "image/png", "Accept": "application/json",
            "X-Fal-File-Name": name,
            "X-Fal-Object-Lifecycle": json.dumps({"expiration_duration_seconds": 86400})}, 180)
        info = _json_or_none(body) if status < 300 else None
        return info.get("access_url") if info else None

    def _initiate(self, name, data):
        hdrs = dict(self.headers)
        hdrs.update({"Accept": "application/json", "Content-Type": "application/json"})
        payload = json.dumps({"file_name": name, "content_type": "image/png"}).encode("utf-8")
        status, _, body = base.http_request(
            "POST", REST_ROOT + "/storage/upload/initiate?storage_type=gcs", payload, hdrs, 60)
        info = _json_or_none(body) if status < 300 else None
        if not info or not info.get("upload_url") or not info.get("file_url"):
            return None
        status, _, _ = base.http_request("PUT", info["upload_url"], data,
                                         {"Content-Type": "image/png"}, 180)
        return info["file_url"] if status < 300 else None


# --------------------------------------------------------------------------
# Results and errors
# --------------------------------------------------------------------------

def _queue_urls(endpoint, request_id):
    """status/response/cancel URLs from the endpoint id (only used if the submit reply lacks them)."""
    parts = endpoint.split("/")
    n = 3 if parts[0] in ("workflows", "comfy") else 2
    root = "%s/%s/requests/%s" % (QUEUE_ROOT, "/".join(parts[:n]), request_id)
    return root + "/status", root, root + "/cancel"


def _fetch_images(result, errors):
    """Download every unflagged image of one result; failures go to ``errors``."""
    items = result.get("images")
    if not isinstance(items, list) or not items:
        items = [result["image"]] if isinstance(result.get("image"), dict) else []
    flags = result.get("has_nsfw_concepts")
    urls = []
    for i, item in enumerate(items):
        if isinstance(flags, list) and i < len(flags) and flags[i]:
            continue  # blacked out by the safety checker
        url = item.get("url") if isinstance(item, dict) else item
        if url:
            urls.append(url)
    return collect(urls, _fetch_one, errors=errors)[0]


def _fetch_one(url):
    if url.startswith("data:"):
        try:
            data = base64.b64decode(url.split(",", 1)[1])
        except (IndexError, ValueError):
            raise BackendError("fal.ai returned an unreadable image.")
    else:
        data = base.download(url)
    return base.check_image(data, "fal.ai")


class _Transient(BackendError):
    """A poll error worth retrying (5xx, 429, network)."""


def _get(url, headers, timeout):
    """GET for polling: network errors and 5xx/429 answers raise :class:`_Transient`."""
    try:
        status, hdrs, data = base.http_request("GET", url, None, headers, timeout)
    except Cancelled:
        raise
    except BackendError as exc:
        raise _Transient(str(exc))
    if status >= 500 or status == 429:
        try:
            _raise_fal(status, data)
        except BackendError as exc:
            raise _Transient(str(exc))
    return status, hdrs, data


def _json_or_none(data):
    try:
        value = json.loads(data.decode("utf-8")) if data else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _json(data):
    value = _json_or_none(data)
    if value is None:
        raise BackendError("fal.ai returned an unexpected response.")
    return value


def _failed_text(message, error_type):
    text = str(message)
    if error_type and "content_policy" in str(error_type):
        return "fal.ai's content filter blocked this request. Try a different prompt."
    return "fal.ai could not finish the request: %s" % text


def _raise_fal(status, data):
    """Raise a readable BackendError for an HTTP error status (``detail`` list or string)."""
    if status < 400:
        return
    info = _json_or_none(data) or {}
    detail = info.get("detail")
    kind = info.get("error_type") or ""
    if isinstance(detail, list):
        parts = []
        for d in detail:
            if not isinstance(d, dict):
                parts.append(str(d))
                continue
            if "content_policy" in str(d.get("type", "")):
                kind = "content_policy_violation"
            loc = [str(p) for p in d.get("loc", []) if p != "body"]
            parts.append("%s: %s" % (".".join(loc), d.get("msg")) if loc else str(d.get("msg")))
        detail = "; ".join(parts)
    if kind and "content_policy" in kind:
        raise BackendError("fal.ai's content filter blocked this request. "
                           "Try a different prompt. (%s)" % (detail or kind))
    detail = detail or info.get("message") or (data.decode("utf-8", "replace")[:300] if data
                                               else "no details")
    if status in (401, 403):
        raise BackendError("fal.ai authentication failed (%s). Check your FAL_KEY and account "
                           "balance. %s" % (status, detail))
    if status == 422 or status == 400:
        raise BackendError("fal.ai rejected the request: %s" % detail)
    if status == 429:
        raise BackendError("fal.ai rate limit reached. %s" % detail)
    raise BackendError("fal.ai error (HTTP %s): %s" % (status, detail))
