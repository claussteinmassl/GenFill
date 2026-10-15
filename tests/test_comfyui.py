"""ComfyUI backend tests against a fake ComfyUI server (no real server is touched)."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))
sys.path.insert(0, os.path.dirname(__file__))

from genfill import config, pngio  # noqa: E402
from genfill.backends import base, comfy_workflows, comfyui  # noqa: E402
from fake_servers import FakeComfyServer  # noqa: E402

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


def _write(folder, wid, graph, sidecar=None):
    with open(os.path.join(folder, wid + ".json"), "w") as fh:
        json.dump(graph, fh)
    if sidecar is not None:
        with open(os.path.join(folder, wid + ".genfill.json"), "w") as fh:
            json.dump(sidecar, fh)


def _png(path, channels=3):
    img = pngio.Image(8, 6, channels)
    for row in img.rows:
        row[:] = bytes(bytearray([128] * (8 * channels)))
    pngio.write(path, img)


class ComfyUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wf_dir = os.path.join(self.tmp, "wf")
        os.mkdir(self.wf_dir)
        _write(self.wf_dir, "fill", GRAPH, {"label": "Fill", "empty_prompt": "erase"})
        erase = json.loads(json.dumps(GRAPH))
        erase["4"]["inputs"]["steps"] = 7
        _write(self.wf_dir, "erase", erase, {"label": "Erase", "kind": "erase"})
        self.img = os.path.join(self.tmp, "img.png")
        self.mask = os.path.join(self.tmp, "mask.png")
        self.mask_a = os.path.join(self.tmp, "mask_a.png")
        _png(self.img)
        _png(self.mask)
        _png(self.mask_a, 4)
        self.fake = None
        self.env = {}
        self.backend = comfyui.ComfyUIBackend()
        for patcher in (mock.patch.object(comfyui, "POLL_INTERVAL", 0.02),
                        mock.patch.object(config, "load", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        if self.fake:
            self.fake.close()
        shutil.rmtree(self.tmp)

    def serve(self, **kw):
        self.fake = FakeComfyServer(**kw)
        self.env.update({"GENFILL_COMFY_URL": self.fake.base,
                         "GENFILL_COMFY_WORKFLOWS": self.wf_dir})
        return self.fake

    def run_generate(self, request, api_key="", **kw):
        with mock.patch.dict(os.environ, self.env):
            return self.backend.generate(request, api_key, **kw)

    def request(self, prompt="a red car", count=2, model="fill", seed=100):
        return base.GenerationRequest(self.img, self.mask, self.mask_a, prompt, 8, 6,
                                      seed=seed, count=count, model=model)

    def test_generate_end_to_end(self):
        fake = self.serve()
        messages = []
        out = self.run_generate(self.request(), progress=lambda f, m: messages.append(m))
        self.assertEqual(len(out), 2)
        self.assertTrue(all(base.sniff_image(o) == ".png" for o in out))
        self.assertEqual(len(fake.uploads), 2)
        fields, name = fake.uploads[0]
        self.assertIn("subfolder", fields)
        self.assertTrue(name.endswith("_source.png"))
        self.assertTrue(fake.uploads[1][1].endswith("_mask.png"))
        self.assertEqual(len(fake.prompts), 2)
        seeds = [p["prompt"]["4"]["inputs"]["seed"] for p in fake.prompts]
        self.assertEqual(seeds, [100, 101])
        g = fake.prompts[0]["prompt"]
        self.assertEqual(g["3"]["inputs"]["text"], "a red car")
        self.assertEqual(g["1"]["inputs"]["image"], "genfill/" + name)
        self.assertEqual(len(fake.find("GET", "/view")), 2)
        self.assertTrue(messages)

    def test_random_seed_stays_below_2_31(self):
        fake = self.serve()
        self.run_generate(self.request(seed=0, count=3))
        seeds = [p["prompt"]["4"]["inputs"]["seed"] for p in fake.prompts]
        self.assertEqual(seeds, [seeds[0], seeds[0] + 1, seeds[0] + 2])
        self.assertLess(max(seeds), 2 ** 31)

    def test_alpha_mask_for_load_image_loader(self):
        graph = json.loads(json.dumps(GRAPH))
        graph["2"]["class_type"] = "LoadImage"
        _write(self.wf_dir, "alpha", graph)
        fake = self.serve()
        self.run_generate(self.request(model="alpha", count=1))
        self.assertTrue(fake.uploads[1][1].endswith("_mask_alpha.png"))

    def test_token_header_and_api_key(self):
        fake = self.serve()
        self.run_generate(self.request(count=1), api_key="t")
        self.assertTrue(fake.requests)
        for _, _, headers, _ in fake.requests:
            self.assertEqual(headers.get("Authorization"), "Bearer t")
        self.assertNotIn("extra_data", fake.prompts[0])
        _write(self.wf_dir, "cloud", GRAPH, {"api_nodes": True})
        with self.assertRaises(base.BackendError) as ctx:
            with mock.patch.dict(os.environ, {"COMFY_API_KEY": ""}):
                self.run_generate(self.request(model="cloud", count=1))
        self.assertIn("COMFY_API_KEY", str(ctx.exception))
        self.env["COMFY_API_KEY"] = "k"
        self.run_generate(self.request(model="cloud", count=1))
        self.assertEqual(fake.prompts[-1]["extra_data"]["api_key_comfy_org"], "k")

    def test_missing_model_message(self):
        self.serve(fail_prompt={
            "error": {"type": "prompt_outputs_failed_validation",
                      "message": "Prompt outputs failed validation"},
            "node_errors": {"4": {"class_type": "CheckpointLoaderSimple", "errors": [{
                "type": "value_not_in_list", "message": "Value not in list",
                "details": "ckpt_name: 'sd.safetensors' not in []",
                "extra_info": {"input_name": "ckpt_name"}}]}}})
        with self.assertRaises(base.BackendError) as ctx:
            self.run_generate(self.request())
        self.assertIn("sd.safetensors", str(ctx.exception))
        self.assertIn("models/checkpoints", str(ctx.exception))

    def test_other_validation_error_is_readable(self):
        self.serve(fail_prompt={"error": {"type": "x", "message": "Bad graph",
                                          "details": "no outputs"}, "node_errors": {}})
        with self.assertRaises(base.BackendError) as ctx:
            self.run_generate(self.request())
        self.assertIn("Bad graph", str(ctx.exception))

    def test_execution_error_and_partial_results(self):
        self.serve(exec_error={1})
        out = self.run_generate(self.request())
        self.assertEqual(len(out), 1)
        self.fake.close()
        self.serve(exec_error=True)
        with self.assertRaises(base.BackendError) as ctx:
            self.run_generate(self.request())
        self.assertIn("out of memory", str(ctx.exception))

    def test_history_failure_keeps_finished_images(self):
        fake = self.serve(history_500={2})
        out = self.run_generate(self.request())
        self.assertEqual(len(out), 1)
        self.assertGreater(len(fake.find("GET", "/history/p2")), comfyui.HISTORY_RETRIES)
        self.assertEqual(json.loads(fake.find("POST", "/queue")[0][3].decode()), {"delete": ["p2"]})

    def test_history_failure_without_results_raises(self):
        self.serve(history_500={1, 2})
        with self.assertRaises(base.BackendError) as ctx:
            self.run_generate(self.request())
        self.assertIn("500", str(ctx.exception))
        self.assertTrue(self.fake.find("POST", "/queue"))

    def test_cancel_deletes_and_interrupts(self):
        fake = self.serve(hold=True)
        cancelled = lambda: bool(fake.find("GET", "/history/p1"))
        with self.assertRaises(base.Cancelled):
            self.run_generate(self.request(), cancelled=cancelled)
        deletes = fake.find("POST", "/queue")
        self.assertEqual(json.loads(deletes[0][3].decode()), {"delete": ["p1", "p2"]})
        interrupts = fake.find("POST", "/interrupt")
        self.assertEqual(json.loads(interrupts[0][3].decode()), {"prompt_id": "p1"})

    def test_timeout_cleans_up(self):
        fake = self.serve(hold=True)
        _write(self.wf_dir, "slow", GRAPH, {"timeout": 0.1})
        with self.assertRaises(base.BackendError) as ctx:
            self.run_generate(self.request(model="slow", count=1))
        self.assertIn("Timed out", str(ctx.exception))
        self.assertTrue(fake.find("POST", "/queue"))
        self.assertTrue(fake.find("POST", "/interrupt"))

    def test_empty_prompt_uses_erase_workflow(self):
        fake = self.serve()
        self.run_generate(self.request(prompt="  ", count=1))
        self.assertEqual(len(fake.prompts), 1)
        self.assertEqual(fake.prompts[0]["prompt"]["4"]["inputs"]["steps"], 7)   # erase graph

    def test_unknown_workflow_lists_available(self):
        self.serve()
        with self.assertRaises(base.BackendError) as ctx:
            self.run_generate(self.request(model="nope"))
        msg = str(ctx.exception)
        self.assertIn("erase", msg)
        self.assertIn("fill", msg)
        self.assertIn(self.wf_dir, msg)

    def test_invalid_workflow_reports_its_error(self):
        _write(self.wf_dir, "ui", {"nodes": [], "links": []})
        self.serve()
        with self.assertRaises(base.BackendError) as ctx:
            self.run_generate(self.request(model="ui"))
        self.assertIn("Export (API)", str(ctx.exception))

    def test_catalogue_and_model_size(self):
        self.serve()
        with mock.patch.dict(os.environ, self.env):
            ids = [m.id for m in self.backend.catalogue]
            self.assertLess(ids.index("erase"), ids.index("fill"))   # sorted by label
            w, h = self.backend.model_size(1000, 500, "fill")
            self.assertEqual((w % 64, h % 64), (0, 0))
            self.assertTrue(self.backend.model_size(1000, 500, "missing"))

    def test_server_info(self):
        fake = self.serve()
        info = comfyui.server_info(fake.base, "tok")
        self.assertEqual(info["version"], "0.3.99")
        self.assertEqual(info["devices"], ["cuda:0 Fake GPU"])
        self.assertEqual(fake.requests[-1][2].get("Authorization"), "Bearer tok")

    def test_server_info_unreachable(self):
        with self.assertRaises(base.BackendError) as ctx:
            comfyui.server_info("http://127.0.0.1:1", "")
        self.assertIn("127.0.0.1:1", str(ctx.exception))


class ConfigTests(unittest.TestCase):
    def test_config_precedence(self):
        env = {"GENFILL_COMFY_URL": "", "GENFILL_COMFY_TOKEN": "", "GENFILL_COMFY_WORKFLOWS": ""}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(config, "load", return_value={}):
                self.assertEqual(config.comfyui_url(), "http://127.0.0.1:8188")
                self.assertEqual(config.comfyui_token(), "")
                self.assertEqual(config.comfyui_workflow_dirs(), [comfy_workflows.REPO_DIR])
            cfg = {"comfyui": {"url": "http://lan:8188/", "workflow_dirs": ["/a", "/b"]},
                   "api_keys": {"comfyui": "stored"}}
            with mock.patch.object(config, "load", return_value=cfg):
                self.assertEqual(config.comfyui_url(), "http://lan:8188")
                self.assertEqual(config.comfyui_token(), "stored")
                self.assertEqual(config.comfyui_workflow_dirs(),
                                 [comfy_workflows.REPO_DIR, "/a", "/b"])
                env = {"GENFILL_COMFY_URL": "https://pod.example/", "GENFILL_COMFY_TOKEN": "env",
                       "GENFILL_COMFY_WORKFLOWS": os.pathsep.join(["/c", "", "/d"])}
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(config.comfyui_url(), "https://pod.example")
                    self.assertEqual(config.comfyui_token(), "env")
                    self.assertEqual(config.comfyui_workflow_dirs(),
                                     [comfy_workflows.REPO_DIR, "/a", "/b", "/c", "/d"])

    def test_set_comfyui(self):
        store = {}

        def update(fn):
            fn(store)
        with mock.patch.object(config, "update", update):
            config.set_comfyui(url="http://x:1/", workflow_dirs=["/w", " "])
            self.assertEqual(store["comfyui"], {"url": "http://x:1", "workflow_dirs": ["/w"]})
            config.set_comfyui(url="")
            self.assertEqual(store["comfyui"], {"workflow_dirs": ["/w"]})
            config.set_comfyui(url=" 192.168.1.20:8188/ ")
            self.assertEqual(store["comfyui"]["url"], "http://192.168.1.20:8188")

    def test_url_without_scheme_gets_http(self):
        self.assertEqual(config.normalize_comfyui_url("192.168.1.20:8188"),
                         "http://192.168.1.20:8188")
        self.assertEqual(config.normalize_comfyui_url("https://pod.example/"), "https://pod.example")
        self.assertEqual(config.normalize_comfyui_url("  "), "")
        env = {"GENFILL_COMFY_URL": "lan:8188"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.comfyui_url(), "http://lan:8188")
        with mock.patch.dict(os.environ, {"GENFILL_COMFY_URL": ""}):
            with mock.patch.object(config, "load", return_value={"comfyui": {"url": "box:8188"}}):
                self.assertEqual(config.comfyui_url(), "http://box:8188")


if __name__ == "__main__":
    unittest.main()
