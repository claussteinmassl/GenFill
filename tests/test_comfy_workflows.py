"""Tests for ComfyUI workflow loading and patching."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
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
                                         "pixels": 262144, "multiple": 16, "min_side": 64})
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

    def test_resolution_tiers_scale_pixels_up_to_max_side(self):
        write(self.tmp, "fill", GRAPH, {"pixels": 1048576, "multiple": 16, "max_side": 2048})
        write(self.tmp, "big", GRAPH, {"pixels": 1048576, "multiple": 16, "max_side": 4096})
        lib = cw.load_dir(self.tmp)
        wf = lib["fill"]
        self.assertEqual(wf.resolutions(), ("1K", "2K"))
        self.assertEqual(lib["big"].resolutions(), ("1K", "2K", "4K"))
        self.assertEqual(wf.model_size(4000, 2250, "auto"), wf.model_size(4000, 2250))
        self.assertEqual(wf.model_size(4000, 2250, "2K"), (2048, 1152))   # capped at max_side
        self.assertEqual(lib["big"].model_size(8000, 8000, "4K"), (4096, 4096))
        self.assertEqual(wf.model_size(900, 500, "2K"), (896, 512))       # crop size
        self.assertEqual(wf.spec().extra["resolutions"], ("1K", "2K"))

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

    def test_mask_false_sidecar_needs_no_mask_node(self):
        g = dict(GRAPH)
        del g["2"]
        write(self.tmp, "edit", g, {"kind": "edit", "mask": False})
        write(self.tmp, "nomask", g, {"kind": "edit"})
        lib = cw.load_dir(self.tmp)
        wf = lib["edit"]
        self.assertEqual(wf.error, "")
        self.assertFalse(wf.needs_mask)
        self.assertTrue(lib["nomask"].needs_mask)
        self.assertIn("GenFill Mask", lib["nomask"].error)
        self.assertEqual(wf.mask_loader(), "")
        graph, outputs = wf.patch("genfill/a.png", None, "a crate", "", 3)
        self.assertEqual(graph["1"]["inputs"]["image"], "genfill/a.png")
        self.assertEqual(graph["3"]["inputs"]["text"], "a crate")
        self.assertEqual(outputs, ["5"])

    def test_family_goes_into_the_spec(self):
        write(self.tmp, "klein", GRAPH, {"family": "Black Forest Labs"})
        write(self.tmp, "plain", GRAPH)
        lib = cw.load_dir(self.tmp)
        self.assertEqual(lib["klein"].spec().extra["family"], "Black Forest Labs")
        self.assertNotIn("family", lib["plain"].spec().extra)

    def test_non_numeric_sidecar_values_are_listed_with_an_error(self):
        write(self.tmp, "slow", GRAPH, {"timeout": "abc"})
        write(self.tmp, "wide", GRAPH, {"pixels": "x", "label": "Wide"})
        write(self.tmp, "good", GRAPH, {"timeout": 60})
        lib = cw.load_dir(self.tmp)
        self.assertIn("timeout", lib["slow"].error)
        self.assertIn("pixels", lib["wide"].error)
        self.assertEqual(lib["slow"].graph, {})
        self.assertEqual(lib["good"].error, "")
        self.assertEqual(lib["good"].timeout, 60.0)
        self.assertIn("slow", cw.library([self.tmp]))

    def test_infinite_and_nan_sidecar_values_are_listed_with_an_error(self):
        # Raw JSON text: 1e400 parses to infinity; NaN and Infinity are non-finite literals.
        for wid, text in (("huge", '{"pixels": 1e400}'), ("nan", '{"timeout": NaN}'),
                          ("inf", '{"timeout": Infinity}')):
            with open(os.path.join(self.tmp, wid + ".json"), "w") as fh:
                json.dump(GRAPH, fh)
            with open(os.path.join(self.tmp, wid + ".genfill.json"), "w") as fh:
                fh.write(text)
        lib = cw.load_dir(self.tmp)
        self.assertIn("pixels", lib["huge"].error)
        self.assertIn("timeout", lib["nan"].error)
        self.assertIn("timeout", lib["inf"].error)
        self.assertIn("huge", cw.library([self.tmp]))

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


REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHIPPED_DIRS = [os.path.join(REPO, "nuke", "workflows", "comfyui"),
                os.path.join(REPO, "tests", "fixtures", "comfyui")]


class ShippedTests(unittest.TestCase):
    def libraries(self):
        for folder in SHIPPED_DIRS:
            lib = cw.load_dir(folder)
            self.assertTrue(lib, folder)
            yield folder, lib

    def test_every_shipped_workflow_loads(self):
        for _, lib in self.libraries():
            for wid, wf in lib.items():
                self.assertEqual(wf.error, "", wid)

    def test_empty_prompt_links_exist(self):
        for _, lib in self.libraries():
            for wid, wf in lib.items():
                if wf.empty_prompt:
                    self.assertIn(wf.empty_prompt, lib, wid)
                    self.assertEqual(lib[wf.empty_prompt].kind, "erase", wid)

    def test_api_sidecars_flag_api_nodes(self):
        for _, lib in self.libraries():
            for wid, wf in lib.items():
                uses_api = wid.startswith("api_")
                self.assertEqual(wf.api_nodes, uses_api, wid)

    def test_one_image_and_one_mask_node(self):
        for _, lib in self.libraries():
            for wid, wf in lib.items():
                self.assertEqual(len(wf.nodes_titled(cw.T_IMAGE)), 1, wid)
                self.assertEqual(len(wf.nodes_titled(cw.T_MASK)), 1 if wf.needs_mask else 0,
                                 wid)

    def test_erase_workflows_have_no_prompt_node(self):
        for _, lib in self.libraries():
            for wid, wf in lib.items():
                if wf.kind == "erase":
                    self.assertEqual(wf.nodes_titled(cw.T_PROMPT), [], wid)


class Flux3KleinTests(unittest.TestCase):
    """The shipped FLUX 3 Klein / FLUX 3 Image workflows (spec section 4)."""

    IDS = ("flux3_klein", "flux3_klein_upsampler", "api_flux3_image")

    def setUp(self):
        self.lib = cw.load_dir(SHIPPED_DIRS[0])

    def node(self, wf, class_type):
        ids = [nid for nid, n in wf.graph.items() if n["class_type"] == class_type]
        self.assertEqual(len(ids), 1, class_type)
        return wf.graph[ids[0]]

    def test_sidecars(self):
        for wid in self.IDS:
            wf = self.lib[wid]
            self.assertEqual(wf.error, "", wid)
            self.assertEqual((wf.kind, wf.needs_mask, wf.meta.get("layout")),
                             ("edit", False, "flux3"), wid)
            self.assertEqual(wf.spec().extra["family"], "Black Forest Labs", wid)
            self.assertIsNone(wf.empty_prompt, wid)
            self.assertEqual(wf.api_nodes, wid.startswith("api_"), wid)
            w, h = wf.model_size(3000, 2000)
            self.assertEqual((w % 16, h % 16), (0, 0), wid)
            self.assertLessEqual(w * h, 1048576 * 1.05, wid)
        self.assertEqual(self.lib["flux3_klein"].resolutions(), ("1K", "2K", "4K"))
        self.assertEqual(self.lib["api_flux3_image"].resolutions(), ("1K",))

    def test_klein_graph(self):
        for wid in ("flux3_klein", "flux3_klein_upsampler"):
            wf = self.lib[wid]
            image = wf.nodes_titled(cw.T_IMAGE)[0]
            self.assertEqual(self.node(wf, "UNETLoader")["inputs"]["unet_name"],
                             "flux3-image-klein-int8-convrot.safetensors")
            ref = self.node(wf, "ReferenceMultiLatent")["inputs"]
            self.assertEqual(ref["resolution"], "keep resolution")
            self.assertEqual(ref["images.image0"], [image, 0])
            seed = self.graph_node(wf, cw.T_SEED)
            self.assertEqual(seed["class_type"], "KSampler")
            self.assertEqual({k: seed["inputs"][k] for k in ("steps", "cfg", "sampler_name",
                                                             "scheduler", "denoise")},
                             {"steps": 16, "cfg": 1.0, "sampler_name": "euler",
                              "scheduler": "simple", "denoise": 1.0})
            self.assertEqual(self.graph_node(wf, cw.T_OUTPUT)["class_type"], "SaveImage")
            graph, outputs = wf.patch("genfill/a.png", None, "a red door", "", 77)
            self.assertEqual(graph[image]["inputs"]["image"], "genfill/a.png")
            self.assertEqual(graph[wf.nodes_titled(cw.T_SEED)[0]]["inputs"]["seed"], 77)
            self.assertEqual(outputs, wf.nodes_titled(cw.T_OUTPUT))
        plain = self.lib["flux3_klein"]
        self.assertEqual(self.graph_node(plain, cw.T_PROMPT)["class_type"], "CLIPTextEncode")
        graph, _ = plain.patch("a", None, "a red door", "", 1)
        self.assertEqual(graph[plain.nodes_titled(cw.T_PROMPT)[0]]["inputs"]["text"],
                         "a red door")

    def test_upsampler_graph(self):
        wf = self.lib["flux3_klein_upsampler"]
        prompt_id = wf.nodes_titled(cw.T_PROMPT)[0]
        self.assertEqual(wf.graph[prompt_id]["class_type"], "PrimitiveStringMultiline")
        gen = self.node(wf, "TextGenerate")["inputs"]
        self.assertEqual(gen["prompt"], [prompt_id, 0])
        self.assertEqual(gen["image"], [wf.nodes_titled(cw.T_IMAGE)[0], 0])
        self.assertEqual(gen["sampling_mode"], "on")
        system = wf.graph[gen["system_prompt"][0]]
        self.assertEqual(system["class_type"], "PrimitiveStringMultiline")
        self.assertTrue(system["inputs"]["value"].startswith(
            "You are the prompt upsampler of an image-editing model."))
        encode = self.node(wf, "CLIPTextEncode")["inputs"]
        gen_id = next(n for n, v in wf.graph.items() if v["class_type"] == "TextGenerate")
        self.assertEqual(encode["text"], [gen_id, 0])
        loaders = sorted(n["inputs"]["clip_name"] for n in wf.graph.values()
                         if n["class_type"] == "CLIPLoader")
        self.assertEqual(loaders, ["qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors",
                                   "qwen3vl_4b_int8_convrot.safetensors"])
        graph, _ = wf.patch("a", None, "a red door", "", 1)
        self.assertEqual(graph[prompt_id]["inputs"]["value"], "a red door")
        self.assertEqual(graph[gen["system_prompt"][0]]["inputs"]["value"],
                         system["inputs"]["value"])      # not patched

    def test_api_flux3_graph(self):
        wf = self.lib["api_flux3_image"]
        node = self.graph_node(wf, cw.T_PROMPT)
        self.assertEqual(node["class_type"], "Flux3ImageNode")
        self.assertEqual(node["inputs"]["images.image_1"], [wf.nodes_titled(cw.T_IMAGE)[0], 0])
        self.assertEqual(node["inputs"]["resolution"], "1K")
        self.assertNotIn("bounding_boxes", node["inputs"])
        graph, _ = wf.patch("a", None, "a red door", "", 5)
        patched = graph[wf.nodes_titled(cw.T_PROMPT)[0]]["inputs"]
        self.assertEqual((patched["prompt"], patched["seed"]), ("a red door", 5))

    def graph_node(self, wf, title):
        ids = wf.nodes_titled(title)
        self.assertEqual(len(ids), 1, title)
        return wf.graph[ids[0]]


if __name__ == "__main__":
    unittest.main()
