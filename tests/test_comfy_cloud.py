"""Comfy Cloud backend: upload parsing, graph building, routes, wait/output, failure, cancel."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))
sys.path.insert(0, os.path.dirname(__file__))

from genfill.backends import base, cloud, comfy_cloud as cc, mcp  # noqa: E402
from fake_servers import FakeMcpServer, text_result  # noqa: E402
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
                  mock.patch.object(cloud.time, "sleep", lambda s: None),
                  mock.patch("genfill.log.warning")):
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

    def test_bria_genfill_required_inputs(self):
        self.b.generate(self.req(prompt="a car", model="bria-genfill"), "k")
        g = self.called("submit_workflow")[0]["workflow"]
        node = [v for v in g.values() if v["class_type"] == "BriaGenFill"][0]
        self.assertEqual(node["inputs"]["negative_prompt"], "")
        self.assertIs(node["inputs"]["refine_prompt"], True)

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

    def test_partner_mask_edit_gpt_image_2_5(self):
        for model in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
            self.client.calls[:] = []
            self.uploads[:] = []
            out = self.b.generate(self.req(model=model, count=2), "k")
            self.assertEqual(len(out), 2)
            self.assertEqual(len(self.called("upload_file")), 2)     # plate + mask
            calls = self.called("partner_generate")
            self.assertEqual(len(calls), 2)
            a = calls[0]
            self.assertEqual(a["model"], "openai/images-generations")
            self.assertEqual(a["params"], {"model": model})
            self.assertEqual([m["role"] for m in a["medias"]], ["image", "mask"])
            self.assertEqual(a["medias"][1]["type"], "input")
            self.assertEqual(a["prompt"], "grass")       # mask edit: no reference wording
            self.assertEqual(self.called("submit_workflow"), [])
        self.assertEqual(self.b.model_spec("gpt-image-2.5-flare").kind, "inpaint")

    def test_partner_mask_edit_ideogram_empty_prompt(self):
        self.b.generate(self.req(model="ideogram-v3-edit", count=1, prompt=" "), "k")
        a = self.called("partner_generate")[0]
        self.assertEqual(a["model"], "ideogram/generate")
        self.assertNotIn("params", a)
        self.assertEqual([m["name"] for m in a["medias"]], ["n1.png", "n2.png"])
        self.assertEqual(a["medias"][1]["role"], "mask")
        self.assertEqual(a["prompt"], cc.EDIT_EMPTY_PROMPT)

    def test_new_partner_edit_models(self):
        self.b.generate(self.req(model="grok-image-edit", count=1), "k")
        a = self.called("partner_generate")[0]
        self.assertEqual(a["model"], "xai/grok-image-edit")
        self.assertEqual(a["params"], {"model": "grok-imagine-image-2.0"})
        self.assertEqual([m["role"] for m in a["medias"]], ["image"])
        self.client.calls[:] = []
        self.b.generate(self.req(model="flux-2-max", count=1), "k")
        a = self.called("partner_generate")[0]
        self.assertEqual(a["model"], "bfl/flux-2-max")
        self.assertNotIn("params", a)
        self.assertEqual(len(self.called("upload_file")), 1)

    def test_resolution_tiers(self):
        b = self.b
        self.assertEqual(b.size_plan(5000, 2800, "flux-pro-fill", "4K").note, "model max 2K")
        w, h = b.model_size(5000, 2800, "flux-pro-fill", "2K")
        self.assertEqual((w % 32, h % 32), (0, 0))
        self.assertAlmostEqual(w * h / 4194304.0, 1.0, delta=0.03)
        self.assertEqual(b.size_plan(5000, 2800, "flux-kontext-pro", "2K").note, "fixed size")
        self.assertEqual(b.size_plan(5000, 2800, "flux-2-pro", "2K").note, "model max 1K")
        for model, res, want in (("nano-banana-2", "4K", {"resolution": "4K"}),
                                 ("nano-banana-pro", "2K", {"resolution": "2K"}),
                                 ("nano-banana-pro", "auto", None),
                                 ("grok-image-edit", "4K", {"resolution": "2k",
                                                            "model": "grok-imagine-image-2.0"}),
                                 ("qwen-image-3", "2K", None)):       # output matches input
            self.client.calls[:] = []
            w, h = b.model_size(5000, 2800, model, res)
            b.generate(self.req(model=model, count=1, width=w, height=h, resolution=res), "k")
            a = self.called("partner_generate")[0]
            self.assertEqual(a.get("params"), want, (model, res))
        self.client.calls[:] = []
        w, h = b.model_size(8000, 4500, "gpt-image-2.5-flare", "4K")
        self.assertEqual((w, h), (3840, 2160))
        b.generate(self.req(model="gpt-image-2.5-flare", count=1, width=w, height=h,
                            resolution="4K"), "k")
        self.assertEqual(self.called("partner_generate")[0]["params"],
                         {"model": "gpt-image-2.5-flare", "size": "3840x2160"})

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

    def test_partner_downloads_per_variation(self):
        order = []
        self.tools["partner_generate"] = lambda a: (order.append("gen") or
                                                    {"results": [{"url": "https://x/s"}]})
        with mock.patch.object(cc, "download", lambda u: order.append("dl") or PNG):
            self.b.generate(self.req(model="nano-banana-2", count=2), "k")
        self.assertEqual(order, ["gen", "dl", "gen", "dl"])

    def test_wait_tool_error_stops(self):
        self.tools["wait_for_job"] = {"result": {"status": "tool_error", "message": "bad id"}}
        with self.assertRaises(BackendError) as cm:
            self.b.generate(self.req(), "k")
        self.assertIn("bad id", str(cm.exception))
        self.assertEqual(len(self.called("wait_for_job")), 1)

    def test_fast_nonterminal_wait_pauses(self):
        states = [{"result": {"job_status": "executing"}},
                  {"result": {"job_status": "completed"}}]
        self.tools["wait_for_job"] = lambda a: states.pop(0)
        sleeps = []
        with mock.patch.object(cc.time, "sleep", lambda s: sleeps.append(s)):
            self.b.generate(self.req(), "k")
        self.assertGreaterEqual(len(sleeps), 4)

    def test_upload_sends_neutral_path_and_darwin(self):
        with mock.patch.object(cc.sys, "platform", "win32"):
            self.b.generate(self.req(), "k")
        calls = self.called("upload_file")
        self.assertEqual(len(calls), 2)
        folder = os.path.dirname(self.img)
        for a in calls:
            self.assertEqual(a["client_os"], "darwin")
            self.assertTrue(a["file_path"].startswith("/genfill/"))
            self.assertNotIn(folder, a["file_path"])
        self.assertEqual([a["file_path"] for a in calls], ["/genfill/i.png", "/genfill/m.png"])
        self.assertEqual(self.b.upload(self.client, "C:\\Users\\artist\\job\\p.png", PNG)["name"],
                         "n3.png")
        self.assertEqual(self.called("upload_file")[-1]["file_path"], "/genfill/p.png")

    def test_one_failed_download_keeps_the_others(self):
        def dl(url):
            if url.endswith("/1"):
                raise BackendError("Could not download the result")
            return PNG
        with mock.patch.object(cc, "download", dl):
            self.assertEqual(len(self.b.generate(self.req(), "k")), 2)
        with mock.patch.object(cc, "download", lambda u: b"<html>"):
            with self.assertRaises(BackendError) as cm:
                self.b.generate(self.req(), "k")
        self.assertIn("isn't an image", str(cm.exception))

    def test_partner_failure_keeps_earlier_variations(self):
        answers = [{"results": [{"url": "https://x/s/1"}]},
                   {"result": {"status": "tool_error", "message": "no credits"}},
                   {"results": [{"url": "https://x/s/3"}]}]
        self.tools["partner_generate"] = lambda a: answers.pop(0)
        out = self.b.generate(self.req(model="nano-banana-2", count=3), "k")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(self.called("partner_generate")), 2)   # stops after the failure
        self.tools["partner_generate"] = {"result": {"status": "tool_error",
                                                     "message": "no credits"}}
        with self.assertRaises(BackendError) as cm:
            self.b.generate(self.req(model="nano-banana-2", count=2), "k")
        self.assertIn("no credits", str(cm.exception))

    def test_poll_timeout_cancels_job(self):
        self.tools["wait_for_job"] = {"result": {"timed_out": True}}
        with mock.patch.object(cc, "MAX_JOB_SECONDS", -1):
            with self.assertRaises(BackendError) as cm:
                self.b.generate(self.req(), "k")
        self.assertIn("Timed out", str(cm.exception))
        self.assertEqual(self.called("cancel_job"), [{"prompt_id": "p1"}])

    def test_failed_job_is_not_cancelled(self):
        self.job = {"result": {"job_status": "failed", "message": "node blew up"}}
        with self.assertRaises(BackendError):
            self.b.generate(self.req(), "k")
        self.assertEqual(self.called("cancel_job"), [])

    def test_uncancellable_copy_keeps_original_hook(self):
        client = mcp.McpClient("http://127.0.0.1:1/mcp", cancelled=lambda: True)
        other = cc._uncancellable(client)
        self.assertIsNone(other.ctx["cancelled"])
        self.assertIsNotNone(client.ctx["cancelled"])

    def test_kontext_model_size_matches_aspect(self):
        w, h = self.b.model_size(1000, 700, "flux-kontext-pro")
        self.assertEqual(cc._closest_aspect(w, h), "3:2")
        self.assertEqual(w % 16, 0)
        req = self.req(model="flux-kontext-pro", count=1, width=w, height=h)
        self.nres = 1
        self.b.generate(req, "k")
        self.assertEqual(self.called("partner_generate")[0]["params"]["aspect_ratio"], "3:2")


class TestRealClientCancel(unittest.TestCase):
    def test_cancel_reaches_server(self):
        d = tempfile.mkdtemp()
        img = os.path.join(d, "i.png")
        with open(img, "wb") as fh:
            fh.write(PNG)
        flag = {"v": False}

        def wait(a):
            flag["v"] = True
            return text_result({"result": {"timed_out": True}})

        srv = FakeMcpServer(api_key="k", tools={
            "upload_file": lambda a: text_result(PROSE),
            "submit_workflow": lambda a: text_result({"result": {"prompt_id": "p9"}}),
            "wait_for_job": wait,
            "cancel_job": lambda a: text_result({"result": {"status": "cancelled"}}),
        })
        self.addCleanup(srv.close)
        b = cc.ComfyCloudBackend()
        b.server_url = srv.url
        req = GenerationRequest(img, img, img, "x", 64, 64, seed=1, count=1,
                                model="flux-pro-fill")
        with mock.patch.object(cc, "put_bytes",
                               lambda *a, **k: b'{"name": "a.png", "subfolder": ""}'):
            with self.assertRaises(Cancelled):
                b.generate(req, "k", cancelled=lambda: flag["v"])
        self.assertIn(("cancel_job", {"prompt_id": "p9"}), srv.calls)


if __name__ == "__main__":
    unittest.main()
