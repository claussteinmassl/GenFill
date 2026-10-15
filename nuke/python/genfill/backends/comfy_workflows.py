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


NUMERIC_META = {"timeout": float, "pixels": int, "multiple": int, "min_side": int, "max_side": int}


def _numeric_error(meta):
    """Error message for a sidecar whose numeric settings are not numbers, or ``""``."""
    for key, cast in NUMERIC_META.items():
        if key in meta:
            try:
                cast(meta[key])
            except (TypeError, ValueError):
                return "sidecar %s is not a number: %r" % (key, meta[key])
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
        if not isinstance(meta, dict):
            meta = {}
        error = _numeric_error(meta)
        if error:
            meta = {}
        with open(path, "r") as fh:
            graph = json.load(fh)
        error = error or validate(graph)
    except (IOError, OSError, ValueError) as exc:
        error = "cannot read: %s" % exc
    return Workflow(wid, path, graph if not error else {}, meta, error)


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
