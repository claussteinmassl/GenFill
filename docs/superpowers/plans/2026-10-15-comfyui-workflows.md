# ComfyUI Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GenFill generates through any ComfyUI instance (and, added mid-run on user request: current model catalogues with live discovery, a 1K/2K/4K resolution setting, a multi-line prompt, cost preview and credit balance) (local, LAN, RunPod) with workflows shipped in `nuke/workflows/comfyui/`, picked from the model version dropdown; the built-in Local AI helper is removed.

**Architecture:** A pure workflow library (`backends/comfy_workflows.py`) loads API-format graphs plus optional sidecars and patches them by node title. A stdlib HTTP backend (`backends/comfyui.py`) uploads plate and mask, queues one patched graph per variation, polls history and downloads outputs. Settings (URL, token, workflow folders) come from env vars first, then `config.json`.

**Tech Stack:** Python 3.7+ stdlib only (urllib, json, threading), Nuke Python API, PySide2/6 via `ui/qt.py`, unittest with `tests/fake_nuke.py`.

**Spec:** `docs/superpowers/specs/2026-10-15-comfyui-workflows-design.md`

## Global Constraints

- Code, comments, docstrings in English; Google-style docstrings. Pure stdlib in `backends/` (no `nuke` import there).
- Python 3.7 compatible (no walrus, no `dict | dict`, no f-string `=`), matching existing style (`%` formatting is common in this repo).
- Backend id `comfyui`, label `ComfyUI (local, network or cloud)`, first entry in `backends._BACKENDS`.
- Env vars (win over config): `GENFILL_COMFY_URL`, `GENFILL_COMFY_TOKEN`, `GENFILL_COMFY_WORKFLOWS` (`os.pathsep`-separated). Config: `config.json` keys `comfyui.url`, `comfyui.workflow_dirs` (list), token in `api_keys.comfyui`. Default URL `http://127.0.0.1:8188`.
- Comfy.org key for API-node workflows: `config.get_api_key("comfy", "COMFY_API_KEY")`, sent as `extra_data.api_key_comfy_org` only when the sidecar says `"api_nodes": true`.
- Title convention (case-insensitive, `_meta.title`): `GenFill Image`, `GenFill Mask`, `GenFill Prompt`, `GenFill Negative`, `GenFill Seed`, `GenFill Output`. Image and Mask are required.
- Repo workflows: `nuke/workflows/comfyui/<id>.json` + `<id>.genfill.json`. Studio folders override repo ids.
- Default workflow id `flux_fill`. Paid-results policy: `cloud.collect` / `cloud.finish`.
- Never print secrets; never `git add -A` (stage explicit paths). Commits end with `Claude-Session: https://claude.ai/code/session_0145ayo4GhtZYb892kjAnJFw`.
- Test command: `python3 -m unittest discover -s tests` (no pytest installed).

**User decisions (already made):**
- "Node-Titel-Konvention" for workflow binding (API format, optional sidecar).
- "Entfernen, Comfy ersetzt ihn" — remove the Local AI helper; old nodes move to ComfyUI.
- Ship "FLUX.1 Fill [dev], Erase/Remove, Comfy API-Nodes".
- Live test: "Ja, ein kleines Modell" — SDXL inpainting checkpoint in the local ComfyUI.
- Mid-run requests (2026-10-15): dropdown must reflect current models (GPT 2.5, Ideogram); multi-line prompt; show cost before generating and credit balance where a subscription exists; choose processing resolution 1K/2K/4K.
- "Ja, autark bis Merge" — implement, PR and merge into claussteinmassl/GenFill without further questions.

---

## File Structure

| File | Responsibility |
|---|---|
| `nuke/python/genfill/backends/comfy_workflows.py` (new) | Find, load, validate, describe and patch workflows. Pure. |
| `nuke/python/genfill/backends/comfyui.py` (new) | HTTP client for a ComfyUI server + `ComfyUIBackend`. |
| `nuke/python/genfill/config.py` | `comfyui_url()`, `comfyui_token()`, `comfyui_workflow_dirs()`, `set_comfyui()`. |
| `nuke/workflows/comfyui/*.json` (new) | Shipped workflows + sidecars. |
| `tests/fixtures/comfyui/sdxl_inpaint*.json` (new) | Test-only SDXL workflow for the live check. |
| `tests/test_comfy_workflows.py`, `tests/test_comfyui.py` (new), `tests/fake_servers.py` | Unit tests, fake ComfyUI server. |
| `nuke/python/genfill/ui/settings.py` | ComfyUI section: URL, token, Test connection. |
| Removed | `backends/local.py`, `localai/`, `ui/localai_dialog.py`, `tests/test_localai.py`, `tests/test_installer.py`, Local AI menus/button/engine branch. |

---

### Task 1: Workflow library

**Goal:** `comfy_workflows.py` loads API-format workflows with sidecars from folders, validates the title convention and patches a graph for one variation.

**Files:**
- Create: `nuke/python/genfill/backends/comfy_workflows.py`
- Test: `tests/test_comfy_workflows.py`

**Acceptance Criteria:**
- [ ] `load_dir(path)` returns `{id: Workflow}` for every `<id>.json` that is not a sidecar; a broken JSON or a graph without `GenFill Image`/`GenFill Mask` yields a `Workflow` with `error` set (listed, not raised).
- [ ] `library(dirs)` merges folders in order; later folders override earlier ids; result cached per (path, mtime) set.
- [ ] UI-format JSON (has top-level `"nodes"` list) gets error "saved in UI format; use Export (API)".
- [ ] `Workflow.patch(image, mask, prompt, negative, seed)` returns a deep copy with inputs set as in the spec table; seed applied to `GenFill Seed` node, else to every `seed`/`noise_seed` input; returns `(graph, output_ids)` where output_ids are `GenFill Output` node ids or every `SaveImage`.
- [ ] `Workflow.spec()` returns a `base.ModelSpec(id, label, kind, erase=empty_prompt, max_count=10, note=description)`.
- [ ] `Workflow.model_size(w, h)` uses sidecar `pixels` (default 1048576), `multiple` (64), `min_side` (512), `max_side` (2048) through `region.scale_to_pixel_budget`.

**Verify:** `python3 -m unittest tests.test_comfy_workflows -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests** (`tests/test_comfy_workflows.py`)

```python
"""Tests for ComfyUI workflow loading and patching."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill.backends import comfy_workflows as cw  # noqa: E402

GRAPH = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}, "_meta": {"title": "GenFill Image"}},
    "2": {"class_type": "LoadImageMask", "inputs": {"image": "m.png", "channel": "red"},
          "_meta": {"title": "genfill mask"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["9", 0]},
          "_meta": {"title": "GenFill Prompt"}},
    "4": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}, "_meta": {"title": "KSampler"}},
    "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0], "filename_prefix": "a"},
          "_meta": {"title": "GenFill Output"}},
    "6": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}, "_meta": {"title": "debug"}},
}


def write(folder, wid, graph, sidecar=None):
    with open(os.path.join(folder, wid + ".json"), "w") as fh:
        json.dump(graph, fh)
    if sidecar is not None:
        with open(os.path.join(folder, wid + ".genfill.json"), "w") as fh:
            json.dump(sidecar, fh)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_load_with_and_without_sidecar(self):
        write(self.tmp, "fill", GRAPH, {"label": "Fill", "kind": "inpaint", "empty_prompt": "erase",
                                         "pixels": 262144, "multiple": 16})
        write(self.tmp, "plain", GRAPH)
        lib = cw.load_dir(self.tmp)
        self.assertEqual(sorted(lib), ["fill", "plain"])
        self.assertEqual(lib["fill"].label, "Fill")
        self.assertEqual(lib["plain"].label, "plain")
        spec = lib["fill"].spec()
        self.assertEqual((spec.id, spec.erase, spec.kind), ("fill", "erase", "inpaint"))
        w, h = lib["fill"].model_size(1000, 500)
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertLessEqual(w * h, 262144 * 1.05)

    def test_invalid_workflows_are_listed_with_an_error(self):
        g = dict(GRAPH)
        del g["2"]
        write(self.tmp, "nomask", g)
        write(self.tmp, "ui", {"nodes": [], "links": []})
        with open(os.path.join(self.tmp, "broken.json"), "w") as fh:
            fh.write("{nope")
        lib = cw.load_dir(self.tmp)
        self.assertIn("GenFill Mask", lib["nomask"].error)
        self.assertIn("Export (API)", lib["ui"].error)
        self.assertTrue(lib["broken"].error)

    def test_later_folders_override(self):
        other = tempfile.mkdtemp()
        try:
            write(self.tmp, "fill", GRAPH, {"label": "Repo"})
            write(other, "fill", GRAPH, {"label": "Studio"})
            lib = cw.library([self.tmp, other, os.path.join(self.tmp, "missing")])
            self.assertEqual(lib["fill"].label, "Studio")
        finally:
            shutil.rmtree(other)


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write(self.tmp, "fill", GRAPH)
        self.wf = cw.load_dir(self.tmp)["fill"]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_patch_sets_inputs_and_keeps_original(self):
        graph, outputs = self.wf.patch("genfill/a.png", "genfill/m.png", "a crate", "", 42)
        self.assertEqual(graph["1"]["inputs"]["image"], "genfill/a.png")
        self.assertEqual(graph["2"]["inputs"]["image"], "genfill/m.png")
        self.assertEqual(graph["3"]["inputs"]["text"], "a crate")
        self.assertEqual(graph["4"]["inputs"]["seed"], 42)       # no GenFill Seed: every seed
        self.assertEqual(outputs, ["5"])
        self.assertEqual(self.wf.graph["3"]["inputs"]["text"], "")  # original untouched

    def test_seed_node_and_default_outputs(self):
        g = json.loads(json.dumps(GRAPH))
        g["7"] = {"class_type": "PrimitiveInt", "inputs": {"value": 0}, "_meta": {"title": "GenFill Seed"}}
        g["5"]["_meta"]["title"] = "Save"
        write(self.tmp, "seeded", g)
        wf = cw.load_dir(self.tmp)["seeded"]
        graph, outputs = wf.patch("a", "m", "", "", 7)
        self.assertEqual(graph["7"]["inputs"]["value"], 7)
        self.assertEqual(graph["4"]["inputs"]["seed"], 1)        # left alone
        self.assertEqual(sorted(outputs), ["5", "6"])

    def test_mask_kind(self):
        self.assertEqual(self.wf.mask_loader(), "LoadImageMask")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_comfy_workflows -v` → ImportError (module missing).

- [ ] **Step 3: Implement `comfy_workflows.py`**

```python
"""ComfyUI workflows GenFill can run: discovery, validation and per-variation patching.

A workflow is a ComfyUI graph in **API format** (ComfyUI: Workflow > Export (API)) saved as
``<id>.json``, with an optional ``<id>.genfill.json`` sidecar::

    {"label": "FLUX.1 Fill [dev]", "description": "...", "kind": "inpaint",
     "empty_prompt": "flux_fill_erase", "pixels": 1048576, "multiple": 16,
     "min_side": 512, "max_side": 2048, "api_nodes": false, "timeout": 1800,
     "requires": ["flux1-fill-dev.safetensors"]}

GenFill finds its inputs by node title (``_meta.title``, case-insensitive):

==================  =====================================================================
GenFill Image       LoadImage: the plate crop
GenFill Mask        LoadImageMask (channel red, white = fill), or LoadImage (mask from alpha)
GenFill Prompt      a node with a ``text`` / ``prompt`` / ``value`` / ``string`` input
GenFill Negative    same, for the negative prompt (optional)
GenFill Seed        a node with ``seed`` / ``noise_seed`` / ``value``; without one, every
                    ``seed`` / ``noise_seed`` input in the graph gets the seed
GenFill Output      the SaveImage / PreviewImage to read; without one, every SaveImage
==================  =====================================================================
"""
from __future__ import annotations

import copy
import json
import os

from .base import ModelSpec
from .. import region

T_IMAGE, T_MASK, T_PROMPT = "genfill image", "genfill mask", "genfill prompt"
T_NEGATIVE, T_SEED, T_OUTPUT = "genfill negative", "genfill seed", "genfill output"
TEXT_INPUTS = ("text", "prompt", "value", "string")
SEED_INPUTS = ("seed", "noise_seed")
SIDECAR = ".genfill.json"
REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", "..", "workflows", "comfyui"))

_cache = {}


class Workflow(object):
    """One workflow file plus its sidecar settings."""

    def __init__(self, wid, path, graph=None, meta=None, error=""):
        self.id = wid
        self.path = path
        self.graph = graph or {}
        self.meta = meta or {}
        self.error = error
        self.label = self.meta.get("label") or wid
        self.description = self.meta.get("description", "")
        self.kind = self.meta.get("kind", "inpaint")
        self.empty_prompt = self.meta.get("empty_prompt") or None
        self.api_nodes = bool(self.meta.get("api_nodes"))
        self.timeout = float(self.meta.get("timeout", 1800))

    def spec(self):
        note = self.description if not self.error else "Not usable: " + self.error
        return ModelSpec(self.id, self.label, kind=self.kind, erase=self.empty_prompt,
                         max_count=10, note=note)

    def model_size(self, width, height):
        m = self.meta
        return region.scale_to_pixel_budget(width, height, int(m.get("pixels", 1048576)),
                                            int(m.get("multiple", 64)),
                                            min_side=int(m.get("min_side", 512)),
                                            max_side=int(m.get("max_side", 2048)),
                                            max_aspect=3.0)

    def nodes_titled(self, title):
        return [nid for nid, n in self.graph.items() if _title(n) == title]

    def mask_loader(self):
        ids = self.nodes_titled(T_MASK)
        return self.graph[ids[0]].get("class_type", "") if ids else ""

    def patch(self, image, mask, prompt, negative, seed):
        """A copy of the graph for one variation.

        Returns:
            ``(graph, output_node_ids)``.
        """
        g = copy.deepcopy(self.graph)
        for nid in self.nodes_titled(T_IMAGE):
            g[nid]["inputs"]["image"] = image
        for nid in self.nodes_titled(T_MASK):
            g[nid]["inputs"]["image"] = mask
        _set_text(g, self.nodes_titled(T_PROMPT), prompt or "")
        _set_text(g, self.nodes_titled(T_NEGATIVE), negative or "")
        seed_nodes = self.nodes_titled(T_SEED)
        if seed_nodes:
            for nid in seed_nodes:
                ins = g[nid]["inputs"]
                key = next((k for k in SEED_INPUTS + ("value",) if k in ins), "seed")
                ins[key] = seed
        else:
            for n in g.values():
                for k in SEED_INPUTS:
                    if k in n.get("inputs", {}) and not isinstance(n["inputs"][k], list):
                        n["inputs"][k] = seed
        outputs = self.nodes_titled(T_OUTPUT) or sorted(
            nid for nid, n in g.items() if n.get("class_type") == "SaveImage")
        return g, outputs


def _title(node):
    return str((node.get("_meta") or {}).get("title", "")).strip().lower()


def _set_text(graph, ids, text):
    for nid in ids:
        ins = graph[nid]["inputs"]
        key = next((k for k in TEXT_INPUTS if k in ins and not isinstance(ins[k], list)), "text")
        ins[key] = text


def validate(graph):
    """Error message for a graph GenFill cannot drive, or ``""``."""
    if isinstance(graph, dict) and isinstance(graph.get("nodes"), list):
        return "saved in UI format; in ComfyUI use Workflow > Export (API)"
    if not isinstance(graph, dict) or not graph or not all(
            isinstance(n, dict) and "class_type" in n for n in graph.values()):
        return "not a ComfyUI API-format graph"
    titles = set(_title(n) for n in graph.values())
    missing = [t for t, name in ((T_IMAGE, "GenFill Image"), (T_MASK, "GenFill Mask"))
               if t not in titles]
    if missing:
        return "no node titled %s" % " or ".join(
            {"genfill image": "'GenFill Image'", "genfill mask": "'GenFill Mask'"}[t] for t in missing)
    return ""


def load(path):
    """Load one ``<id>.json`` (and its sidecar). Never raises: problems go to ``error``."""
    wid = os.path.basename(path)[:-len(".json")]
    meta, graph, error = {}, {}, ""
    side = os.path.join(os.path.dirname(path), wid + SIDECAR)
    try:
        if os.path.exists(side):
            with open(side, "r") as fh:
                meta = json.load(fh)
        with open(path, "r") as fh:
            graph = json.load(fh)
        error = validate(graph)
    except (IOError, OSError, ValueError) as exc:
        error = "cannot read: %s" % exc
    return Workflow(wid, path, graph if not error else {}, meta if isinstance(meta, dict) else {},
                    error)


def load_dir(folder):
    out = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if name.endswith(".json") and not name.endswith(SIDECAR):
            wf = load(os.path.join(folder, name))
            out[wf.id] = wf
    return out


def _stamp(folder):
    try:
        return tuple(sorted((n, os.path.getmtime(os.path.join(folder, n)))
                            for n in os.listdir(folder) if n.endswith(".json")))
    except OSError:
        return ()


def library(dirs):
    """All workflows of ``dirs`` (later folders win), re-read only when a file changed."""
    key = tuple(dirs)
    stamp = tuple(_stamp(d) for d in dirs)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    lib = {}
    for d in dirs:
        lib.update(load_dir(d))
    _cache[key] = (stamp, lib)
    return lib
```

Check `region.scale_to_pixel_budget` signature first (`grep -n "def scale_to_pixel_budget" nuke/python/genfill/region.py`) and match keyword names.

- [ ] **Step 4: Run tests** → `python3 -m unittest tests.test_comfy_workflows -v` all pass; full suite still green.

- [ ] **Step 5: Commit** `git add nuke/python/genfill/backends/comfy_workflows.py tests/test_comfy_workflows.py && git commit -m "feat: ComfyUI workflow library (title convention, sidecars)"` (with the session trailer).

```json:metadata
{"files": ["nuke/python/genfill/backends/comfy_workflows.py", "tests/test_comfy_workflows.py"], "verifyCommand": "python3 -m unittest tests.test_comfy_workflows -v", "acceptanceCriteria": ["load_dir lists invalid workflows with error", "library: later dirs override, mtime cache", "UI-format error mentions Export (API)", "patch sets image/mask/prompt/negative/seed and returns output ids", "spec() returns ModelSpec with erase=empty_prompt", "model_size uses sidecar pixel budget"], "modelTier": "mechanical"}
```

---

### Task 2: ComfyUI backend and settings

**Goal:** `ComfyUIBackend` runs a library workflow against a ComfyUI server end to end (upload, queue, poll, download, cancel) with settings from env/config.

**Files:**
- Create: `nuke/python/genfill/backends/comfyui.py`
- Modify: `nuke/python/genfill/config.py` (getters), `nuke/python/genfill/backends/__init__.py` (register first), `tests/fake_servers.py` (add `FakeComfyServer`), `tests/test_core.py:196` (backend id list), `installers/install.ps1:66-73` (drop `backends\comfyui.py` and `tests\test_comfyui.py` from `$Obsolete` — otherwise the installer deletes the new files)
- Test: `tests/test_comfyui.py`

**Acceptance Criteria:**
- [ ] `config.comfyui_url()` = `GENFILL_COMFY_URL` > `comfyui.url` > `http://127.0.0.1:8188` (trailing `/` stripped); `comfyui_token()` = `GENFILL_COMFY_TOKEN` > `api_keys.comfyui`; `comfyui_workflow_dirs()` = `[REPO_DIR] + config dirs + env dirs` (env split by `os.pathsep`, empties dropped); `set_comfyui(url=None, workflow_dirs=None)` writes config.
- [ ] Backend attributes: `id="comfyui"`, `label="ComfyUI (local, network or cloud)"`, `needs_key=False`, `auth="key"`, `env_var="GENFILL_COMFY_TOKEN"`, `default_model="flux_fill"`, `catalogue` is a property from the library (valid and invalid workflows), `model_size` per workflow.
- [ ] `generate`: unknown workflow id → `BackendError` listing available ids and folders; invalid workflow → its error; empty prompt + `empty_prompt` set → that workflow; uploads `source.png` and the mask (`mask.png` for LoadImageMask, `mask_alpha.png` for LoadImage) via `POST /upload/image` (subfolder `genfill`, overwrite true, unique names); queues `count` graphs with seeds `seed+i` (random base if 0, kept below 2**31); sends `Authorization: Bearer <token>` when a token is set and `extra_data.api_key_comfy_org` only for `api_nodes` workflows (error if no key).
- [ ] `/prompt` 400 with `node_errors` → readable `BackendError`; `value_not_in_list` on a loader names the file and folder (`CheckpointLoaderSimple`→`checkpoints`, `UNETLoader`→`diffusion_models`, `DualCLIPLoader`/`CLIPLoader`→`text_encoders`, `VAELoader`→`vae`, `LoraLoader`→`loras`, else `models`).
- [ ] Polls `GET /history/<id>` every 1 s; execution errors (`status.status_str == "error"`, messages `execution_error`) become errors per variation; downloads every image of the output nodes via `/view`; returns `cloud.finish(...)`; progress messages include queue position from `/queue`.
- [ ] Cancel: `POST /queue {"delete": [pending ids]}` and `POST /interrupt` with `{"prompt_id": running id}`; raises `Cancelled`. Timeout per workflow → error and same cleanup.
- [ ] `server_info(url=None, token=None)` → `GET /system_stats` → `{"version", "devices"}`; network error → `BackendError` naming the URL.

**Verify:** `python3 -m unittest tests.test_comfyui tests.test_core -v` → all pass

**Steps:**

- [ ] **Step 1: Fake server** in `tests/fake_servers.py`: `FakeComfyServer(workflow_result=None, fail_prompt=None, exec_error=False, hold=False)` on 127.0.0.1:0 implementing `POST /upload/image` (parse multipart minimally: record field names and filename, reply `{"name": filename, "subfolder": "genfill", "type": "input"}`), `POST /prompt` (record body; reply `{"prompt_id": "p<n>", "number": n, "node_errors": {}}` or 400 with `fail_prompt` dict), `GET /history/<id>` (`{}` while `hold` or for the first poll, then `{id: {"status": {"status_str": "success", "completed": true, "messages": []}, "outputs": {"<out>": {"images": [{"filename": "r<n>.png", "subfolder": "", "type": "output"}]}}}}` using the prompt's `GenFill Output` id; error variant for `exec_error`), `GET /view` (a tiny valid PNG from `genfill.pngio`), `GET /queue`, `POST /queue`, `POST /interrupt`, `GET /system_stats`. Record `requests` as `(method, path, headers, body)`.

- [ ] **Step 2: Write failing tests** (`tests/test_comfyui.py`): a temp workflow dir with the `GRAPH` from Task 1 (copy it into this file), env patched with `GENFILL_COMFY_URL=<fake base>`, `GENFILL_COMFY_WORKFLOWS=<tmp>`; tests:
  - `test_generate_end_to_end`: count 2 → 2 PNGs; 2 uploads; 2 prompts with seeds `s`, `s+1`; prompt text set; output downloaded.
  - `test_token_header_and_api_key`: token → `Authorization: Bearer t` on every request; `api_nodes` sidecar + `COMFY_API_KEY=k` → `extra_data.api_key_comfy_org == "k"`; without key → `BackendError` mentioning `COMFY_API_KEY`; non-api workflow → no `extra_data` key.
  - `test_missing_model_message`: `fail_prompt={"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"}, "node_errors": {"4": {"class_type": "CheckpointLoaderSimple", "errors": [{"type": "value_not_in_list", "message": "Value not in list", "details": "ckpt_name: 'sd.safetensors' not in []", "extra_info": {"input_name": "ckpt_name"}}]}}}` → message contains `sd.safetensors` and `models/checkpoints`.
  - `test_execution_error_and_partial_results`: one prompt errors, one succeeds → 1 image returned; all fail → `BackendError` with the exception message.
  - `test_cancel_deletes_and_interrupts`: `hold=True`, `cancelled` flips after the first poll → `Cancelled`; fake saw `POST /queue` with delete ids and `POST /interrupt`.
  - `test_empty_prompt_uses_erase_workflow` and `test_unknown_workflow_lists_available`.
  - `test_config_precedence` for the three getters (patch `config.load`).
  - `test_server_info` and unreachable URL error.

- [ ] **Step 3: Implement** `config.py` getters (same `_env` helper style as the naming getters) and `backends/comfyui.py`:

```python
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

    def workflow(self, model):
        ...  # resolve id (default_model if empty); errors as in the acceptance criteria

    def model_size(self, width, height, model=""):
        try:
            return self.workflow(model).model_size(width, height)
        except BackendError:
            return region.scale_to_pixel_budget(width, height, 1024 * 1024, 64, min_side=512,
                                                max_side=2048, max_aspect=3.0)
```

  A small `ComfyClient(url, token, timeout)` wraps `base.http_request` / `http_json` / `encode_multipart` (`get_json(path)`, `post_json(path, obj)`, `upload(path_on_disk, name)`, `view(image_dict)`), so the backend reads as the five steps of the spec. Use `cloud.poll`, `cloud.pause`, `cloud.collect`, `cloud.finish`, `cloud.progress_step` (import from `.cloud`). `config` is imported lazily inside methods (backends stay importable without settings side effects, like the other backends).

- [ ] **Step 4: Register** `ComfyUIBackend()` first in `_BACKENDS`; update `test_core.py` id list; fix `install.ps1` `$Obsolete`.

- [ ] **Step 5: Run** `python3 -m unittest discover -s tests` → green. **Commit** explicit paths, message `feat: ComfyUI backend (local, network or cloud instances)`.

```json:metadata
{"files": ["nuke/python/genfill/backends/comfyui.py", "nuke/python/genfill/config.py", "nuke/python/genfill/backends/__init__.py", "tests/fake_servers.py", "tests/test_comfyui.py", "tests/test_core.py", "installers/install.ps1"], "verifyCommand": "python3 -m unittest tests.test_comfyui tests.test_core -v", "acceptanceCriteria": ["config getters with env precedence", "backend attributes and dynamic catalogue", "upload/queue/poll/download with seeds and auth", "readable node_errors incl. missing model folder", "execution errors per variation, paid-results policy", "cancel deletes queue entries and interrupts", "server_info", "installer obsolete list no longer deletes comfyui.py"], "modelTier": "standard"}
```

---

### Task 3: Shipped workflows

**Goal:** The repo ships working API-format workflows for FLUX.1 Fill, FLUX erase and the Comfy API nodes, plus a test-only SDXL inpaint workflow.

**Files:**
- Create: `nuke/workflows/comfyui/{flux_fill,flux_fill_erase,api_flux_pro_fill,api_flux_erase,api_bria_genfill,api_bria_eraser,api_recraft_inpaint}.json` and matching `.genfill.json`
- Create: `nuke/workflows/comfyui/README.md` (title convention, sidecar keys, how to add a workflow, required model files and folders)
- Create: `tests/fixtures/comfyui/sdxl_inpaint.json`, `sdxl_inpaint.genfill.json`
- Test: `tests/test_comfy_workflows.py` (add `ShippedTests`)

**Acceptance Criteria:**
- [ ] Every shipped workflow loads with `error == ""`; `flux_fill` sidecar sets `empty_prompt: "flux_fill_erase"`; `api_flux_pro_fill` → `api_flux_erase`; `api_bria_genfill` → `api_bria_eraser`; `api_recraft_inpaint` → `api_flux_erase`; api sidecars have `"api_nodes": true`.
- [ ] FLUX graph: `UNETLoader` (`flux1-fill-dev.safetensors`, weight_dtype default), `DualCLIPLoader` (`clip_l.safetensors`, `t5xxl_fp8_e4m3fn.safetensors`, type `flux`), `VAELoader` (`ae.safetensors`), `CLIPTextEncode` "GenFill Prompt", `FluxGuidance` 30, negative `CLIPTextEncode` "" (`ConditioningZeroOut` fine too), `DifferentialDiffusion`, `InpaintModelConditioning` (noise_mask true), `KSampler` (steps 28, cfg 1, euler, normal, denoise 1), `VAEDecode`, `SaveImage` "GenFill Output", `LoadImage` "GenFill Image", `LoadImageMask` "GenFill Mask" channel red. Sidecar: pixels 1048576, multiple 16, `requires` lists the four files with folders.
- [ ] Erase graph: same as FLUX, prompt node titled `GenFill Prompt` must NOT exist; a fixed CLIPTextEncode text "clean empty background, seamless continuation of the surrounding surfaces and textures, nothing in the masked area". Sidecar `kind: "erase"`.
- [ ] API graphs use the exact node inputs from the local ComfyUI 0.36 `object_info` (FluxProFillNode: image, mask, prompt, prompt_upsampling false, guidance 60, steps 50, seed; FluxEraseNode: image, mask, dilate_pixels 5, seed; BriaGenFill: image, mask, prompt, negative_prompt, refine_prompt true, seed, moderation per object_info; BriaEraser: image, mask, mask_type, moderation; RecraftImageInpaintingNode: image, mask, prompt, n 1, seed). Fetch exact option values with `curl -s http://127.0.0.1:8188/object_info/<Node>`; mask from `LoadImageMask` channel red.
- [ ] SDXL fixture: `CheckpointLoaderSimple` `sd_xl_base_1.0_inpainting_0.1.safetensors`, `VAEEncodeForInpaint` (grow_mask_by 8) or `InpaintModelConditioning`, `KSampler` steps 20 cfg 7 dpmpp_2m karras, sidecar pixels 1048576 multiple 64.
- [ ] `POST /prompt` of every shipped workflow (patched with an uploaded test image) to the local ComfyUI at `http://127.0.0.1:8188` returns either success or only `value_not_in_list` errors on loader model inputs (FLUX models are not installed); API workflows queue successfully or fail only for the missing account key — never a graph/type error. Do not leave paid API jobs running: for API workflows only validate without an api key (they fail at execution for auth, which costs nothing), then `POST /queue {"clear": true}` is NOT allowed (user's queue) — delete only the ids you queued.

**Verify:** `python3 -m unittest tests.test_comfy_workflows -v` → all pass, plus the live validation script output recorded in the report.

```json:metadata
{"files": ["nuke/workflows/comfyui/", "tests/fixtures/comfyui/", "tests/test_comfy_workflows.py"], "verifyCommand": "python3 -m unittest tests.test_comfy_workflows -v", "acceptanceCriteria": ["all shipped workflows load without error", "empty_prompt links set", "graphs use exact object_info inputs", "live /prompt validation only reports missing models or missing key"], "modelTier": "standard"}
```

---

### Task 4: Remove Local AI, settings dialog, node upgrade

**Goal:** The Local AI helper is gone, ComfyUI is the default backend, old nodes on "Local" move to ComfyUI, and the API Keys & Accounts dialog has a ComfyUI section.

**Files:**
- Delete: `nuke/python/genfill/backends/local.py`, `nuke/python/genfill/localai/` (whole package), `nuke/python/genfill/ui/localai_dialog.py`, `tests/test_localai.py`, `tests/test_installer.py`
- Modify: `nuke/python/genfill/backends/__init__.py`, `nuke/python/genfill/engine.py:494-508` (drop the `is_installed` branch), `nuke/menu.py` (drop Local AI menus), `nuke/python/genfill/ui/panel.py:311-317,430-435` (drop the Local AI button and handler), `nuke/python/genfill/devtools.py:63-70` (drop helper stop), `nuke/python/genfill/node.py` (`ensure_current`: an old label starting with `Local:` → ComfyUI label, `gf_model` cleared; tooltip text that mentions the local model), `nuke/python/genfill/ui/settings.py`, `installers/install.ps1` (`$Obsolete` += removed localai files so dev copies get cleaned), `tests/test_pipeline.py`
- Test: `tests/test_pipeline.py` (`test_local_backend_moves_to_comfyui`), existing suite

**Acceptance Criteria:**
- [ ] `grep -rn "localai\|LocalBackend\|Local AI" nuke/ tests/` → no hits (README/CHANGELOG handled in Task 5).
- [ ] A node whose `gf_backend` value is `Local: FLUX.1 Fill (free, runs on your GPU)` gets `ComfyUI (local, network or cloud)` after `ensure_current`, `gf_model` == "".
- [ ] Settings dialog: group "ComfyUI" with URL field (placeholder shows effective URL and source; read-only hint if `GENFILL_COMFY_URL` set), token field (password echo, toggled by "Show keys"), a "Test connection" button that runs `ComfyUIBackend().server_info(url, token)` on a worker thread and shows "ComfyUI 0.36.0 on mps" or the error (same `_poll` timer pattern as sign-in; no Qt off-thread), and a read-only label listing the workflow folders. Save writes `config.set_comfyui(url=...)` and `config.set_api_key("comfyui", token)`.
- [ ] Full suite green.

**Verify:** `python3 -m unittest discover -s tests` → OK; `grep -rn "localai\|LocalBackend\|Local AI" nuke tests` → empty

```json:metadata
{"files": ["nuke/python/genfill/backends/__init__.py", "nuke/python/genfill/engine.py", "nuke/menu.py", "nuke/python/genfill/ui/panel.py", "nuke/python/genfill/ui/settings.py", "nuke/python/genfill/devtools.py", "nuke/python/genfill/node.py", "installers/install.ps1", "tests/test_pipeline.py"], "verifyCommand": "python3 -m unittest discover -s tests", "acceptanceCriteria": ["no Local AI references in nuke/ and tests/", "old Local nodes upgrade to ComfyUI", "settings dialog ComfyUI section with thread-safe Test connection", "suite green"], "modelTier": "standard"}
```

---

### Task 5: Docs, live check, version 0.9.0

**Goal:** README, ARCHITECTURE, installers/README, CHANGELOG describe ComfyUI and no longer mention Local AI; `live_check.py` runs ComfyUI workflows; version 0.9.0.

**Files:**
- Modify: `README.md` (Install §2 becomes "ComfyUI (optional, recommended)": URL, env vars, model files per shipped workflow and their folders, RunPod example `https://<pod-id>-8188.proxy.runpod.net`; Models section; Pipeline env table gains the three `GENFILL_COMFY_*` vars; Privacy table; Troubleshooting: "model missing", "cannot reach ComfyUI"; Development tree), `docs/ARCHITECTURE.md`, `installers/README.md`, `CHANGELOG.md` (0.9.0 entry), `nuke/python/genfill/__init__.py` (`__version__ = "0.9.0"`), `nuke/python/genfill/backends/base.py` (USER_AGENT `GenFill-Nuke/0.9`), `installers/live_check.py` (works unchanged with `comfyui <workflow id>`; add `--workflows DIR` that sets `GENFILL_COMFY_WORKFLOWS`)

**Acceptance Criteria:**
- [ ] `grep -n "Local AI\|GenFill-LocalAI\|LaMa" README.md docs/ARCHITECTURE.md installers/README.md` → only historical CHANGELOG mentions remain (none in these three files).
- [ ] README documents the title convention table and links `nuke/workflows/comfyui/README.md`.
- [ ] `python installers/live_check.py comfyui sdxl_inpaint --workflows tests/fixtures/comfyui` is documented in installers/README.md.

**Verify:** `python3 -m unittest discover -s tests` → OK; the grep above → empty

```json:metadata
{"files": ["README.md", "docs/ARCHITECTURE.md", "installers/README.md", "CHANGELOG.md", "nuke/python/genfill/__init__.py", "nuke/python/genfill/backends/base.py", "installers/live_check.py"], "verifyCommand": "python3 -m unittest discover -s tests", "acceptanceCriteria": ["no Local AI in README/ARCHITECTURE/installers README", "title convention documented", "live_check --workflows documented", "version 0.9.0"], "modelTier": "mechanical"}
```

---

### Task 7: Current model catalogues (static refresh)

**Goal:** The curated catalogues of Magnific, Higgsfield, Comfy Cloud and fal list the provider's current inpaint/edit models with correct ids, labels and parameters (GPT 2.5, Ideogram 4.5, Flux.2 Pro, Seedream 5 Lite/Flash, Nano Banana 2.1, ...).

**Files:**
- Modify: `nuke/python/genfill/backends/magnific.py`, `higgsfield.py`, `comfy_cloud.py`, `fal.py`
- Test: `tests/test_magnific.py`, `tests/test_higgsfield.py`, `tests/test_comfy_cloud.py`, `tests/test_fal.py`

**Acceptance Criteria:**
- [ ] Every "add"/"rename" recommendation of the catalogue-diff tables in `docs/superpowers/research/research-models-costs.md` §1a-1d is applied, except entries marked "skip"; private/beta provider models get " (beta)" in the label and a note "may need account access".
- [ ] Magnific: `gpt-2-mini` "GPT 2.5" edit model and `gpt-2` pass `quality` (GenFill low/medium/high → model value; `auto` = omit); `retouch-inpaint-lab-gpt-2-mini` "Retouch GPT 2.5 (beta)"; `retouch-ideogram-4-5` "Retouch Ideogram 4.5 (beta)"; `flux-2` label "Flux.2 Pro"; `seedream-5-lite`, `seedream-5-flash` added.
- [ ] Higgsfield: models whose `models_explore` roles include `mask` (`nano_banana_2`, `nano_banana_2_1`, `nano_banana_2_lite`, `ideogram_4_5`) send the GenFill mask as a `mask` media (uploaded like the image) and become `kind="inpaint"`; others stay reference edits. Verify the exact role names/param shape with `mcp__claude_ai_Higgsfield__models_explore` (read-only) before coding.
- [ ] Comfy Cloud: add `openai/images-generations` GPT Image 2.5 (flare, sunburst via `params.model`), `ideogram/generate` V3 edit with mask, `bfl/flux-2-max`, `xai/grok-image-edit`, with param shapes from `get_prompting_guide(model:"partner")` (read-only).
- [ ] fal: add `ideogram/v4.5/edit`, `openai/gpt-image-2.5/flare/edit`, `openai/gpt-image-2.5/sunburst/edit` (mask), `google/nano-banana-2.1/edit`; input field names taken from `GET https://api.fal.ai/v1/models?endpoint_id=<id>&expand=openapi-3.0` (no key needed).
- [ ] Unit tests cover the new request shapes (quality passthrough, Higgsfield mask media, fal mask field per new endpoint); full suite green. No paid calls.

**Verify:** `python3 -m unittest tests.test_magnific tests.test_higgsfield tests.test_comfy_cloud tests.test_fal -v` → all pass

```json:metadata
{"files": ["nuke/python/genfill/backends/magnific.py", "nuke/python/genfill/backends/higgsfield.py", "nuke/python/genfill/backends/comfy_cloud.py", "nuke/python/genfill/backends/fal.py", "tests/test_magnific.py", "tests/test_higgsfield.py", "tests/test_comfy_cloud.py", "tests/test_fal.py"], "verifyCommand": "python3 -m unittest tests.test_magnific tests.test_higgsfield tests.test_comfy_cloud tests.test_fal -v", "acceptanceCriteria": ["research diff recommendations applied", "Magnific GPT 2.5 + quality passthrough", "Higgsfield mask role for inpaint models", "Comfy Cloud and fal new endpoints with verified fields", "tests for new shapes, suite green"], "modelTier": "standard"}
```

---

### Task 8: Live model discovery

**Goal:** The version dropdown also shows models a provider added after this release: Magnific, Higgsfield and fal catalogues are fetched live, cached for 24 h and merged with the curated list.

**Files:**
- Create: `nuke/python/genfill/backends/discovery.py` (cache file `~/.nuke/genfill/models_cache.json`, TTL, merge)
- Modify: `nuke/python/genfill/backends/base.py` (`Backend.discover(api_key)` default `[]`, `Backend.model_ids()/model_spec()` include discovered specs), `magnific.py`, `higgsfield.py`, `fal.py`, `nuke/python/genfill/ui/panel.py` (version combo: "Refresh model list" entry at the end that runs discovery on a worker thread; discovered-only entries labelled "<label> (new)")
- Test: `tests/test_discovery.py`

**Acceptance Criteria:**
- [ ] Magnific: `retouch_models_list` (text, parse per research §1a) + `images_models_list` (JSON); edit entries only with `supportsReferences` and "image" in `referenceTypes`; aspect ratios/customSize/resolutions carried in `ModelSpec.extra`. Higgsfield: `models_explore {action:"list", type:"image"}`, entries with an image-reference or mask role. fal: `GET https://api.fal.ai/v1/models?category=image-to-image&limit=100&expand=openapi-3.0` (paged via `next_cursor` if present), entries whose input schema has `image_url`/`image_urls` (+ `mask_url` → inpaint).
- [ ] Curated entries always win (labels, params); discovered entries never replace them. Discovery never opens a browser (`interactive=False`); not signed in / no network → silently keeps the cache/curated list (logged).
- [ ] Cache: per backend, `{"fetched": iso, "models": [...]}`; reused within 24 h; "Refresh model list" forces a fetch.
- [ ] A discovered model runs through the backend's generic path (Magnific `images_generate`/`images_retouch`, Higgsfield `generate_image_batch`, fal generic `image_url`/`mask_url`/`prompt` payload) — unit-tested with the existing fakes.

**Verify:** `python3 -m unittest tests.test_discovery -v` → all pass; full suite green

```json:metadata
{"files": ["nuke/python/genfill/backends/discovery.py", "nuke/python/genfill/backends/base.py", "nuke/python/genfill/backends/magnific.py", "nuke/python/genfill/backends/higgsfield.py", "nuke/python/genfill/backends/fal.py", "nuke/python/genfill/ui/panel.py", "tests/test_discovery.py"], "verifyCommand": "python3 -m unittest tests.test_discovery -v", "acceptanceCriteria": ["live lists for Magnific/Higgsfield/fal", "curated wins, non-interactive, offline-safe", "24h cache + forced refresh", "discovered models run through generic path"], "modelTier": "standard"}
```

---

### Task 9: Resolution setting (Auto / 1K / 2K / 4K)

**Goal:** The artist chooses the processing resolution; every backend sends the crop at that size (within the model's limits) and tells the panel the exact size it will send.

**Files:**
- Modify: `nuke/python/genfill/node.py` (new knob `gf_resolution`, Enumeration `["auto", "1K", "2K", "4K"]`, default `auto`, next to quality; `ensure_current` adds it to old nodes), `nuke/python/genfill/backends/base.py` (`GenerationRequest.resolution`; `Backend.model_size(width, height, model="", resolution="auto")`; helper `resolution_budget(resolution, default_budget)` → 1K 1048576, 2K 4194304, 4K 16777216 pixels; `ModelSpec.extra["resolutions"]` = supported tiers), every backend's `model_size` and request params (Magnific `resolution` "1k"/"2k"/"4k" where the model lists it, Higgsfield `resolution`, Comfy Cloud/partner and fal size/`resolution` params, OpenAI max 3840x2160 for gpt-image-2+, Stability fixed ~1 MP, ComfyUI workflows: sidecar `pixels` is the 1K default and scales by tier up to `max_side`), `nuke/python/genfill/engine.py` (pass knob to `model_size` and request; log), `nuke/python/genfill/ui/panel.py` (resolution combo beside quality; info line "sends 2048 x 1152 (2K)")
- Test: `tests/test_core.py`, backend tests, `tests/test_pipeline.py`

**Acceptance Criteria:**
- [ ] `auto` keeps today's sizes exactly (existing tests unchanged).
- [ ] A tier above what the model supports is clamped to the model's largest tier, and the panel/log says so ("model max 2K").
- [ ] Never upscale beyond the crop: if the context region in plate pixels is smaller than the tier budget, the native crop size (rounded to the model's multiple, respecting the model minimum) is sent.
- [ ] Providers with a resolution parameter receive the matching value; tests assert it for Magnific, Higgsfield, fal, Comfy Cloud, OpenAI sizes.
- [ ] Old nodes get `gf_resolution` = auto via `ensure_current`; pipeline test covers 2K end to end with the mock backend.

**Verify:** `python3 -m unittest discover -s tests` → OK

```json:metadata
{"files": ["nuke/python/genfill/node.py", "nuke/python/genfill/backends/base.py", "nuke/python/genfill/backends/", "nuke/python/genfill/engine.py", "nuke/python/genfill/ui/panel.py", "tests/"], "verifyCommand": "python3 -m unittest discover -s tests", "acceptanceCriteria": ["auto unchanged", "clamp to model max with message", "no upscaling beyond crop", "provider resolution params set", "old nodes upgraded, 2K pipeline test"], "modelTier": "frontier"}
```

---

### Task 10: Multi-line prompt

**Goal:** Prompt (and negative prompt) are multi-line text fields in the panel and on the node.

**Files:**
- Modify: `nuke/python/genfill/ui/panel.py` (QPlainTextEdit, 3 lines tall, grows to 8, Ctrl/Cmd+Enter generates, Enter inserts a newline, placeholder kept), `nuke/python/genfill/node.py` (new nodes: `gf_prompt` and `gf_negative` as `nuke.Multiline_Eval_String_Knob`; old String_Knob nodes keep working — the panel reads/writes `.value()`), `nuke/python/genfill/engine.py` (`gf_info` summary uses the first line), `tests/fake_nuke.py` (Multiline knob if missing), `tests/test_pipeline.py`
- Test: `tests/test_pipeline.py` (`test_multiline_prompt_round_trip`: prompt with newlines, brackets and `$` survives generate, state knob and `gf_info` stays one line)

**Acceptance Criteria:**
- [ ] Newlines survive into `GenerationRequest.prompt` and the state; `gf_info` shows only the first line.
- [ ] TCL-sensitive characters in a multiline knob do not get evaluated (prompt read with `.value()`, never `.evaluate()`).
- [ ] Ctrl/Cmd+Enter generates; plain Enter adds a line.

**Verify:** `python3 -m unittest tests.test_pipeline -v` → all pass

```json:metadata
{"files": ["nuke/python/genfill/ui/panel.py", "nuke/python/genfill/node.py", "nuke/python/genfill/engine.py", "tests/fake_nuke.py", "tests/test_pipeline.py"], "verifyCommand": "python3 -m unittest tests.test_pipeline -v", "acceptanceCriteria": ["newlines survive, gf_info first line", "no TCL evaluation of prompt", "Ctrl/Cmd+Enter generates"], "modelTier": "standard"}
```

---

### Task 11: Cost preview and credit balance

**Goal:** Before generating, the panel shows what the current settings will cost, and for subscription providers the remaining credit balance.

**Files:**
- Create: `nuke/python/genfill/pricing.py` (static price tables: OpenAI per-image estimates by model/quality/size from token prices in research §2, Stability 5 credits/call, fal keyless price notes as fallback; `Quote(amount, unit, exact, note)`), `tests/test_pricing.py`
- Modify: `nuke/python/genfill/backends/base.py` (`Backend.estimate(request) -> Quote|None`, `Backend.balance(api_key) -> Balance|None`, both default None, never interactive), `magnific.py` (`simulate_cost` with the exact tool + args GenFill would send, no references; retouch = N x quote; `account_balance` → credits.available, plan productName), `higgsfield.py` (`generate_image` `get_cost:true` with count 1, total = per-image x count, `exact=False` if count>1; `balance` → credits, plan), `comfy_cloud.py` (partner: static note "billed per Comfy credits" + `estimate_credits` for graph models; no balance), `comfyui.py` (free unless `api_nodes`; then "Comfy credits, see comfy.org"), `fal.py` (pricing API with the user's key, `GET https://api.fal.ai/v1/models/pricing?endpoint_id=`, tolerant parsing; fallback table; no balance), `openai_images.py`, `stability.py` (balance `GET https://api.stability.ai/v1/user/balance`, tolerant), `mock.py` (free), `nuke/python/genfill/ui/panel.py` (one muted line under Generate: "≈ 30 credits · balance 243,317 credits (Premium+)" / "≈ $0.12" / "free (your GPU)"; refreshed on a worker thread, debounced 600 ms after model/version/resolution/quality/variations change, cached per settings key 5 min, balance cached 60 s and refreshed after each generation; errors show "cost unknown")
- Test: `tests/test_pricing.py`, backend tests (fakes return the research sample responses)

**Acceptance Criteria:**
- [ ] Estimates use the model size the backend would send for the current plate/selection (`model_size` with the last region if available, else the plate format) and the resolution/quality/count knobs.
- [ ] Magnific and Higgsfield quotes come from the live preflight tools (never submit work); unit tests assert no generation tool is called.
- [ ] Balance shown for Magnific and Higgsfield (and Stability if the endpoint answers); hidden for key-only services without a balance API (fal, OpenAI, Comfy).
- [ ] No sign-in prompt, no Qt access off the main thread, Generate never waits for a quote.

**Verify:** `python3 -m unittest tests.test_pricing tests.test_magnific tests.test_higgsfield -v` → all pass; full suite green

```json:metadata
{"files": ["nuke/python/genfill/pricing.py", "nuke/python/genfill/backends/base.py", "nuke/python/genfill/backends/", "nuke/python/genfill/ui/panel.py", "tests/test_pricing.py"], "verifyCommand": "python3 -m unittest tests.test_pricing tests.test_magnific tests.test_higgsfield -v", "acceptanceCriteria": ["estimate uses real send size and knobs", "live preflight quotes, no generation calls", "balance for Magnific/Higgsfield (+Stability)", "non-interactive, thread-safe, non-blocking"], "modelTier": "frontier"}
```

---

### Task 12: Live verification (controller)


**Goal:** A real generation through GenFill's own backend code on the local ComfyUI with the SDXL fixture, validation of all shipped workflows, and one cheap live check each of the new catalogue entries, resolution, cost quotes and balances (Magnific, Higgsfield).

**Files:** none (results go to `tasks/todo.md` review)

**Acceptance Criteria:**
- [ ] `python3 installers/live_check.py comfyui sdxl_inpaint --workflows tests/fixtures/comfyui --count 2` → `OK ... 2 image(s)`; images inspected (the red ball replaced by a crate).
- [ ] Erase path: `--prompt ""` with a sidecar `empty_prompt` pointing to a fixture erase workflow (or the same SDXL graph) → OK.
- [ ] Cancel against the real server leaves no GenFill prompt in `GET /queue`.
- [ ] Shipped workflow validation (Task 3 criterion) re-run on the final branch.

**Verify:** captured live_check output in the ledger

```json:metadata
{"files": [], "verifyCommand": "python3 installers/live_check.py comfyui sdxl_inpaint --workflows tests/fixtures/comfyui --count 2", "acceptanceCriteria": ["2 real images from local ComfyUI", "erase path works", "cancel leaves queue clean", "shipped workflows validate"], "modelTier": "standard"}
```

---

### Task 13: Final review, PR, merge

**Goal:** Final code review, PR against `claussteinmassl/GenFill` main, merge.

**Files:** none (fixes from review only)

**Acceptance Criteria:**
- [ ] Final reviewer findings fixed or parked with rulings.
- [ ] `gh pr create -R claussteinmassl/GenFill --base main --head feat/comfyui-workflows`; merged with `gh pr merge --merge`; never against `iconnorthomas/GenFill`.
- [ ] Suite green on merged `main`.

**Verify:** `gh pr view <n> -R claussteinmassl/GenFill --json state` → `MERGED`

```json:metadata
{"files": [], "verifyCommand": "gh pr view -R claussteinmassl/GenFill --json state", "acceptanceCriteria": ["review findings handled", "PR on fork merged", "suite green on main"], "modelTier": "standard"}
```
