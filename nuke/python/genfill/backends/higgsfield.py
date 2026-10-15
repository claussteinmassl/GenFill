"""Higgsfield image models through Higgsfield's remote MCP server.

Protocol (verified live, see docs/superpowers/research/research-higgsfield.md):
``media_upload`` -> PUT -> ``media_confirm`` -> ``generate_image_batch`` -> ``jobs_wait`` until
``all_terminal`` -> plain GET of every ``result_url``.

Every model is a reference edit (``kind="edit"``) for now: the model sees only the crop and an
instruction, GenFill composites the result back through the selection. The reference-image role
differs per model (``media_role``). Results come back at the model's own resolution; the engine
resamples.

Four models (nano_banana_2, nano_banana_2_1, nano_banana_2_lite, ideogram_4_5) list a ``mask``
media role in ``models_explore`` (the Nano Banana ones also an ``is_inpaint`` flag). The masked
path is implemented (``mask=True``: the GenFill mask, white = fill, is uploaded like the plate and
sent as a ``mask`` media, the model becomes ``kind="inpaint"``), but it stays switched off through
``MASK_ROLE_LIVE`` until a live test confirms that the role is accepted (it answered "Unknown media
role: mask" in earlier tests) and which mask polarity Higgsfield expects; neither is documented.
"""
from __future__ import annotations

import math
import random
import time

from .base import (EDIT_EMPTY_PROMPT, EDIT_PROMPT_SUFFIX, BackendError, Cancelled, ModelSpec,
                   check_image, download, tiers_from)
from . import discovery
from .cloud import McpBackend, collect, finish, poll, progress_step, put_bytes, ref_prompt
from .oauth import LoginRequired
from .. import pricing, region

MAX_SEED = 2147483647
WAIT_SECONDS = 15           # jobs_wait long-poll limit on the server
RESULT_TIMEOUT = 900

_CLASSIC = ["1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "9:16", "16:9", "21:9"]
_BASIC = ["1:1", "4:3", "3:4", "16:9", "9:16"]
_GPT = ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "27:16", "16:27",
        "9:8", "8:9", "4:5", "5:4"]
_GPT2 = ["1:1", "4:3", "3:4", "16:9", "21:9", "9:16", "3:2", "2:3"]
_WIDE = _CLASSIC + ["4:1", "1:4", "8:1", "1:8"]
_IDEOGRAM = ["1:1", "1:2", "2:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9", "5:8", "8:5",
             "3:4", "4:3", "9:22", "22:9", "9:23", "23:9", "3:8", "8:3", "5:12", "12:5",
             "1:3", "3:1"]
_SEEDREAM = ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"]
_SEEDREAM_LITE = ["1:1", "4:3", "3:4", "16:9", "9:16", "21:9"]
_FLUX3 = ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"]
_HAZEL = ["1:1", "3:2", "2:3"]
_GROK = ["1:1", "1:2", "2:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"]
_KLING = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"]
_QUALITY = ("low", "medium", "high")

# The mask media role is listed by models_explore but not live-tested (role accepted? polarity?).
# True turns the models declared with ``_masked()`` into mask inpaint models.
MASK_ROLE_LIVE = False


_ALL = ("1K", "2K", "4K")


def _spec(id, label, ratios, resolution="1k", quality=None, seed=False, note="",
          role="image_references", mask=False, inpaint_flag=False, resolutions=None):
    """One catalogue entry.

    Args:
        resolution: ``resolution`` sent for auto (None: the model takes none).
        resolutions: Tiers sent as ``resolution`` "1k"/"2k"/"4k" (models_explore options);
            default 1K only with a ``resolution`` parameter, else fixed size.
        quality: GenFill quality -> model value ("auto" always means the model default).
        role: Media role of the reference image (differs per model).
        mask: Send the GenFill mask as a ``mask`` media; the model becomes ``kind="inpaint"``.
        inpaint_flag: The model also takes ``is_inpaint`` (sent as true with the mask).
    """
    if resolutions is None:
        resolutions = ("1K",) if resolution else ()
    return ModelSpec(id, label, kind="inpaint" if mask else "edit", note=note,
                     ratios=list(ratios), resolution=resolution, resolutions=tuple(resolutions),
                     quality=quality, seed=seed,
                     media_role=role, mask=bool(mask), inpaint_flag=bool(mask and inpaint_flag))


def _masked(**kw):
    """Keyword args for a model whose models_explore entry lists a ``mask`` role."""
    return dict(kw, mask=MASK_ROLE_LIVE)


_CATALOGUE = (
    _spec("gpt_image_2_5", "GPT Image 2.5", _GPT, quality={q: q for q in _QUALITY},
          resolutions=_ALL,
          note="Follows the prompt well and keeps the geometry. About 0.25 credits at low."),
    _spec("gpt_image_2", "GPT Image 2", _GPT2, quality={q: q for q in _QUALITY}, role="image",
          resolutions=_ALL),
    _spec("nano_banana_2_1", "Nano Banana 2.1", _WIDE, seed=True, note="About 1.5 credits.",
          resolutions=_ALL,
          **_masked(inpaint_flag=True)),
    _spec("nano_banana_2", "Nano Banana 2", _CLASSIC, note="About 1.5 credits.", resolutions=_ALL,
          **_masked(inpaint_flag=True)),
    _spec("nano_banana_2_lite", "Nano Banana 2 Lite", _CLASSIC, note="About 1 credit.",
          **_masked(inpaint_flag=True)),
    _spec("nano_banana_pro", "Nano Banana Pro", _CLASSIC, resolutions=_ALL),
    _spec("nano_banana", "Nano Banana", _CLASSIC, resolution=None),
    _spec("ideogram_4_5", "Ideogram 4.5", _IDEOGRAM, seed=True, resolutions=("1K", "2K"),
          quality={"low": "low", "medium": "medium", "high": "high"}, note="About 1 credit at low.",
          **_masked()),
    _spec("seedream_v5_pro", "Seedream 5 Pro", _SEEDREAM, resolutions=("1K", "2K")),
    _spec("seedream_5_0_flash", "Seedream 5 Flash", _SEEDREAM, resolutions=("1K", "2K")),
    _spec("seedream_v5_lite", "Seedream 5 Lite", _SEEDREAM_LITE, resolution=None),
    _spec("seedream_v4_5", "Seedream 4.5", _SEEDREAM, resolution=None),
    _spec("flux_kontext", "FLUX Kontext", _BASIC, resolution=None),
    _spec("flux_2", "FLUX.2", _BASIC, resolutions=("1K", "2K")),
    _spec("flux_3_image", "FLUX.3", _FLUX3, resolutions=_ALL),
    _spec("openai_hazel", "OpenAI Hazel", _HAZEL, resolution=None,
          quality={q: q for q in _QUALITY}),
    _spec("grok_image_2_0", "Grok Image 2.0", _GROK,
          quality={"low": "low", "medium": "medium", "high": "medium"}, resolutions=("1K", "2K")),
    _spec("grok_image", "Grok Image", _GROK, resolutions=("1K", "2K")),
    _spec("kling_omni_image", "Kling O1 Image", _KLING, resolutions=("1K", "2K")),
)


MAX_EXPLORE_PAGES = 5
# Media roles that carry the crop (preferred first) and the roles GenFill can fill.
_IMAGE_ROLES = ("image_references", "image")
# Workflow tools that are not image edits (characters, ads, upscalers, routers, sprites).
_EXPLORE_SKIP = ("soul_", "ms_", "marketing_", "image_auto", "autosprite", "3d_jutsu",
                 "outpaint", "topaz", "upscale", "decompose", "background", "cinematic_studio_soul")


def explore_specs(items):
    """Edit specs from ``models_explore`` items.

    Kept: a public provider (internal tools have an empty ``provider_name``), an image or mask
    media role, a non-empty aspect-ratio list and no required parameter GenFill can't fill.
    """
    specs = []
    for item in items:
        mid = str(item.get("id") or "")
        if (not mid or not str(item.get("provider_name") or "").strip()
                or not str(item.get("description") or "").strip()
                or any(k in mid for k in _EXPLORE_SKIP)):
            continue
        roles = set()
        for media in item.get("medias") or []:
            if isinstance(media, dict) and media.get("type", "image") == "image":
                roles.update(str(r) for r in media.get("roles") or [])
        role = next((r for r in _IMAGE_ROLES if r in roles), None)
        ratios = discovery.ratio_list(item.get("aspect_ratios"))
        params = dict((str(p.get("name")), p) for p in item.get("parameters") or []
                      if isinstance(p, dict))
        if (not role or not ratios
                or any(str(p.get("required")) == "required" for p in params.values())):
            continue
        options = (params.get("resolution") or {}).get("options")
        resolution = discovery.smallest_resolution(options)
        quality = discovery.quality_map((params.get("quality") or {}).get("options"))
        kw = dict(resolution=resolution, resolutions=tiers_from(options) if resolution else (),
                  quality=quality, seed="seed" in params, role=role,
                  note=str(item.get("description") or "").strip()[:160])
        if "mask" in roles:
            kw = _masked(inpaint_flag="is_inpaint" in params, **kw)
        specs.append(_spec(mid, str(item.get("name") or mid), ratios, **kw))
    return specs


def nearest_ratio(width, height, ratios):
    """The ``"w:h"`` string in ``ratios`` closest to ``width`` x ``height`` (log scale)."""
    a = math.log(float(width) / float(height))

    def dist(r):
        w, h = (float(x) for x in r.split(":"))
        return abs(math.log(w / h) - a)
    return min(ratios, key=dist)


class HiggsfieldBackend(McpBackend):
    id = "higgsfield"
    label = "Higgsfield (MCP)"
    server_url = "https://mcp.higgsfield.ai/mcp"
    description = ("Higgsfield image-edit models (GPT Image, Nano Banana, Seedream, FLUX, "
                   "Ideogram). Sign in with your Higgsfield account; costs credits.")
    default_model = "gpt_image_2_5"
    catalogue = _CATALOGUE
    models = tuple(m.id for m in _CATALOGUE)
    discoverable = True

    def discover(self, api_key=""):
        """Live catalogue from ``models_explore`` (image models that take an image or mask)."""
        if not self.provider().signed_in():
            raise LoginRequired("Not signed in to Higgsfield (API Keys & Accounts...).")
        client = self.client(api_key, interactive=False)
        items, after = [], None
        for _ in range(MAX_EXPLORE_PAGES):
            args = {"action": "list", "type": "image", "limit": 100}
            if after:
                args["after"] = after
            out = client.call_tool("models_explore", args)
            if not isinstance(out, dict):
                raise BackendError("Higgsfield models_explore returned an unexpected answer.")
            items += [i for i in out.get("items") or [] if isinstance(i, dict)]
            after = out.get("next_page_token")
            if not out.get("has_more") or not after:
                break
        return explore_specs(items)

    def resolutions(self, model=""):
        """Entries without a tier list (typed ids, older cached lists): 1K."""
        spec = self.model_spec(model)
        if spec is not None and "resolutions" not in spec.extra:
            return ("1K",)
        return super(HiggsfieldBackend, self).resolutions(model)

    def model_size(self, width, height, model="", resolution="auto"):
        ratios = self.model_spec(model).extra.get("ratios") or _BASIC
        ratio = nearest_ratio(width, height, ratios)
        a, b = (float(x) for x in ratio.split(":"))
        budget = self.tier_budget(width, height, model, resolution, 1024 * 1024)
        return region.scale_to_pixel_budget(a, b, budget, 16, min_side=256)

    def _params(self, request, spec, prompt, media_id, ratio, seed, mask_id=None):
        """One request's params; ``media_id`` None (a cost quote) sends no medias."""
        params = {
            "model": spec.id,
            "prompt": prompt,
            "aspect_ratio": ratio,
            "use_unlim": False,
        }
        if media_id:
            medias = [{"role": spec.extra.get("media_role") or "image_references",
                       "value": media_id}]
            if mask_id:
                medias.append({"role": "mask", "value": mask_id})
            params["medias"] = medias
        if mask_id and spec.extra.get("inpaint_flag"):
            params["is_inpaint"] = True
        tier = self.request_tier(request, spec.id)
        if spec.extra.get("resolution") and tier in (spec.extra.get("resolutions") or ()):
            params["resolution"] = tier.lower()
        elif spec.extra.get("resolution"):
            params["resolution"] = spec.extra["resolution"]
        quality = (spec.extra.get("quality") or {}).get((request.quality or "auto").lower())
        if quality:
            params["quality"] = quality
        if spec.extra.get("seed"):
            params["seed"] = seed
        return params

    def generate(self, request, api_key, progress=None, cancelled=None):
        def check():
            if cancelled and cancelled():
                raise Cancelled("Cancelled")

        spec = self.model_spec(request.model)
        ratio = nearest_ratio(request.width, request.height,
                              spec.extra.get("ratios") or _BASIC)
        masked = bool(spec.extra.get("mask"))
        prompt = self._prompt(spec, request)
        count = max(1, min(request.count, 12))
        base_seed = request.seed or random.randint(1, MAX_SEED - count)
        client = self.client(api_key, progress, cancelled)

        progress_step(progress, 0.02, "Uploading the image to Higgsfield...")
        media_id = self._upload(client, request.read("image_path"))
        check()
        mask_id = None
        if masked:
            mask_id = self._upload(client, request.read("mask_path"), "genfill_mask.png")
            check()

        progress_step(progress, 0.1, "Submitting %d variation%s..." % (count, "s" * (count > 1)))
        requests = [{"index": i, "params": self._params(request, spec, prompt, media_id, ratio,
                                                        (base_seed + i) % MAX_SEED, mask_id)}
                    for i in range(count)]
        out = client.call_tool("generate_image_batch", {"requests": requests,
                                                        "context": "GenFill generative fill"})
        jobs = self._submitted(out)
        check()

        errors = []
        results = self._wait(client, jobs, progress, cancelled, errors)
        urls = [j["result_url"] for j in results
                if j.get("status") == "completed" and j.get("result_url")]
        if not urls and not errors:
            raise BackendError(self._failure_text(results))
        progress_step(progress, 0.9, "Downloading results...")
        images, _ = collect(urls, lambda url: check_image(download(url), "Higgsfield"),
                            cancelled, errors)
        return finish(images, errors, "Higgsfield")

    @staticmethod
    def _prompt(spec, request):
        text = request.prompt.strip()
        if spec.extra.get("mask"):
            return text + EDIT_PROMPT_SUFFIX if text else EDIT_EMPTY_PROMPT
        return ref_prompt(text)

    # -- cost and balance ----------------------------------------------------

    def estimate(self, request, api_key=""):
        """``generate_image`` with ``get_cost: true`` (submits nothing) for one image.

        Generate sends one batch entry per variation, so the total is N x the one-image quote.
        A live check found ``count`` ignored by the quote (count 3 cost the same as count 1),
        hence the quote is for count 1; a multi-image total is marked approximate. The quote
        is taken without the input image (media ids must be real uploads).
        """
        client = self.quiet_client(api_key)
        spec = self.model_spec(request.model)
        ratio = nearest_ratio(request.width, request.height, spec.extra.get("ratios") or _BASIC)
        count = max(1, min(request.count, 12))
        params = self._params(request, spec, self._prompt(spec, request), None, ratio,
                              request.seed or 1)
        params.update(get_cost=True, count=1)
        out = client.call_tool("generate_image", {"params": params,
                                                  "context": "GenFill cost preview"})
        cost = out.get("cost") if isinstance(out, dict) else None
        credits = None
        if isinstance(cost, dict):
            credits = cost.get("credits_exact", cost.get("credits"))
        if not isinstance(credits, (int, float)):
            raise BackendError("Higgsfield returned no cost: %s" % str(out)[:200])
        return pricing.per_image(credits, count, pricing.CREDITS, exact=count == 1)

    def balance(self, api_key=""):
        """``balance``: credits and the subscription plan."""
        out = self.quiet_client(api_key).call_tool("balance", {})
        if not isinstance(out, dict) or not isinstance(out.get("credits"), (int, float)):
            return None
        plan = str(out.get("subscription_plan_type") or "").replace("_", " ").strip()
        return pricing.Balance(out["credits"], plan[:1].upper() + plan[1:])

    # -- steps ---------------------------------------------------------------

    def _upload(self, client, data, filename="genfill_plate.png"):
        out = client.call_tool("media_upload", {"filename": filename,
                                                "content_type": "image/png"})
        uploads = out.get("uploads") if isinstance(out, dict) else None
        if not uploads or not uploads[0].get("upload_url") or not uploads[0].get("media_id"):
            raise BackendError("Higgsfield did not return an upload URL.")
        up = uploads[0]
        put_bytes(up["upload_url"], data, up.get("content_type") or "image/png")
        client.call_tool("media_confirm", {"type": "image", "media_ids": [up["media_id"]]})
        return up["media_id"]

    @staticmethod
    def _submitted(out):
        if not isinstance(out, dict):
            raise BackendError("Higgsfield rejected the request: %s" % str(out)[:300])
        if out.get("recovery_tool"):
            raise BackendError("Higgsfield could not start the generation (%s)."
                               % (out.get("message") or out.get("recovery_tool")))
        jobs = [{"index": j.get("index", i), "job_id": j["job_id"]}
                for i, j in enumerate(out.get("jobs") or []) if j.get("job_id")]
        if not jobs:
            raise BackendError("Higgsfield did not accept the generation request.")
        return jobs

    def _wait(self, client, jobs, progress, cancelled, errors):
        """Job states once all are terminal; if waiting fails, the ones finished so far."""
        last = []

        def step():
            out = client.call_tool("jobs_wait", {"jobs": jobs, "timeout_seconds": WAIT_SECONDS})
            if not isinstance(out, dict):
                raise BackendError("Higgsfield returned an unexpected job status.")
            last[:] = [j for j in out.get("jobs") or [] if isinstance(j, dict)]
            summary = out.get("summary") or {}
            done = int(summary.get("completed", 0)) + int(summary.get("failed", 0))
            progress_step(progress, 0.1 + 0.8 * done / float(len(jobs)),
                          "Generating (%d of %d done)..." % (done, len(jobs)))
            if out.get("all_terminal"):
                return out.get("jobs") or []
            self._pause(float(out.get("poll_after_seconds") or 0), cancelled)
            return None
        try:
            return poll(step, cancelled, timeout=RESULT_TIMEOUT, interval=0,
                        what="Higgsfield to finish")
        except Cancelled:
            raise
        except BackendError as exc:
            done = [j for j in last if j.get("status") == "completed" and j.get("result_url")]
            if not done:
                raise
            errors.append(str(exc))   # Higgsfield has no cancel: keep what is paid for
            return done

    @staticmethod
    def _pause(seconds, cancelled):
        end = time.time() + min(seconds, 10)
        while time.time() < end:
            if cancelled and cancelled():
                raise Cancelled("Cancelled")
            time.sleep(0.25)

    @staticmethod
    def _failure_text(results):
        statuses = sorted(set(str(j.get("status")) for j in results))
        if any("nsfw" in s for s in statuses):
            return "Higgsfield's content filter blocked this result. Try a different prompt."
        return "Higgsfield did not produce an image (%s)." % (", ".join(statuses) or "no jobs")
