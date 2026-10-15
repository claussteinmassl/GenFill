"""Magnific (Freepik) through its remote MCP server (``https://mcp.magnific.com``).

Two routes, both on the artist's Magnific credits:

* ``images_retouch``: masked inpaint/erase that keeps the unmasked pixels. It has no count or
  seed, so N variations are N calls that are waited on together.
* ``images_generate`` with the crop as an image reference: re-renders the crop (GenFill comps it
  back through the selection). One call returns ``count`` images and takes a seed.

Plate and mask are uploaded once (presigned PUT + finalize) and shared by all variations.
Magnific has no cancel tool: a queued job is billed even if GenFill stops waiting for it, so
every finished image is returned even when sibling jobs failed (see ``cloud``).
"""
from __future__ import annotations

from . import discovery
from .base import (BackendError, Cancelled, ModelSpec, check_image, download, sent_tier,
                   tiers_from)
from .cloud import (McpBackend, collect, finish, pause, poll, progress_step, put_bytes,
                    ref_prompt)
from .oauth import LoginRequired
from .. import pricing, region

SERVER_URL = "https://mcp.magnific.com"
PIXEL_BUDGET = 1024 * 1024
MAX_WAIT_IDS = 8       # creations_wait takes at most 8 identifiers
WAIT_SECONDS = 25      # longest long-poll creations_wait allows
PENDING = ("queued", "processing")
MIN_PAUSE, MAX_PAUSE = 1.0, 5.0   # between creations_wait rounds that brought no progress

# aspectRatio enum of images_generate (models without customSize snap to one of these).
ASPECT_RATIOS = ("1:1", "21:9", "16:9", "9:16", "2:3", "3:4", "1:2", "2:1", "5:4", "4:5",
                 "3:2", "4:3")


BETA_NOTE = "Beta on Magnific; may need account access."
_QUALITY = dict((q, q) for q in ("low", "medium", "high"))   # GenFill quality -> model value
# aspectRatios of models whose list differs from ASPECT_RATIOS (images_models_list).
_FLUX2_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "1:2", "2:1", "4:5")
_SEEDREAM_RATIOS = _FLUX2_RATIOS + ("21:9",)
_SEEDREAM5_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9")
_IDEOGRAM45_RATIOS = ("1:1", "4:5", "5:4", "3:4", "4:3", "2:3", "3:2", "9:16", "16:9", "1:2",
                      "2:1", "1:3", "3:1")
_FLUX3_RATIOS = ("21:9", "2:1", "16:9", "3:2", "7:5", "4:3", "5:4", "1:1", "4:5", "3:4", "5:7",
                 "2:3", "9:16", "1:2", "9:21")
_NB_LITE_RATIOS = ("1:1", "2:3", "3:2", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16", "21:9")


_ALL = ("1K", "2K", "4K")


def _retouch(id, label, erase=None, **extra):
    """``extra``: ``resolution`` (the value sent for auto), ``resolutions`` (tiers sent as
    ``resolution`` "1k"/"2k"/"4k"). A retouch model without a resolution list returns the
    input size, so every tier works by sending the crop larger."""
    extra.setdefault("res_param", "resolution" in extra)
    extra.setdefault("resolutions", _ALL)
    return ModelSpec(id, label, kind="inpaint", erase=erase, max_count=8, **extra)


def _edit(id, label, **extra):
    """``extra``: ``custom_size``, ``resolution`` (sent for auto), ``resolutions`` (tiers the
    model renders; none = fixed ~1 MP, so 1K only), ``quality`` (GenFill -> model value,
    "auto" always omits it) and ``ratios`` (the model's aspectRatio enum when not
    ASPECT_RATIOS)."""
    extra.setdefault("res_param", "resolution" in extra or "resolutions" in extra)
    extra.setdefault("resolutions", ("1K",))
    return ModelSpec(id, label, kind="edit", max_count=8, **extra)


CATALOGUE = (
    # Retouch: erase=None means the model erases itself; replace-only models erase via Auto.
    _retouch("retouch-auto", "Retouch Auto"),
    _retouch("retouch-pro", "Retouch Pro"),
    _retouch("retouch-classic", "Retouch Classic", erase="retouch-auto"),
    _retouch("retouch-imagen-nano-banana-2-flash", "Retouch Nano Banana 2",
             erase="retouch-auto", resolution="1k", resolutions=_ALL),
    _retouch("retouch-imagen-nano-banana-2", "Retouch Nano Banana Pro",
             erase="retouch-auto", resolution="2k", resolutions=("2K", "4K")),
    _retouch("retouch-inpaint-lab-nano-banana-2-flash", "Retouch Nano Banana 2 (beta)",
             resolution="1k"),
    _retouch("retouch-inpaint-lab-nano-banana-pro", "Retouch Nano Banana Pro (beta)",
             resolution="2k", resolutions=("2K", "4K")),
    _retouch("retouch-inpaint-lab-seedream-5-pro", "Retouch Seedream 5 Pro (beta)",
             resolution="1.5k", resolutions=("2K",)),
    _retouch("retouch-inpaint-lab-gpt-2-mini", "Retouch GPT 2.5 (beta)", resolution="1k",
             note=BETA_NOTE),
    _retouch("retouch-precise-gpt-2-mini", "Retouch GPT 2.5 Precise (beta)", note=BETA_NOTE),
    _retouch("retouch-inpaint-lab-gpt-2", "Retouch GPT 2 (beta)", resolution="1k",
             note=BETA_NOTE),
    _retouch("retouch-precise-gpt-2", "Retouch GPT 2 Precise (beta)", note=BETA_NOTE),
    _retouch("retouch-inpaint-lab-nano-banana-2-1", "Retouch Nano Banana 2.1 (beta)",
             resolution="1k", note=BETA_NOTE),
    _retouch("retouch-precise-nano-banana-2-1", "Retouch Nano Banana 2.1 Precise (beta)",
             note=BETA_NOTE),
    _retouch("retouch-precise-nano-banana-2-flash", "Retouch Nano Banana 2 Precise (beta)",
             note=BETA_NOTE),
    _retouch("retouch-precise-nano-banana-pro", "Retouch Nano Banana Pro Precise (beta)",
             note=BETA_NOTE),
    _retouch("retouch-ideogram-4-5", "Retouch Ideogram 4.5 (beta)", note=BETA_NOTE),
    _retouch("retouch-precise-ideogram-4-5", "Retouch Ideogram 4.5 Precise (beta)",
             note=BETA_NOTE),
    _retouch("retouch-erase", "Retouch Erase (erase only)", erase_only=True),
    # Edit: images_generate with the crop as an image reference.
    _edit("seedream-5-pro", "Seedream 5 Pro", custom_size=True, resolution="1.5k",
          resolutions=("2K",)),
    _edit("seedream-5-lite", "Seedream 5 Lite", resolution="2k", ratios=_SEEDREAM_RATIOS,
          resolutions=("2K", "4K")),
    _edit("seedream-5-flash", "Seedream 5 Flash", resolution="1.5k", ratios=_SEEDREAM5_RATIOS,
          resolutions=("2K",)),
    _edit("imagen-nano-banana-2-flash", "Nano Banana 2", custom_size=True, resolution="1k",
          resolutions=_ALL),
    _edit("imagen-nano-banana-2", "Nano Banana Pro", custom_size=True, resolution="1k",
          resolutions=_ALL),
    _edit("imagen-nano-banana-2-1", "Nano Banana 2.1 (beta)", custom_size=True,
          resolution="1k", resolutions=_ALL, note=BETA_NOTE),
    _edit("imagen-nano-banana-2-lite", "Nano Banana 2 Lite", ratios=_NB_LITE_RATIOS),
    _edit("gpt-2-mini", "GPT 2.5 (beta)", custom_size=True, resolution="1k", quality=_QUALITY,
          resolutions=_ALL, note=BETA_NOTE),
    _edit("gpt-2", "GPT 2", custom_size=True, resolution="1k", quality=_QUALITY,
          resolutions=_ALL),
    _edit("ideogram-4-5", "Ideogram 4.5 (beta)", quality=_QUALITY, ratios=_IDEOGRAM45_RATIOS,
          note=BETA_NOTE),
    _edit("flux-kontext", "Flux.1 Kontext Pro"),
    _edit("flux-kontext-high", "Flux.1 Kontext Max"),
    _edit("flux-2", "Flux.2 Pro", resolution="1k", ratios=_FLUX2_RATIOS,
          resolutions=("1K", "2K")),
    _edit("flux-2-max", "Flux.2 Max", resolution="1k", ratios=_FLUX2_RATIOS,
          resolutions=("1K", "2K")),
    _edit("flux-2-flex", "Flux.2 Flex", resolution="1k", ratios=_FLUX2_RATIOS,
          resolutions=("1K", "2K")),
    _edit("flux-3-image", "Flux.3 (beta)", resolution="1k", ratios=_FLUX3_RATIOS,
          resolutions=_ALL, note=BETA_NOTE),
    _edit("qwen-image-3-0-pro", "Qwen Image 3.0 Pro (beta)", resolution="1k",
          ratios=_FLUX2_RATIOS, resolutions=("1K", "2K"), note=BETA_NOTE),
    _edit("seedream-4-5", "Seedream 4.5", ratios=_SEEDREAM_RATIOS, resolutions=("2K", "4K")),
)


class MagnificBackend(McpBackend):
    id = "magnific"
    label = "Magnific (MCP)"
    server_url = SERVER_URL
    auth = "oauth"
    default_model = "retouch-auto"
    catalogue = CATALOGUE
    description = "Pixel-preserving Retouch plus reference-edit models, billed to Magnific credits."
    discoverable = True

    def adhoc_spec(self, model):
        kind = "inpaint" if model.startswith("retouch-") else "edit"
        return ModelSpec(model, model, kind=kind, max_count=8)

    def resolutions(self, model=""):
        """Entries without a tier list (typed ids, older cached lists): retouch answers at
        the input size (any tier); an edit model at its own ~1 MP size (1K)."""
        spec = self.model_spec(model)
        if spec is not None and "resolutions" not in spec.extra:
            return _ALL if spec.kind == "inpaint" else ("1K",)
        return super(MagnificBackend, self).resolutions(model)

    def discover(self, api_key=""):
        """Live catalogue: ``retouch_models_list`` (text) plus ``images_models_list`` (JSON)."""
        if not self.provider().signed_in():
            raise LoginRequired("Not signed in to Magnific (API Keys & Accounts...).")
        client = self.client(api_key, interactive=False)
        retouch = client.call_tool("retouch_models_list", {})
        edits = client.call_tool("images_models_list", {})
        return retouch_specs(retouch) + edit_specs(edits)

    def model_size(self, width, height, model="", resolution="auto"):
        spec = self.model_spec(model)
        budget = self.tier_budget(width, height, model, resolution, PIXEL_BUDGET)
        if spec.kind == "edit" and not spec.extra.get("custom_size"):
            w, h, _ = _closest_ratio(spec, width, height, budget)
            return w, h
        # Retouch returns the input size; custom-size edit models take any size.
        return region.scale_to_pixel_budget(width, height, budget, 16, min_side=256,
                                            max_aspect=4.0)

    def _resolution_arg(self, spec, request):
        """The ``resolution`` argument for ``spec``: the tier ("2k") or the auto default."""
        x = (CATALOGUE_BY_ID.get(spec.id) or spec).extra
        tier = self.request_tier(request, spec.id)
        if tier != "auto" and x.get("res_param") and tier in (x.get("resolutions") or ()):
            return tier.lower()
        return x.get("resolution")

    # ---- generation -----------------------------------------------------------------
    def generate(self, request, api_key, progress=None, cancelled=None):
        client = self.client(api_key, progress, cancelled)
        spec = self.model_spec(request.model)
        count = max(1, min(int(request.count), 8))

        def check():
            if cancelled and cancelled():
                raise Cancelled("Cancelled")

        progress_step(progress, 0.02, "Uploading to Magnific...")
        plate_id, mask_id = self._upload(client, request, check)
        check()
        errors = []
        if spec.kind == "inpaint":
            ids = self._submit_retouch(client, spec, request, plate_id, mask_id, count, check,
                                       errors)
        else:
            ids = self._submit_edit(client, spec, request, plate_id, count)
        urls = self._wait(client, ids, progress, cancelled, errors)
        images, _ = collect(urls, lambda url: check_image(download(url), "Magnific"),
                            cancelled, errors)
        return finish(images, errors, "Magnific")

    # ---- cost and balance -----------------------------------------------------------
    def estimate(self, request, api_key=""):
        """``simulate_cost`` with the arguments Generate would send (read-only, exact).

        Retouch has no count, so the total is N x the per-call quote; ``images_generate``
        quotes all ``count`` images in one call.
        """
        client = self.quiet_client(api_key)
        spec = self.model_spec(request.model)
        count = max(1, min(int(request.count), 8))
        if spec.kind == "inpaint":
            tool, args, calls = "images_retouch", self._retouch_args(spec, request), count
        else:
            tool, args, calls = "images_generate", self._edit_args(spec, request, None, count), 1
        res = _call(client, "simulate_cost", {"tool": tool, "arguments": args})
        if res.get("isUnlimited"):
            return pricing.Quote(0, pricing.FREE, note="unlimited on your plan", count=count)
        credits = res.get("credits")
        if not isinstance(credits, (int, float)):
            raise BackendError("Magnific simulate_cost returned no credits: %s" % _trim(res))
        exact = str(res.get("certainty") or "exact") == "exact"
        return pricing.Quote(credits * calls, pricing.CREDITS, exact, count=count)

    def balance(self, api_key=""):
        """``account_balance``: ``credits.available`` and the plan's product name."""
        res = _call(self.quiet_client(api_key), "account_balance", {})
        credits = (res.get("credits") or {}).get("available")
        if not isinstance(credits, (int, float)):
            return None
        return pricing.Balance(credits, (res.get("plan") or {}).get("productName") or "")

    def _upload(self, client, request, check):
        """Upload plate and mask; returns their creation identifiers."""
        targets = _call(client, "creations_request_upload",
                        {"mimeType": "image/png", "count": 2}).get("uploads") or []
        if len(targets) < 2 or not all(isinstance(t, dict) and t.get("proxyUploadUrl")
                                       for t in targets[:2]):
            raise BackendError("Magnific did not return upload targets.")
        names = ("genfill_plate.png", "genfill_mask.png")
        for target, attr in zip(targets, ("image_path", "mask_path")):
            check()
            put_bytes(target["proxyUploadUrl"], request.read(attr), "image/png")
        res = _call(client, "creations_finalize_upload", {
            "fileName": names[0],
            "uploads": [{"path": t.get("path"), "fileName": n}
                        for t, n in zip(targets, names)],
            "visible": False,
        })
        done = res.get("results") or []
        if res.get("errors") or len(done) < 2 or not all(
                isinstance(r, dict) and r.get("identifier") for r in done[:2]):
            raise BackendError("Magnific could not store the uploaded images: %s"
                               % _trim(res.get("errors") or res))
        return done[0]["identifier"], done[1]["identifier"]

    def _retouch_args(self, spec, request, plate_id=None, mask_id=None):
        """``images_retouch`` arguments (one call per variation); no ids for a quote."""
        prompt = request.prompt.strip()
        erase = not prompt or spec.extra.get("erase_only")
        model = (spec.erase or spec.id) if erase else spec.id
        args = {"mode": "erase" if erase else "replace", "model": model}
        if plate_id:
            args["creationIdentifier"] = plate_id
            args["maskCreationIdentifier"] = mask_id
        if not erase:
            args["prompt"] = prompt
        resolution = self._resolution_arg(self.model_spec(model), request)
        if resolution:
            args["resolution"] = resolution
        return args

    def _submit_retouch(self, client, spec, request, plate_id, mask_id, count, check, errors):
        """Queue one retouch job per variation; a failure keeps the jobs already queued."""
        args = self._retouch_args(spec, request, plate_id, mask_id)
        ids = []
        for i in range(count):
            check()
            try:
                res = _call(client, "images_retouch", args)
                creation = res.get("creation")
                if not isinstance(creation, dict) or not creation.get("identifier"):
                    raise BackendError("Magnific did not queue the retouch job: %s"
                                       % _trim(res))
            except Cancelled:
                raise
            except BackendError as exc:
                if not ids:
                    raise
                errors.append(str(exc))
                break
            ids.append(creation["identifier"])
        return ids

    def _edit_args(self, spec, request, plate_id=None, count=1):
        """``images_generate`` arguments; without ``plate_id`` (a quote) no references,
        because ``simulate_cost`` only accepts identifiers of real creations."""
        args = {"prompt": ref_prompt(request.prompt), "mode": spec.id, "count": count}
        if plate_id:
            args["references"] = [{"type": "image", "identifier": plate_id}]
        seed = request.seed & 0xFFFFFFFF    # images_generate takes 0..4294967295
        if seed:
            args["seed"] = seed
        if spec.extra.get("custom_size"):
            args["width"], args["height"] = request.width, request.height
        else:
            args["aspectRatio"] = _closest_ratio(spec, request.width, request.height)[2]
        resolution = self._resolution_arg(spec, request)
        if resolution:
            args["resolution"] = resolution
        quality = (spec.extra.get("quality") or {}).get((request.quality or "auto").lower())
        if quality:
            args["quality"] = quality
        return args

    def _submit_edit(self, client, spec, request, plate_id, count):
        res = _call(client, "images_generate", self._edit_args(spec, request, plate_id, count))
        ids = [c.get("identifier") for c in res.get("creations") or [] if isinstance(c, dict)]
        if not ids or not all(ids):
            raise BackendError("Magnific did not queue the image job: %s" % _trim(res))
        return ids

    def _wait(self, client, ids, progress, cancelled, errors):
        """Long-poll ``creations_wait`` until every job is terminal.

        Returns the result URLs of the completed jobs in submission order; failed jobs (and a
        wait that gives up early) add a message to ``errors`` instead of discarding the rest.
        """
        urls, failed = {}, set()

        def step():
            pending = [i for i in ids if i not in urls and i not in failed]
            res = _call(client, "creations_wait",
                        {"identifiers": pending[:MAX_WAIT_IDS], "timeoutSeconds": WAIT_SECONDS},
                        timeout=WAIT_SECONDS + 30)
            before = len(urls) + len(failed)
            after_s = 0.0
            for entry in res.get("results") or []:
                if not isinstance(entry, dict) or entry.get("identifier") not in pending:
                    continue
                ident = entry["identifier"]
                status = str(entry.get("status") or "").lower()
                url = (entry.get("results") or {}).get("url")
                if status == "completed" and url:
                    urls[ident] = url
                elif status == "completed":
                    failed.add(ident)
                    errors.append("Magnific finished without a result image.")
                elif status not in PENDING:
                    failed.add(ident)
                    errors.append("Magnific could not generate the image (%s). %s" % (
                        status or "unknown status",
                        _trim(entry.get("error") or entry.get("message") or "")))
                else:
                    after_s = max(after_s, _number(entry.get("poll_after_seconds")))
            progress_step(progress, 0.1 + 0.8 * len(urls) / float(len(ids)),
                          "Magnific: %d of %d done..." % (len(urls), len(ids)))
            if len(urls) + len(failed) == len(ids):
                return True
            if len(urls) + len(failed) == before:
                # No progress: never hammer the server if the long-poll answers at once.
                pause(min(max(after_s, MIN_PAUSE), MAX_PAUSE), cancelled)
            return None

        try:
            poll(step, cancelled, timeout=900, interval=0, what="Magnific")
        except Cancelled:
            raise
        except BackendError as exc:
            if not urls:
                raise
            errors.append(str(exc))   # keep the finished (billed) images
        return [urls[i] for i in ids if i in urls]


CATALOGUE_BY_ID = dict((m.id, m) for m in CATALOGUE)

# Retouch models that need inputs GenFill doesn't have (a character image, a LoRA).
_RETOUCH_SKIP = ("retouch-reference", "retouch-lora")


def _name(entry):
    name = str(entry.get("name") or entry.get("slug") or "").strip()
    return name[len("Google "):] if name.startswith("Google ") else name


def retouch_specs(result):
    """Inpaint specs from a ``retouch_models_list`` answer (indented text or JSON)."""
    data = result if isinstance(result, dict) else discovery.parse_toon(result)
    specs = []
    for m in data.get("models") or []:
        if not isinstance(m, dict):
            continue
        slug = str(m.get("slug") or "")
        modes = [str(x) for x in m.get("retouchModes") or []]
        if (not slug.startswith("retouch-") or slug in _RETOUCH_SKIP
                or m.get("tool", "retouch") != "retouch"
                or not ("replace" in modes or "erase" in modes)):
            continue
        label = "Retouch " + _name(m)
        if "-precise-" in slug:
            label += " Precise"
        extra = {}
        if "replace" not in modes:
            extra["erase_only"] = True
            label += " (erase only)"
        elif m.get("beta"):
            label += " (beta)"
        resolution = discovery.smallest_resolution(m.get("resolutions"))
        if resolution:
            extra["resolution"] = resolution
            extra["resolutions"] = tiers_from(m.get("resolutions"))
        specs.append(_retouch(slug, label, erase=None if "erase" in modes else "retouch-auto",
                              note=BETA_NOTE if m.get("private") else "", **extra))
    return specs


def edit_specs(result):
    """Reference-edit specs from an ``images_models_list`` answer.

    Only models that take an ``image`` reference qualify; models that need extra settings
    (``extraSettings``) and the ``auto`` router are left out.
    """
    specs = []
    for m in (result.get("models") if isinstance(result, dict) else None) or []:
        if not isinstance(m, dict):
            continue
        slug = str(m.get("slug") or "")
        if (not slug or slug == "auto" or m.get("extraSettings") or not m.get("supportsReferences")
                or "image" not in (m.get("referenceTypes") or [])):
            continue
        extra = {}
        if m.get("customSize"):
            extra["custom_size"] = True
        ratios = discovery.ratio_list(m.get("aspectRatios"))
        if ratios:
            extra["ratios"] = ratios
        resolution = discovery.smallest_resolution(m.get("resolutions"))
        if resolution:
            extra["resolution"] = resolution
            extra["resolutions"] = tiers_from(m.get("resolutions"))
        quality = discovery.quality_map(m.get("qualities"))
        if quality:
            extra["quality"] = quality
        label = _name(m) + (" (beta)" if m.get("beta") else "")
        specs.append(_edit(slug, label, note=BETA_NOTE if m.get("private") else "", **extra))
    return specs


def _closest_ratio(spec, width, height, budget=PIXEL_BUDGET):
    """``(w, h, "a:b")``: the model's aspect ratio closest to ``width`` x ``height``, sized to
    ``budget`` pixels."""
    sizes = region.aspect_sizes(spec.extra.get("ratios") or ASPECT_RATIOS, budget)
    return region.closest_size(width, height, sizes)


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _trim(value, limit=300):
    return str(value).strip()[:limit]


def _call(client, name, args, timeout=None):
    """Call a tool and insist on a JSON object (some tools answer in plain text)."""
    res = client.call_tool(name, args, timeout=timeout)
    if not isinstance(res, dict):
        raise BackendError("Magnific %s returned an unexpected answer: %s" % (name, _trim(res)))
    return res
