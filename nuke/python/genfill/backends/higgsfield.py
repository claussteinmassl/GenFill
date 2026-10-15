"""Higgsfield image models through Higgsfield's remote MCP server.

Protocol (verified live, see docs/superpowers/research/research-higgsfield.md):
``media_upload`` -> PUT -> ``media_confirm`` -> ``generate_image_batch`` -> ``jobs_wait`` until
``all_terminal`` -> plain GET of every ``result_url``.

The server rejects the ``mask`` media role today, so every model is a reference edit
(``kind="edit"``): the model sees only the crop and an instruction, GenFill composites the result
back through the selection. Results come back at the model's own resolution; the engine resamples.
"""
from __future__ import annotations

import math
import random
import time

from .base import BackendError, Cancelled, ModelSpec, check_image, download
from .cloud import McpBackend, poll, progress_step, put_bytes, ref_prompt
from .. import region

MAX_SEED = 2147483647
WAIT_SECONDS = 15           # jobs_wait long-poll limit on the server
RESULT_TIMEOUT = 900

_CLASSIC = ["1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "9:16", "16:9", "21:9"]
_BASIC = ["1:1", "4:3", "3:4", "16:9", "9:16"]
_GPT = ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "27:16", "16:27",
        "9:8", "8:9", "4:5", "5:4"]
_WIDE = _CLASSIC + ["4:1", "1:4", "8:1", "1:8"]
_IDEOGRAM = _CLASSIC + ["1:3", "3:1", "1:2", "2:1"]
_QUALITY = ("low", "medium", "high")


def _spec(id, label, ratios, resolution="1k", quality=None, seed=False, note=""):
    """``quality``: GenFill quality -> model value ("auto" always means the model default)."""
    return ModelSpec(id, label, kind="edit", note=note, ratios=list(ratios),
                     resolution=resolution, quality=quality, seed=seed)


_CATALOGUE = (
    _spec("gpt_image_2_5", "GPT Image 2.5", _GPT, quality={q: q for q in _QUALITY},
          note="Follows the prompt well and keeps the geometry. About 0.25 credits at low."),
    _spec("gpt_image_2", "GPT Image 2", _BASIC + ["3:2", "2:3"], quality={q: q for q in _QUALITY}),
    _spec("nano_banana_2_1", "Nano Banana 2.1", _WIDE, seed=True, note="About 1.5 credits."),
    _spec("nano_banana_2", "Nano Banana 2", _CLASSIC, note="About 1.5 credits."),
    _spec("nano_banana_2_lite", "Nano Banana 2 Lite", _CLASSIC, note="About 1 credit."),
    _spec("nano_banana_pro", "Nano Banana Pro", _CLASSIC),
    _spec("nano_banana", "Nano Banana", _CLASSIC, resolution=None),
    _spec("ideogram_4_5", "Ideogram 4.5", _IDEOGRAM, seed=True,
          quality={"low": "low", "medium": "medium", "high": "high"}, note="About 1 credit at low."),
    _spec("seedream_v5_pro", "Seedream 5 Pro", ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3",
                                                "21:9"]),
    _spec("seedream_5_0_flash", "Seedream 5 Flash", _CLASSIC),
    _spec("seedream_v5_lite", "Seedream 5 Lite", _CLASSIC, resolution=None),
    _spec("seedream_v4_5", "Seedream 4.5", _CLASSIC, resolution=None),
    _spec("flux_kontext", "FLUX Kontext", _BASIC, resolution=None),
    _spec("flux_2", "FLUX.2", _BASIC),
    _spec("flux_3_image", "FLUX.3", ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"]),
)


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

    def model_size(self, width, height, model=""):
        ratios = self.model_spec(model).extra.get("ratios") or _BASIC
        ratio = nearest_ratio(width, height, ratios)
        a, b = (float(x) for x in ratio.split(":"))
        return region.scale_to_pixel_budget(a, b, 1024 * 1024, 16, min_side=256)

    def _params(self, request, spec, prompt, media_id, ratio, seed):
        params = {
            "model": spec.id,
            "prompt": prompt,
            "medias": [{"role": "image_references", "value": media_id}],
            "aspect_ratio": ratio,
            "use_unlim": False,
        }
        if spec.extra.get("resolution"):
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
        prompt = ref_prompt(request.prompt)
        count = max(1, min(request.count, 12))
        base_seed = request.seed or random.randint(1, MAX_SEED - count)
        client = self.client(api_key, progress, cancelled)

        progress_step(progress, 0.02, "Uploading the image to Higgsfield...")
        media_id = self._upload(client, request.read("image_path"))
        check()

        progress_step(progress, 0.1, "Submitting %d variation%s..." % (count, "s" * (count > 1)))
        requests = [{"index": i, "params": self._params(request, spec, prompt, media_id, ratio,
                                                        (base_seed + i) % MAX_SEED)}
                    for i in range(count)]
        out = client.call_tool("generate_image_batch", {"requests": requests,
                                                        "context": "GenFill generative fill"})
        jobs = self._submitted(out)
        check()

        results = self._wait(client, jobs, progress, cancelled)
        images = []
        for n, job in enumerate(results):
            check()
            if job.get("status") != "completed" or not job.get("result_url"):
                continue
            images.append(check_image(download(job["result_url"]), "Higgsfield"))
            progress_step(progress, 0.9 + 0.1 * (n + 1) / len(results), "Downloading results...")
        if not images:
            raise BackendError(self._failure_text(results))
        return images

    # -- steps ---------------------------------------------------------------

    def _upload(self, client, data):
        out = client.call_tool("media_upload", {"filename": "genfill_plate.png",
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

    def _wait(self, client, jobs, progress, cancelled):
        def step():
            out = client.call_tool("jobs_wait", {"jobs": jobs, "timeout_seconds": WAIT_SECONDS})
            if not isinstance(out, dict):
                raise BackendError("Higgsfield returned an unexpected job status.")
            summary = out.get("summary") or {}
            done = int(summary.get("completed", 0)) + int(summary.get("failed", 0))
            progress_step(progress, 0.1 + 0.8 * done / float(len(jobs)),
                          "Generating (%d of %d done)..." % (done, len(jobs)))
            if out.get("all_terminal"):
                return out.get("jobs") or []
            self._pause(float(out.get("poll_after_seconds") or 0), cancelled)
            return None
        return poll(step, cancelled, timeout=RESULT_TIMEOUT, interval=0,
                    what="Higgsfield to finish")

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
