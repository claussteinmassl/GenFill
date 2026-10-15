"""Magnific (Freepik) through its remote MCP server (``https://mcp.magnific.com``).

Two routes, both on the artist's Magnific credits:

* ``images_retouch``: masked inpaint/erase that keeps the unmasked pixels. It has no count or
  seed, so N variations are N calls that are waited on together.
* ``images_generate`` with the crop as an image reference: re-renders the crop (GenFill comps it
  back through the selection). One call returns ``count`` images and takes a seed.

Plate and mask are uploaded once (presigned PUT + finalize) and shared by all variations.
"""
from __future__ import annotations

from .base import BackendError, Cancelled, ModelSpec, check_image, download
from .cloud import McpBackend, poll, progress_step, put_bytes, ref_prompt
from .. import region

SERVER_URL = "https://mcp.magnific.com"
PIXEL_BUDGET = 1024 * 1024
MAX_WAIT_IDS = 8       # creations_wait takes at most 8 identifiers
WAIT_SECONDS = 25      # longest long-poll creations_wait allows
PENDING = ("queued", "processing")

# aspectRatio enum of images_generate (models without customSize snap to one of these).
ASPECT_RATIOS = ("1:1", "21:9", "16:9", "9:16", "2:3", "3:4", "1:2", "2:1", "5:4", "4:5",
                 "3:2", "4:3")


def _retouch(id, label, erase=None, **extra):
    return ModelSpec(id, label, kind="inpaint", erase=erase, max_count=8, **extra)


def _edit(id, label, **extra):
    return ModelSpec(id, label, kind="edit", max_count=8, **extra)


CATALOGUE = (
    # Retouch: erase=None means the model erases itself; replace-only models erase via Auto.
    _retouch("retouch-auto", "Retouch Auto"),
    _retouch("retouch-pro", "Retouch Pro"),
    _retouch("retouch-classic", "Retouch Classic", erase="retouch-auto"),
    _retouch("retouch-imagen-nano-banana-2-flash", "Retouch Nano Banana 2",
             erase="retouch-auto", resolution="1k"),
    _retouch("retouch-imagen-nano-banana-2", "Retouch Nano Banana Pro",
             erase="retouch-auto", resolution="2k"),
    _retouch("retouch-inpaint-lab-nano-banana-2-flash", "Retouch Nano Banana 2 (beta)",
             resolution="1k"),
    _retouch("retouch-inpaint-lab-nano-banana-pro", "Retouch Nano Banana Pro (beta)",
             resolution="2k"),
    _retouch("retouch-inpaint-lab-seedream-5-pro", "Retouch Seedream 5 Pro (beta)",
             resolution="1.5k"),
    _retouch("retouch-erase", "Retouch Erase (erase only)", erase_only=True),
    # Edit: images_generate with the crop as an image reference.
    _edit("seedream-5-pro", "Seedream 5 Pro", custom_size=True, resolution="1.5k"),
    _edit("imagen-nano-banana-2-flash", "Nano Banana 2", custom_size=True, resolution="1k"),
    _edit("imagen-nano-banana-2", "Nano Banana Pro", custom_size=True, resolution="1k"),
    _edit("imagen-nano-banana-2-lite", "Nano Banana 2 Lite"),
    _edit("gpt-2", "GPT 2", custom_size=True, resolution="1k"),
    _edit("flux-kontext", "Flux.1 Kontext Pro"),
    _edit("flux-kontext-high", "Flux.1 Kontext Max"),
    _edit("flux-2", "Flux.2", resolution="1k"),
    _edit("seedream-4-5", "Seedream 4.5"),
)


class MagnificBackend(McpBackend):
    id = "magnific"
    label = "Magnific (MCP)"
    server_url = SERVER_URL
    auth = "oauth"
    default_model = "retouch-auto"
    catalogue = CATALOGUE
    description = "Pixel-preserving Retouch plus reference-edit models, billed to Magnific credits."

    def model_spec(self, model=""):
        model = (model or self.default_model).strip()
        for m in self.catalogue:
            if m.id == model:
                return m
        kind = "inpaint" if model.startswith("retouch-") else "edit"
        return ModelSpec(model, model, kind=kind, max_count=8)

    def model_size(self, width, height, model=""):
        spec = self.model_spec(model)
        if spec.kind == "edit" and not spec.extra.get("custom_size"):
            sizes = region.aspect_sizes(ASPECT_RATIOS, PIXEL_BUDGET)
            w, h, _ = region.closest_size(width, height, sizes)
            return w, h
        # Retouch returns the input size; custom-size edit models take any size.
        return region.scale_to_pixel_budget(width, height, PIXEL_BUDGET, 16, min_side=256,
                                            max_aspect=4.0)

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
        if spec.kind == "inpaint":
            ids = self._submit_retouch(client, spec, request, plate_id, mask_id, count, check)
        else:
            ids = self._submit_edit(client, spec, request, plate_id, count)
        urls = self._wait(client, ids, progress, cancelled)
        results = []
        for url in urls:
            check()
            results.append(check_image(download(url), "Magnific"))
        return results

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

    def _submit_retouch(self, client, spec, request, plate_id, mask_id, count, check):
        prompt = request.prompt.strip()
        erase = not prompt or spec.extra.get("erase_only")
        model = (spec.erase or spec.id) if erase else spec.id
        args = {"creationIdentifier": plate_id, "maskCreationIdentifier": mask_id,
                "mode": "erase" if erase else "replace", "model": model}
        if not erase:
            args["prompt"] = prompt
        resolution = (CATALOGUE_BY_ID.get(model) or spec).extra.get("resolution")
        if resolution:
            args["resolution"] = resolution
        ids = []
        for i in range(count):
            check()
            res = _call(client, "images_retouch", args)
            creation = res.get("creation")
            if not isinstance(creation, dict) or not creation.get("identifier"):
                raise BackendError("Magnific did not queue the retouch job: %s" % _trim(res))
            ids.append(creation["identifier"])
        return ids

    def _submit_edit(self, client, spec, request, plate_id, count):
        args = {"prompt": ref_prompt(request.prompt), "mode": spec.id, "count": count,
                "references": [{"type": "image", "identifier": plate_id}]}
        if request.seed:
            args["seed"] = request.seed
        if spec.extra.get("custom_size"):
            args["width"], args["height"] = request.width, request.height
        else:
            sizes = region.aspect_sizes(ASPECT_RATIOS, PIXEL_BUDGET)
            args["aspectRatio"] = region.closest_size(request.width, request.height, sizes)[2]
        if spec.extra.get("resolution"):
            args["resolution"] = spec.extra["resolution"]
        res = _call(client, "images_generate", args)
        ids = [c.get("identifier") for c in res.get("creations") or [] if isinstance(c, dict)]
        if not ids or not all(ids):
            raise BackendError("Magnific did not queue the image job: %s" % _trim(res))
        return ids

    def _wait(self, client, ids, progress, cancelled):
        """Long-poll ``creations_wait`` until every job finished; returns result URLs in order."""
        urls = {}

        def step():
            pending = [i for i in ids if i not in urls]
            res = _call(client, "creations_wait",
                        {"identifiers": pending[:MAX_WAIT_IDS], "timeoutSeconds": WAIT_SECONDS},
                        timeout=WAIT_SECONDS + 30)
            for entry in res.get("results") or []:
                if not isinstance(entry, dict):
                    continue
                status = str(entry.get("status") or "").lower()
                if status == "completed":
                    url = (entry.get("results") or {}).get("url")
                    if not url:
                        raise BackendError("Magnific finished without a result image.")
                    urls[entry.get("identifier")] = url
                elif status not in PENDING:
                    raise BackendError("Magnific could not generate the image (%s). %s" % (
                        status or "unknown status",
                        _trim(entry.get("error") or entry.get("message") or "")))
            progress_step(progress, 0.1 + 0.8 * len(urls) / float(len(ids)),
                          "Magnific: %d of %d done..." % (len(urls), len(ids)))
            return True if all(i in urls for i in ids) else None

        poll(step, cancelled, timeout=900, interval=0, what="Magnific")
        return [urls[i] for i in ids]


CATALOGUE_BY_ID = dict((m.id, m) for m in CATALOGUE)


def _trim(value, limit=300):
    return str(value).strip()[:limit]


def _call(client, name, args, timeout=None):
    """Call a tool and insist on a JSON object (some tools answer in plain text)."""
    res = client.call_tool(name, args, timeout=timeout)
    if not isinstance(res, dict):
        raise BackendError("Magnific %s returned an unexpected answer: %s" % (name, _trim(res)))
    return res
