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
                self.assertEqual(len(wf.nodes_titled(cw.T_MASK)), 1, wid)

    def test_erase_workflows_have_no_prompt_node(self):
        for _, lib in self.libraries():
            for wid, wf in lib.items():
                if wf.kind == "erase":
                    self.assertEqual(wf.nodes_titled(cw.T_PROMPT), [], wid)


if __name__ == "__main__":
    unittest.main()
