"""Comfy Cloud through its remote MCP server (https://cloud.comfy.org/mcp).

* Mask models (Flux Pro Fill, Bria GenFill, Recraft inpaint; Flux Erase / Bria Eraser for an
  empty prompt) run as ONE API-format graph: ``LoadImage`` + ``LoadImageMask`` feeding N fill
  nodes (consecutive seeds), each into its own ``SaveImage``. One job, one wait, one
  ``get_output``.
* Partner edit models without a mask (Flux Kontext, Nano Banana, Qwen, Flux 2) go through
  ``partner_generate`` with the plate as ``image`` media, one call per variation.

Files go up through ``upload_file``: the server hands back a single-use PUT URL (the file
path is only templated into a curl command, the server never reads it), we PUT the bytes
there without auth and keep the returned ``name``. Results come from short-lived public links
that redirect to signed storage URLs; they are downloaded right away.
"""
from __future__ import annotations

import json
import random
import re
import sys
import time

from .base import (EDIT_EMPTY_PROMPT, BackendError, Cancelled, ModelSpec,
                   check_image, download)
from .cloud import McpBackend, poll, put_bytes, ref_prompt
from .. import region

MAX_SEED = 2147483646
WAIT_SECONDS = 60          # per wait_for_job call
MAX_JOB_SECONDS = 1200     # whole job
UPLOAD_URL_RE = re.compile(r"https://[^\s'\"\\]+/api/uploads/[A-Za-z0-9_-]+")
CONTENT_TYPE_RE = re.compile(r"Content-Type:\s*([\w/.+-]+)")
ASPECTS = ((1, 1), (4, 3), (3, 4), (3, 2), (2, 3), (16, 9), (9, 16), (21, 9), (9, 21))


class NodeSpec(object):
    """A Comfy API node that takes image + mask: its ``class_type`` and input builder."""

    def __init__(self, class_type, inputs, seeded=True):
        self.class_type = class_type
        self.inputs = inputs      # fn(prompt, seed, negative_prompt) -> dict of extra inputs
        self.seeded = seeded      # False: deterministic, one image is enough


NODES = {
    "flux-pro-fill": NodeSpec("FluxProFillNode", lambda p, s, n: {
        "prompt": p, "prompt_upsampling": False, "guidance": 60, "steps": 50, "seed": s}),
    "flux-erase": NodeSpec("FluxEraseNode", lambda p, s, n: {
        "dilate_pixels": 10, "seed": s}),
    "bria-genfill": NodeSpec("BriaGenFill", lambda p, s, n: {
        "prompt": p, "negative_prompt": n or "", "refine_prompt": True,
        "seed": max(1, s), "moderation": "false"}),
    "bria-eraser": NodeSpec("BriaEraser", lambda p, s, n: {
        "mask_type": "manual", "moderation": "false"}, seeded=False),
    "recraft-inpaint": NodeSpec("RecraftImageInpaintingNode", lambda p, s, n: dict(
        {"prompt": p, "n": 1, "seed": s}, **({"negative_prompt": n} if n else {}))),
}

PARTNER_SLUGS = {
    "flux-kontext-pro": "bfl/flux-kontext-pro",
    "flux-kontext-max": "bfl/flux-kontext-max",
    "flux-2-pro": "bfl/flux-2-pro",
    "nano-banana-2": "vertexai/nano-banana-2",
    "nano-banana-2-lite": "vertexai/nano-banana-2-lite",
    "nano-banana-pro": "vertexai/nano-banana-pro",
    "qwen-image-3": "qwen/qwen-image-3",
}


def client_os():
    """``darwin`` / ``windows`` / ``linux``, as ``upload_file`` and ``get_output`` expect."""
    if sys.platform.startswith("win"):
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def build_fill_graph(image_name, mask_name, prompt, seeds, spec, negative_prompt=""):
    """API-format graph: one plate + mask loader, a fill node and a SaveImage per seed.

    Args:
        image_name: Uploaded plate name (``subfolder/name`` if the server gave a subfolder).
        mask_name: Uploaded mask name (white = fill).
        prompt: Text for the fill nodes (ignored by erase nodes).
        seeds: One seed per variation.
        spec: The :class:`NodeSpec` of the fill node.
        negative_prompt: Optional, for nodes that take one.

    Returns:
        ``{node_id: {"class_type", "inputs", "_meta"}}``; the fill nodes are ``3, 5, ...`` and
        their SaveImage nodes ``4, 6, ...``.
    """
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name},
              "_meta": {"title": "GenFill Plate"}},
        "2": {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"},
              "_meta": {"title": "GenFill Mask"}},
    }
    for i, seed in enumerate(seeds):
        fill, save = str(3 + 2 * i), str(4 + 2 * i)
        inputs = {"image": ["1", 0], "mask": ["2", 0]}
        inputs.update(spec.inputs(prompt, seed, negative_prompt))
        graph[fill] = {"class_type": spec.class_type, "inputs": inputs,
                       "_meta": {"title": "GenFill Fill %d" % (i + 1)}}
        graph[save] = {"class_type": "SaveImage",
                       "inputs": {"images": [fill, 0], "filename_prefix": "genfill_%d" % (i + 1)},
                       "_meta": {"title": "GenFill Result %d" % (i + 1)}}
    return graph


def parse_upload_url(result):
    """``(put_url, content_type)`` from an ``upload_file`` result (structured or prose)."""
    if isinstance(result, dict):
        for key in ("upload_url", "url", "put_url"):
            val = result.get(key)
            if isinstance(val, str) and UPLOAD_URL_RE.search(val):
                text = json.dumps(result)
                break
        else:
            text = json.dumps(result)
    else:
        text = str(result)
    m = UPLOAD_URL_RE.search(text)
    if not m:
        raise BackendError("Comfy Cloud did not return an upload URL.")
    ct = CONTENT_TYPE_RE.search(text)
    return m.group(0), (ct.group(1) if ct else "image/png")


def _flat(res):
    """One dict from a tool result: the inner ``result`` merged with the outer fields."""
    if not isinstance(res, dict):
        return {"message": str(res)}
    out = {}
    if isinstance(res.get("result"), dict):
        out.update(res["result"])
    out.update((k, v) for k, v in res.items() if k != "result")
    return out


def _fail_text(info):
    return str(info.get("message") or info.get("error") or info.get("detail")
               or info.get("error_type") or "unknown error")[:400]


def _check_tool(res, what):
    info = _flat(res)
    if info.get("status") in ("tool_error", "error", "failed") or info.get("error_type"):
        raise BackendError("Comfy Cloud %s failed: %s" % (what, _fail_text(info)))
    return info


def _snapped(w, h, budget=1024 * 1024):
    """``(width, height, "a:b")``: the supported aspect closest to ``w`` x ``h``."""
    sizes = region.aspect_sizes(["%d:%d" % ab for ab in ASPECTS], budget, 16)
    return region.closest_size(w, h, sizes)


def _closest_aspect(w, h):
    return _snapped(w, h)[2]


def _pause(seconds, cancelled):
    """Sleep ``seconds`` in short steps, raising :class:`Cancelled` if asked to stop."""
    for _ in range(max(1, int(seconds / 0.25))):
        if cancelled and cancelled():
            raise Cancelled("Cancelled")
        time.sleep(0.25)


class ComfyCloudBackend(McpBackend):
    id = "comfy"
    label = "Comfy Cloud (MCP)"
    server_url = "https://cloud.comfy.org/mcp"
    api_key_header = "X-API-Key"
    env_var = "COMFY_API_KEY"
    auth = "key_or_oauth"
    needs_key = False
    default_model = "flux-pro-fill"
    description = ("Mask inpainting (Flux Fill, Bria, Recraft) and partner edit models on "
                   "Comfy Cloud. Needs a Comfy Cloud subscription; sign in or paste an API key.")
    catalogue = (
        ModelSpec("flux-pro-fill", "Flux Pro Fill", "inpaint", erase="flux-erase", max_count=4,
                  note="Best mask inpainting; ~11 credits per image."),
        ModelSpec("flux-erase", "Flux Erase", "erase", max_count=4,
                  note="Object removal, no prompt; ~6 credits."),
        ModelSpec("bria-genfill", "Bria GenFill", "inpaint", erase="bria-eraser", max_count=4,
                  note="Keeps pixels outside the mask exactly."),
        ModelSpec("bria-eraser", "Bria Eraser", "erase", max_count=1, note="Deterministic."),
        ModelSpec("recraft-inpaint", "Recraft Inpaint", "inpaint", max_count=4),
        ModelSpec("flux-kontext-pro", "Flux Kontext Pro", "edit", max_count=4),
        ModelSpec("flux-kontext-max", "Flux Kontext Max", "edit", max_count=4),
        ModelSpec("flux-2-pro", "Flux 2 Pro", "edit", max_count=4),
        ModelSpec("nano-banana-2", "Nano Banana 2", "edit", max_count=4),
        ModelSpec("nano-banana-2-lite", "Nano Banana 2 Lite", "edit", max_count=4,
                  note="Cheap ~1K tier."),
        ModelSpec("nano-banana-pro", "Nano Banana Pro", "edit", max_count=4),
        ModelSpec("qwen-image-3", "Qwen Image 3", "edit", max_count=4),
    )

    def model_size(self, width, height, model=""):
        spec = self.model_spec(model)
        if spec.kind == "edit" and "kontext" in (PARTNER_SLUGS.get(spec.id) or spec.id):
            # Kontext takes an aspect_ratio from ASPECTS: send the plate in exactly that shape.
            return _snapped(width, height)[:2]
        multiple = 32 if spec.kind != "edit" else 16
        return region.scale_to_pixel_budget(width, height, 1024 * 1024, multiple,
                                            min_side=256, max_aspect=3.0)

    # ---- generation ---------------------------------------------------------
    def generate(self, request, api_key, progress=None, cancelled=None):
        spec = self.model_spec(request.model)
        has_prompt = bool(request.prompt.strip())
        if not has_prompt and spec.erase:
            spec = self.model_spec(spec.erase)
        client = self.client(api_key, progress=progress, cancelled=cancelled)

        def step(frac, msg):
            if progress:
                progress(frac, msg)

        def check():
            if cancelled and cancelled():
                raise Cancelled("Cancelled")

        base_seed = request.seed or random.randint(1, MAX_SEED - request.count)
        step(0.02, "Uploading to Comfy Cloud...")
        if spec.kind == "edit":
            plate = self.upload(client, request.image_path, request.read("image_path"))
            check()
            return self._partner(client, spec, request, plate, base_seed, step, check,
                                 cancelled)
        plate = self.upload(client, request.image_path, request.read("image_path"))
        check()
        mask = self.upload(client, request.mask_path, request.read("mask_path"))
        check()
        node = NODES.get(spec.id)
        if node is None:
            raise BackendError("Comfy Cloud model %r has no mask workflow." % spec.id)
        count = request.count if node.seeded else 1
        seeds = [(base_seed + i) % MAX_SEED for i in range(count)]
        prompt = request.prompt if has_prompt else EDIT_EMPTY_PROMPT
        graph = build_fill_graph(plate["path"], mask["path"], prompt, seeds, node,
                                 request.negative_prompt)
        step(0.1, "Submitting to Comfy Cloud...")
        sub = _check_tool(client.call_tool("submit_workflow",
                                           {"workflow": graph, "confirm": True}),
                          "workflow submission")
        pid = sub.get("prompt_id")
        if not pid:
            raise BackendError("Comfy Cloud did not return a job id.")
        self._wait(client, pid, cancelled, step)
        check()
        urls = self._output_urls(client, pid)
        return self._download(urls, step, check)

    def upload(self, client, local_path, data):
        """Upload bytes through ``upload_file``; returns ``{"name", "subfolder", "path"}``."""
        res = client.call_tool("upload_file", {"file_path": local_path,
                                               "client_os": client_os()})
        url, ctype = parse_upload_url(res)
        body = put_bytes(url, data, ctype)
        try:
            info = json.loads(body.decode("utf-8"))
            name = info["name"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise BackendError("Comfy Cloud accepted the upload but did not name the file.")
        sub = info.get("subfolder") or ""
        return {"name": name, "subfolder": sub, "path": "%s/%s" % (sub, name) if sub else name}

    def _wait(self, client, prompt_id, cancelled, step):
        def once():
            info = _check_tool(client.call_tool(
                "wait_for_job", {"prompt_id": prompt_id, "max_wait_seconds": WAIT_SECONDS},
                timeout=WAIT_SECONDS + 30), "wait")
            state = info.get("job_status") or info.get("raw_status") or info.get("status")
            if state in ("completed", "succeeded"):
                return info
            if state in ("error", "failed", "cancelled"):
                raise BackendError("Comfy Cloud job %s: %s"
                                   % ("was cancelled" if state == "cancelled" else "failed",
                                      _fail_text(info)))
            step(0.4, "Generating on Comfy Cloud...")
            if not info.get("timed_out"):
                _pause(1.5, cancelled)   # the server answered without holding the request
            return None

        try:
            return poll(once, cancelled, timeout=MAX_JOB_SECONDS, interval=0,
                        what="Comfy Cloud")
        except Cancelled:
            # The client's own cancel hook would refuse this call, so lift it first.
            ctx = getattr(client, "ctx", None)
            if isinstance(ctx, dict):
                ctx["cancelled"] = None
            try:
                client.call_tool("cancel_job", {"prompt_id": prompt_id})
            except BackendError:
                pass
            raise

    def _output_urls(self, client, prompt_id):
        res = client.call_tool("get_output", {"prompt_id": prompt_id, "client_os": client_os(),
                                              "inline_urls": False})
        return self._result_urls(_check_tool(res, "output"))

    @staticmethod
    def _result_urls(info):
        urls = [r["url"] for r in info.get("results") or []
                if isinstance(r, dict) and r.get("url")]
        if not urls:
            raise BackendError("Comfy Cloud finished but returned no images.")
        return urls

    def _download(self, urls, step, check):
        out = []
        for i, url in enumerate(urls):
            check()
            step(0.7 + 0.3 * i / len(urls), "Downloading result %d of %d..." % (i + 1, len(urls)))
            out.append(check_image(download(url), "Comfy Cloud"))
        return out

    # ---- partner edit models ------------------------------------------------
    def _partner(self, client, spec, request, plate, base_seed, step, check, cancelled):
        slug = PARTNER_SLUGS.get(spec.id) or spec.id
        prompt = ref_prompt(request.prompt)
        out = []
        for i in range(request.count):
            check()
            step(0.1 + 0.6 * i / request.count,
                 "Generating variation %d of %d..." % (i + 1, request.count))
            args = {"type": "image", "model": slug, "client_os": client_os(),
                    "prompt": prompt, "seed": (base_seed + i) % MAX_SEED,
                    "medias": [{"role": "image", "name": plate["name"],
                                "subfolder": plate["subfolder"], "type": "input"}],
                    "confirm": True, "wait_for_output": True}
            if "kontext" in slug:
                args["params"] = {"aspect_ratio": _closest_aspect(request.width,
                                                                  request.height)}
            info = _check_tool(client.call_tool("partner_generate", args, timeout=120),
                               "generation")
            if not [r for r in info.get("results") or [] if isinstance(r, dict) and r.get("url")]:
                pid = info.get("prompt_id")
                if not pid:
                    raise BackendError("Comfy Cloud returned no result.")
                self._wait(client, pid, cancelled, step)
                urls = self._output_urls(client, pid)[:1]
            else:
                urls = self._result_urls(info)[:1]
            # Download now: the shortlinks only live a few minutes.
            out += self._download(urls, step, check)
        return out

