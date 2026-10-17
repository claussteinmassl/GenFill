"""ComfyUI as a backend: any ComfyUI server (this machine, the network, RunPod, ...).

GenFill runs the API-format workflows of :mod:`comfy_workflows` in five steps:

1. upload the plate crop and the mask to the server (``POST /upload/image``),
2. queue one patched graph per variation (``POST /prompt``, seeds ``seed + i``),
3. poll ``GET /history/<id>`` until every graph has finished,
4. download the images of the output nodes (``GET /view``),
5. on cancel or timeout remove what is still queued and interrupt what is running.

A protected server (reverse proxy, RunPod proxy) takes the token as ``Authorization: Bearer``.
Workflows that call Comfy API nodes (partner models) also need a Comfy account key, sent as
``extra_data.api_key_comfy_org``. Paid-results policy as in :mod:`cloud`: every downloaded
image is returned even when a sibling variation failed.
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid

from . import comfy_workflows
from .base import (BackendError, Backend, Cancelled, check_image, encode_multipart, http_json,
                   http_request, raise_for_status)
from .cloud import collect, finish, poll, progress_step
from .. import pricing, region

MAX_SEED = 2 ** 31 - 1
POLL_INTERVAL = 1.0        # seconds between /history polls
HISTORY_RETRIES = 3        # tolerated consecutive failed /history polls per job
UPLOAD_FOLDER = "genfill"
COMFY_KEY_ENV = "COMFY_API_KEY"

# Where ComfyUI looks for the file a loader node asks for.
MODEL_FOLDERS = {
    "CheckpointLoaderSimple": "checkpoints", "CheckpointLoader": "checkpoints",
    "UNETLoader": "diffusion_models", "DualCLIPLoader": "text_encoders",
    "CLIPLoader": "text_encoders", "VAELoader": "vae", "LoraLoader": "loras",
    "LoraLoaderModelOnly": "loras",
}
NOT_IN_LIST_RE = re.compile(r"'(.*?)' not in")


class ComfyClient(object):
    """The few ComfyUI HTTP calls GenFill needs, with the token on every request."""

    def __init__(self, url, token="", timeout=60):
        self.url = url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def headers(self):
        return {"Authorization": "Bearer " + self.token} if self.token else {}

    def get_json(self, path):
        return http_json("GET", self.url + path, None, self.headers(), self.timeout)

    def post_json(self, path, obj):
        return http_json("POST", self.url + path, obj, self.headers(), self.timeout)

    def upload(self, path_on_disk, name):
        """Upload a PNG into the ``genfill`` input subfolder; returns the name to reference."""
        with open(path_on_disk, "rb") as fh:
            data = fh.read()
        body, ctype = encode_multipart(
            {"subfolder": UPLOAD_FOLDER, "type": "input", "overwrite": "true"},
            {"image": (name, data, "image/png")})
        hdrs = dict(self.headers(), **{"Content-Type": ctype})
        status, _, resp = http_request("POST", self.url + "/upload/image", body, hdrs, 300)
        raise_for_status(status, resp)
        try:
            info = json.loads(resp.decode("utf-8"))
            got = info["name"]
        except (ValueError, KeyError, TypeError):
            raise BackendError("ComfyUI accepted the upload but did not name the file.")
        sub = info.get("subfolder") or ""
        return "%s/%s" % (sub, got) if sub else got

    def queue_prompt(self, graph, api_key=""):
        """Queue one graph; returns its prompt id. Validation errors are made readable."""
        payload = {"prompt": graph, "client_id": uuid.uuid4().hex}
        if api_key:
            payload["extra_data"] = {"api_key_comfy_org": api_key}
        hdrs = dict(self.headers(), **{"Content-Type": "application/json",
                                       "Accept": "application/json"})
        status, _, body = http_request("POST", self.url + "/prompt",
                                       json.dumps(payload).encode("utf-8"), hdrs, self.timeout)
        if status == 400:
            raise BackendError(prompt_error(body))
        raise_for_status(status, body)
        try:
            return str(json.loads(body.decode("utf-8"))["prompt_id"])
        except (ValueError, KeyError, TypeError):
            raise BackendError("ComfyUI did not return a job id.")

    def view(self, image):
        """Bytes of one output image dict (``filename`` / ``subfolder`` / ``type``)."""
        from urllib.parse import urlencode
        query = urlencode({"filename": image.get("filename", ""),
                           "subfolder": image.get("subfolder", ""),
                           "type": image.get("type", "output")})
        status, _, data = http_request("GET", "%s/view?%s" % (self.url, query), None,
                                       self.headers(), 300)
        if status >= 400:
            raise BackendError("Could not download %s from ComfyUI (HTTP %s)."
                               % (image.get("filename", "a result"), status))
        return data

    def queue(self):
        """``(running ids, pending ids)`` from ``GET /queue``."""
        info = self.get_json("/queue")
        return _queue_ids(info.get("queue_running")), _queue_ids(info.get("queue_pending"))


class _Timeout(BackendError):
    """The workflow did not finish within its timeout."""


def _queue_ids(entries):
    return [str(e[1]) for e in entries or [] if isinstance(e, (list, tuple)) and len(e) > 1]


def prompt_error(body):
    """Readable message for a ``POST /prompt`` 400 answer (``error`` + ``node_errors``)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, AttributeError):
        return "ComfyUI rejected the workflow: %s" % (body or b"")[:300].decode("utf-8", "replace")
    if not isinstance(data, dict):
        return "ComfyUI rejected the workflow."
    lines = []
    for nid, node in sorted((data.get("node_errors") or {}).items()):
        cls = node.get("class_type", "node")
        for err in node.get("errors") or []:
            lines.append(_node_error(nid, cls, err))
    if lines:
        return "ComfyUI rejected the workflow: " + " ".join(lines)
    err = data.get("error")
    if isinstance(err, dict):
        text = " - ".join(str(x) for x in (err.get("message"), err.get("details")) if x)
        return "ComfyUI rejected the workflow: %s" % (text or "unknown error")
    return "ComfyUI rejected the workflow: %s" % (err or "unknown error")


def _node_error(nid, cls, err):
    extra = err.get("extra_info") or {}
    details = str(err.get("details") or "")
    if err.get("type") == "value_not_in_list" and "Loader" in cls:
        m = NOT_IN_LIST_RE.search(details)
        value = extra.get("received_value") or (m.group(1) if m else "?")
        return ("The server has no file '%s' for %s (node %s). Put it into ComfyUI's "
                "models/%s folder, or pick another workflow." % (value, cls, nid,
                                                                 MODEL_FOLDERS.get(cls, "models")))
    text = " - ".join(str(x) for x in (err.get("message"), details) if x)
    return "%s (node %s): %s." % (cls, nid, text or err.get("type") or "error")


def execution_error(entry):
    """Error message of a finished history entry, or ``""`` when it succeeded."""
    status = entry.get("status") or {}
    if status.get("status_str") != "error":
        return ""
    for msg in status.get("messages") or []:
        if isinstance(msg, (list, tuple)) and len(msg) > 1 and msg[0] == "execution_error":
            data = msg[1] if isinstance(msg[1], dict) else {}
            text = data.get("exception_message") or "execution error"
            node = data.get("node_type")
            return ("ComfyUI failed in %s: %s" % (node, text.strip()) if node
                    else "ComfyUI failed: %s" % text.strip())
    return "ComfyUI failed to run the workflow."


def server_info(url=None, token=None):
    """``{"version", "devices"}`` of a ComfyUI server (``GET /system_stats``).

    Args:
        url: Server URL; defaults to the configured one.
        token: Bearer token; defaults to the configured one.

    Raises:
        BackendError: If the server cannot be reached or answers with an error.
    """
    from .. import config
    url = (url or config.comfyui_url()).rstrip("/")
    token = config.comfyui_token() if token is None else token
    try:
        stats = ComfyClient(url, token, 10).get_json("/system_stats")
    except BackendError as exc:
        raise BackendError("Cannot reach ComfyUI at %s: %s" % (url, exc))
    system = stats.get("system") if isinstance(stats, dict) else None
    devices = stats.get("devices") if isinstance(stats, dict) else None
    return {"version": (system or {}).get("comfyui_version", ""),
            "devices": [str(d.get("name", "")) for d in devices or [] if isinstance(d, dict)]}


class ComfyUIBackend(Backend):
    id = "comfyui"
    label = "ComfyUI (local, network or cloud)"
    needs_key = False
    auth = "key"
    env_var = "GENFILL_COMFY_TOKEN"
    default_model = "flux_fill"
    description = ("Runs GenFill's ComfyUI workflows on your ComfyUI: this machine, one on the "
                   "network or a cloud GPU (RunPod etc.). Set the URL in API Keys & Accounts.")

    @property
    def catalogue(self):
        return [w.spec() for w in sorted(self.workflows().values(), key=lambda w: w.label.lower())]

    def workflows(self):
        from .. import config
        return comfy_workflows.library(config.comfyui_workflow_dirs())

    def workflow(self, model=""):
        """The usable workflow for ``model`` (default model if empty); else a BackendError."""
        from .. import config
        wid = (model or self.default_model).strip()
        lib = self.workflows()
        wf = lib.get(wid)
        if wf is None:
            folders = ", ".join(config.comfyui_workflow_dirs())
            avail = ", ".join(sorted(lib)) or "none"
            raise BackendError("Unknown ComfyUI workflow %r. Available: %s. Folders: %s."
                               % (wid, avail, folders))
        if wf.error:
            raise BackendError("ComfyUI workflow %r is not usable: %s" % (wid, wf.error))
        return wf

    def layout_style(self, model=""):
        """The workflow sidecar's ``layout`` (``"flux3"``: boxes go into ``GenFill Prompt``)."""
        try:
            return self.workflow(model).meta.get("layout") or None
        except BackendError:
            return None

    def _pick(self, request):
        """``(workflow, prompt_text)``: an empty prompt without boxes runs ``empty_prompt``."""
        wf = self.workflow(request.model)
        prompt, _ = self.layout_prompt(request, request.prompt, wf.id)
        if not prompt.strip() and wf.empty_prompt:
            wf = self.workflow(wf.empty_prompt)
        return wf, prompt

    def resolutions(self, model=""):
        try:
            return self.workflow(model).resolutions()
        except BackendError:
            return ("1K", "2K")     # fallback size below: max_side 2048

    def model_size(self, width, height, model="", resolution="auto"):
        tier, _ = self.resolution_plan(model, resolution)
        try:
            return self.workflow(model).model_size(width, height, tier)
        except BackendError:
            budget = self.tier_budget(width, height, model, resolution, 1024 * 1024)
            return region.scale_to_pixel_budget(width, height, budget, 64, min_side=512,
                                                max_side=2048, max_aspect=3.0)

    def estimate(self, request, api_key=""):
        """Free on your own GPU; a workflow with Comfy API nodes bills Comfy credits."""
        try:
            wf, _ = self._pick(request)
        except BackendError:
            return None
        if wf.api_nodes:
            return pricing.Quote(None, note="Comfy credits (API nodes)", count=request.count)
        return pricing.Quote(0, pricing.FREE, note="free (your GPU)", count=request.count)

    # ---- generation ---------------------------------------------------------
    def generate(self, request, api_key, progress=None, cancelled=None):
        from .. import config
        wf, prompt = self._pick(request)
        comfy_key = ""
        if wf.api_nodes:
            comfy_key = config.get_api_key("comfy", COMFY_KEY_ENV)
            if not comfy_key:
                raise BackendError("Workflow %r uses Comfy API nodes and needs a Comfy account "
                                   "key. Set %s or add it under API Keys & Accounts."
                                   % (wf.id, COMFY_KEY_ENV))
        client = ComfyClient(config.comfyui_url(), api_key if api_key is not None
                             else config.comfyui_token())

        def check():
            if cancelled and cancelled():
                raise Cancelled("Cancelled")

        progress_step(progress, 0.02, "Uploading to ComfyUI...")
        uid = uuid.uuid4().hex[:12]
        image = client.upload(request.image_path, "%s_source.png" % uid)
        check()
        if wf.mask_loader() == "LoadImage":
            mask = client.upload(request.mask_alpha_path, "%s_mask_alpha.png" % uid)
        else:
            mask = client.upload(request.mask_path, "%s_mask.png" % uid)
        check()

        base_seed = request.seed or random.randint(1, MAX_SEED - request.count)
        jobs, errors = [], []     # jobs: [prompt_id, output_ids]
        try:
            for i in range(request.count):
                check()
                progress_step(progress, 0.05 + 0.05 * i / request.count,
                              "Queueing variation %d of %d..." % (i + 1, request.count))
                graph, outputs = wf.patch(image, mask, prompt, request.negative_prompt,
                                          (base_seed + i) % MAX_SEED)
                try:
                    jobs.append([client.queue_prompt(graph, comfy_key), outputs])
                except Cancelled:
                    raise
                except BackendError as exc:
                    if not jobs:
                        raise
                    errors.append(str(exc))   # keep the variations already queued
                    break
            results = self._wait(client, jobs, wf, progress, cancelled, errors)
        except Exception:     # cancel, timeout or any failure: do not leave work behind
            self._cleanup(client, [j[0] for j in jobs])
            raise
        return finish(results, errors, "ComfyUI")

    def _wait(self, client, jobs, wf, progress, cancelled, errors):
        """Poll until every job finished; returns the downloaded images in job order.

        A failed poll (after retries) or the workflow timeout ends the wait. Images already
        downloaded are kept (the rest is cleaned up); with none, the error is raised.
        """
        done = {}           # prompt id -> list of images
        fails = {}          # prompt id -> consecutive failed /history polls
        deadline = time.time() + wf.timeout

        def once():
            if time.time() > deadline:
                raise _Timeout("Timed out waiting for ComfyUI.")
            for pid, outputs in jobs:
                if pid in done:
                    continue
                try:
                    entry = client.get_json("/history/" + pid).get(pid)
                except Cancelled:
                    raise
                except BackendError:
                    fails[pid] = fails.get(pid, 0) + 1
                    if fails[pid] > HISTORY_RETRIES:
                        raise
                    continue
                fails[pid] = 0
                if entry:
                    done[pid] = self._collect(client, entry, outputs, errors, cancelled)
            if len(done) == len(jobs):
                return True
            self._report(client, jobs, done, progress)
            return None

        try:
            poll(once, cancelled, timeout=wf.timeout + 3600, interval=POLL_INTERVAL,
                 what="ComfyUI")
        except Cancelled:
            raise
        except BackendError as exc:
            if not any(done.values()):
                raise
            errors.append(str(exc))   # keep what was produced, drop the rest
            self._cleanup(client, [pid for pid, _ in jobs if pid not in done])
        return [img for pid, _ in jobs for img in done.get(pid) or []]

    def _collect(self, client, entry, outputs, errors, cancelled):
        """Images of one finished job; its failures go to ``errors``."""
        message = execution_error(entry)
        if message:
            errors.append(message)
            return []
        images = []
        for oid in outputs:
            images += [i for i in ((entry.get("outputs") or {}).get(oid) or {}).get("images") or []
                       if isinstance(i, dict)]
        if not images:
            errors.append("ComfyUI finished but the workflow produced no images.")
            return []
        return collect(images, lambda i: check_image(client.view(i), "ComfyUI"), cancelled,
                       errors)[0]

    def _report(self, client, jobs, done, progress):
        """Progress line: queue position of the first waiting job, else generating."""
        if not progress:
            return
        frac = 0.1 + 0.85 * len(done) / max(1, len(jobs))
        msg = "Generating on ComfyUI..."
        try:
            running, pending = client.queue()
            waiting = [pid for pid, _ in jobs if pid not in done]
            for pid in waiting:
                if pid in running:
                    break
                if pid in pending:
                    msg = "Waiting in the ComfyUI queue (position %d)..." % (pending.index(pid) + 1)
                    break
        except BackendError:
            pass
        progress(frac, msg)

    def _cleanup(self, client, prompt_ids):
        """Remove queued jobs and interrupt the running one; never raises."""
        if not prompt_ids:
            return
        try:
            running, _ = client.queue()
        except BackendError:
            running = []
        try:
            client.post_json("/queue", {"delete": list(prompt_ids)})
        except BackendError:
            pass
        for pid in prompt_ids:
            if pid in running:
                try:
                    client.post_json("/interrupt", {"prompt_id": pid})
                except BackendError:
                    pass
