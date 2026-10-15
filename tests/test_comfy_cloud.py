"""Comfy Cloud backend: upload parsing, graph building, routes, wait/output, failure, cancel."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))
sys.path.insert(0, os.path.dirname(__file__))

from genfill.backends import base, cloud, comfy_cloud as cc  # noqa: E402
from genfill.backends.base import BackendError, Cancelled, GenerationRequest  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
URL = "https://cloud.comfy.org/api/uploads/aFMt-H5pWwP_pVf80gk95g"
PROSE = ("This client cannot run shell commands...\n\ncurl -sS -X PUT -H 'Content-Type: "
         "image/webp' --upload-file '/x/a.png' -- '%s'\n\nOn success it prints JSON." % URL)


class FakeClient(object):
    def __init__(self, tools):
        self.tools = tools
        self.calls = []

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, args))
        res = self.tools[name]
        return res(args) if callable(res) else res


class Env(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.img, self.msk = os.path.join(d, "i.png"), os.path.join(d, "m.png")
        for p in (self.img, self.msk):
            with open(p, "wb") as fh:
                fh.write(PNG)
        self.uploads = []
        self.n = 0
        self.b = cc.ComfyCloudBackend()
        self.job = {"result": {"status": "succeeded", "job_status": "completed"}}
        self.tools = {
            "upload_file": lambda a: PROSE,
            "submit_workflow": {"result": {"prompt_id": "p1", "status": "succeeded"}},
            "wait_for_job": lambda a: self.job,
            "get_output": lambda a: {"results": [
                {"url": "https://x/api/s/%d" % i} for i in range(self.nres)]},
            "partner_generate": lambda a: {"results": [{"url": "https://x/api/s/p"}],
                                           "prompt_id": "q", "status": "completed"},
            "cancel_job": {},
        }
        self.nres = 3
        self.client = FakeClient(self.tools)

        def put(url, data, ctype, **kw):
            self.n += 1
            self.uploads.append((url, ctype))
            return json.dumps({"name": "n%d.png" % self.n, "subfolder": ""}).encode()

        for p in (mock.patch.object(cc.McpBackend, "client", lambda s, *a, **k: self.client),
                  mock.patch.object(cc, "put_bytes", put),
                  mock.patch.object(cc, "download", lambda u: PNG),
                  mock.patch.object(cloud.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def req(self, **kw):
        a = dict(image_path=self.img, mask_path=self.msk, mask_alpha_path=self.msk,
                 prompt="grass", width=512, height=512, seed=10, count=3,
                 model="flux-pro-fill")
        a.update(kw)
        return GenerationRequest(**a)

    def called(self, name):
        return [a for n, a in self.client.calls if n == name]


class TestPieces(unittest.TestCase):
    def test_parse_upload_prose(self):
        self.assertEqual(cc.parse_upload_url(PROSE), (URL, "image/webp"))

    def test_parse_upload_structured(self):
        url, ct = cc.parse_upload_url({"upload_url": URL})
        self.assertEqual((url, ct), (URL, "image/png"))
        with self.assertRaises(BackendError):
            cc.parse_upload_url("nothing here")

    def test_graph(self):
        g = cc.build_fill_graph("a.png", "m.png", "sky", [5, 6], cc.NODES["flux-pro-fill"])
        self.assertEqual(g["2"]["inputs"], {"image": "m.png", "channel": "red"})
        self.assertEqual(g["3"]["inputs"]["seed"], 5)
        self.assertEqual(g["5"]["inputs"]["seed"], 6)
        self.assertEqual(g["5"]["inputs"]["image"], ["1", 0])
        self.assertEqual(g["6"]["inputs"]["images"], ["5", 0])
        self.assertEqual(sum(v["class_type"] == "SaveImage" for v in g.values()), 2)
        self.assertTrue(all(v["_meta"]["title"] for v in g.values()))

    def test_client_os(self):
        for plat, want in (("darwin", "darwin"), ("win32", "windows"), ("linux", "linux")):
            with mock.patch.object(cc.sys, "platform", plat):
                self.assertEqual(cc.client_os(), want)

    def test_attrs(self):
        b = cc.ComfyCloudBackend()
        self.assertEqual((b.id, b.api_key_header, b.env_var, b.auth, b.needs_key),
                         ("comfy", "X-API-Key", "COMFY_API_KEY", "key_or_oauth", False))
        self.assertEqual(b.server_url, "https://cloud.comfy.org/mcp")
        self.assertEqual(b.model_size(1000, 700, "flux-pro-fill")[0] % 32, 0)


class TestGenerate(Env):
    def test_fill_route(self):
        out = self.b.generate(self.req(), "k")
        self.assertEqual(len(out), 3)
        self.assertEqual(len(self.called("upload_file")), 2)
        self.assertEqual(self.uploads[0], (URL, "image/webp"))
        sub = self.called("submit_workflow")[0]
        self.assertTrue(sub["confirm"])
        g = sub["workflow"]
        self.assertEqual(g["1"]["inputs"]["image"], "n1.png")
        self.assertEqual(g["2"]["inputs"]["image"], "n2.png")
        fills = [v for v in g.values() if v["class_type"] == "FluxProFillNode"]
        self.assertEqual([f["inputs"]["seed"] for f in fills], [10, 11, 12])
        self.assertEqual(self.called("wait_for_job")[0]["prompt_id"], "p1")

    def test_random_seed(self):
        self.b.generate(self.req(seed=0), "k")
        g = self.called("submit_workflow")[0]["workflow"]
        self.assertGreater(g["3"]["inputs"]["seed"], 0)

    def test_erase_route(self):
        self.b.generate(self.req(prompt="  "), "k")
        g = self.called("submit_workflow")[0]["workflow"]
        self.assertEqual({v["class_type"] for v in g.values()} - {"LoadImage", "LoadImageMask",
                                                                  "SaveImage"},
                         {"FluxEraseNode"})
        self.assertNotIn("prompt", g["3"]["inputs"])

    def test_deterministic_eraser_single(self):
        self.nres = 1
        out = self.b.generate(self.req(prompt="", model="bria-genfill"), "k")
        self.assertEqual(len(out), 1)
        g = self.called("submit_workflow")[0]["workflow"]
        self.assertEqual(g["3"]["class_type"], "BriaEraser")

    def test_partner_route(self):
        out = self.b.generate(self.req(model="nano-banana-2", count=2), "k")
        self.assertEqual(len(out), 2)
        self.assertEqual(len(self.called("upload_file")), 1)
        calls = self.called("partner_generate")
        self.assertEqual(len(calls), 2)
        a = calls[0]
        self.assertEqual(a["model"], "vertexai/nano-banana-2")
        self.assertEqual(a["medias"][0]["role"], "image")
        self.assertEqual(a["medias"][0]["name"], "n1.png")
        self.assertTrue(a["confirm"])
        self.assertTrue(a["prompt"].startswith("grass"))
        self.assertNotEqual(calls[0]["seed"], calls[1]["seed"])
        self.assertEqual(self.called("submit_workflow"), [])

    def test_partner_in_progress_falls_back_to_wait(self):
        self.tools["partner_generate"] = {"prompt_id": "q", "status": "in_progress",
                                          "timed_out": True}
        self.nres = 1
        out = self.b.generate(self.req(model="flux-kontext-pro", count=1), "k")
        self.assertEqual(len(out), 1)
        self.assertEqual(self.called("wait_for_job")[0]["prompt_id"], "q")
        self.assertIn("aspect_ratio", self.called("partner_generate")[0]["params"])

    def test_wait_loops_until_done(self):
        states = [{"result": {"timed_out": True}},
                  {"result": {"job_status": "completed"}}]
        self.tools["wait_for_job"] = lambda a: states.pop(0)
        self.assertEqual(len(self.b.generate(self.req(), "k")), 3)
        self.assertEqual(states, [])

    def test_job_failure(self):
        self.job = {"result": {"job_status": "error", "message": "node blew up"}}
        with self.assertRaises(BackendError) as cm:
            self.b.generate(self.req(), "k")
        self.assertIn("node blew up", str(cm.exception))

    def test_tool_error_on_submit(self):
        self.tools["submit_workflow"] = {"result": {"status": "tool_error",
                                                    "message": "no credits"}}
        with self.assertRaises(BackendError) as cm:
            self.b.generate(self.req(), "k")
        self.assertIn("no credits", str(cm.exception))

    def test_no_outputs(self):
        self.nres = 0
        with self.assertRaises(BackendError):
            self.b.generate(self.req(), "k")

    def test_cancel_during_wait(self):
        flag = {"v": False}

        def wait(a):
            flag["v"] = True
            return {"result": {"timed_out": True}}

        self.tools["wait_for_job"] = wait
        with self.assertRaises(Cancelled):
            self.b.generate(self.req(), "k", cancelled=lambda: flag["v"])
        self.assertEqual(len(self.called("cancel_job")), 1)
        self.assertEqual(self.called("get_output"), [])

    def test_cancel_before_start(self):
        with self.assertRaises(Cancelled):
            self.b.generate(self.req(), "k", cancelled=lambda: True)
        self.assertEqual(self.called("submit_workflow"), [])


if __name__ == "__main__":
    unittest.main()
