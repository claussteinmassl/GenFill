"""Magnific MCP backend against a scripted fake MCP client (no network).

Run: python -m unittest tests.test_magnific -v
"""
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

from genfill.backends import base, layout, magnific  # noqa: E402
from genfill.backends.magnific import MagnificBackend  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeClient(object):
    """Records tool calls; ``tools`` maps a name to ``fn(args)`` (or a list of answers)."""

    def __init__(self, tools):
        self.tools = tools
        self.calls = []

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, args))
        fn = self.tools[name]
        if isinstance(fn, list):
            return fn.pop(0)
        return fn(args)

    def named(self, name):
        return [a for n, a in self.calls if n == name]


def upload_tools(extra=None):
    tools = {
        "creations_request_upload": lambda a: {"uploads": [
            {"proxyUploadUrl": "https://up/%d" % i, "path": "temp-files/%d.png" % i}
            for i in range(2)], "count": 2},
        "creations_finalize_upload": lambda a: {
            "results": [{"identifier": "PLATE", "status": "completed"},
                        {"identifier": "MASK", "status": "completed"}], "errors": []},
    }
    tools.update(extra or {})
    return tools


def done(ids):
    return {"results": [{"identifier": i, "status": "completed",
                         "results": {"url": "https://cdn/%s.png" % i}} for i in ids],
            "allTerminal": True}


class MagnificTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "i.png")
        self.msk = os.path.join(self.tmp, "m.png")
        for p, data in ((self.img, PNG + b"img"), (self.msk, PNG + b"msk")):
            with open(p, "wb") as fh:
                fh.write(data)
        self.puts = []
        self.backend = MagnificBackend()
        patches = [
            mock.patch.object(magnific, "put_bytes",
                              lambda url, data, ctype, **k: self.puts.append((url, data, ctype))),
            mock.patch.object(magnific, "download", lambda url, **k: PNG + url.encode()),
            mock.patch("genfill.log.warning"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def request(self, **kw):
        args = dict(prompt="a red door", width=1024, height=768, seed=0, count=2,
                    model="")
        args.update(kw)
        return base.GenerationRequest(self.img, self.msk, self.msk, **args)

    def run_with(self, tools, request, cancelled=None):
        client = FakeClient(tools)
        with mock.patch.object(MagnificBackend, "client", lambda *a, **k: client):
            out = self.backend.generate(request, "", cancelled=cancelled)
        return client, out

    # ---- catalogue ----
    def test_catalogue(self):
        b = self.backend
        self.assertEqual(b.server_url, "https://mcp.magnific.com")
        self.assertEqual(b.auth, "oauth")
        self.assertEqual(b.default_model, "retouch-auto")
        ids = b.model_ids()
        self.assertEqual(len(ids), 37)
        self.assertNotIn("retouch-reference", ids)
        self.assertNotIn("retouch-lora", ids)
        for mid in ("gpt-2-mini", "retouch-inpaint-lab-gpt-2-mini", "retouch-ideogram-4-5",
                    "seedream-5-lite", "seedream-5-flash", "flux-2-max"):
            self.assertIn(mid, ids)
        self.assertEqual(b.model_spec("flux-2").label, "Flux.2 Pro")
        self.assertEqual(b.model_spec("gpt-2-mini").label, "GPT 2.5 (beta)")
        self.assertEqual(b.model_spec("retouch-inpaint-lab-gpt-2-mini").label,
                         "Retouch GPT 2.5 (beta)")
        self.assertEqual(b.model_spec("retouch-ideogram-4-5").label,
                         "Retouch Ideogram 4.5 (beta)")
        for m in b.catalogue:
            if m.note == magnific.BETA_NOTE:
                self.assertIn("(beta)", m.label)
            self.assertIn(m.kind, ("inpaint", "edit"))
        self.assertEqual(b.model_spec("retouch-classic").erase, "retouch-auto")
        self.assertEqual(b.model_spec("retouch-future").kind, "inpaint")
        self.assertEqual(b.model_spec("some-new-model").kind, "edit")
        nb = b.model_spec("imagen-nano-banana-2-flash")
        self.assertTrue(nb.extra["custom_size"])
        self.assertEqual(nb.extra["resolution"], "1k")

    def test_model_size(self):
        b = self.backend
        w, h = b.model_size(2000, 1000, "retouch-auto")
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertAlmostEqual(float(w) / h, 2.0, places=1)
        self.assertEqual(b.model_size(1000, 1000, "imagen-nano-banana-2-lite"), (1024, 1024))
        w, h = b.model_size(1600, 900, "flux-2")  # snaps to 16:9
        self.assertAlmostEqual(float(w) / h, 16 / 9.0, places=1)
        w, h = b.model_size(1000, 3000, "seedream-5-pro")  # custom size, clamped at 4:1
        self.assertAlmostEqual(float(w) / h, 0.333, places=1)

    # ---- retouch ----
    def test_retouch_replace(self):
        ids = iter(["R1", "R2", "R3"])
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": next(ids),
                                                      "status": "processing"}},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, out = self.run_with(tools, self.request(count=3))
        self.assertEqual(len(out), 3)
        # upload sequence: one request, two PUTs without extra headers, one finalize
        self.assertEqual(client.named("creations_request_upload"),
                         [{"mimeType": "image/png", "count": 2}])
        self.assertEqual([p[0] for p in self.puts], ["https://up/0", "https://up/1"])
        self.assertEqual([p[1] for p in self.puts], [PNG + b"img", PNG + b"msk"])
        self.assertEqual({p[2] for p in self.puts}, {"image/png"})
        fin = client.named("creations_finalize_upload")
        self.assertEqual(len(fin), 1)
        self.assertEqual(fin[0]["fileName"], "genfill_plate.png")
        self.assertFalse(fin[0]["visible"])
        self.assertEqual([u["path"] for u in fin[0]["uploads"]],
                         ["temp-files/0.png", "temp-files/1.png"])
        retouch = client.named("images_retouch")
        self.assertEqual(len(retouch), 3)
        self.assertEqual(retouch[0], {
            "creationIdentifier": "PLATE", "maskCreationIdentifier": "MASK",
            "mode": "replace", "prompt": "a red door", "model": "retouch-auto"})
        waits = client.named("creations_wait")
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["timeoutSeconds"], 25)
        self.assertEqual(len(waits[0]["identifiers"]), 3)

    def test_retouch_erase_and_fallback(self):
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": "R1"}},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, _ = self.run_with(tools, self.request(prompt="  ", count=1))
        self.assertEqual(client.named("images_retouch")[0]["mode"], "erase")
        self.assertNotIn("prompt", client.named("images_retouch")[0])
        # replace-only model falls back to Auto for erase
        client, _ = self.run_with(tools, self.request(
            prompt="", count=1, model="retouch-imagen-nano-banana-2-flash"))
        args = client.named("images_retouch")[0]
        self.assertEqual((args["mode"], args["model"]), ("erase", "retouch-auto"))
        self.assertNotIn("resolution", args)
        # replace on that model keeps it and asks for its cheapest resolution
        client, _ = self.run_with(tools, self.request(
            count=1, model="retouch-imagen-nano-banana-2-flash"))
        args = client.named("images_retouch")[0]
        self.assertEqual((args["mode"], args["model"], args["resolution"]),
                         ("replace", "retouch-imagen-nano-banana-2-flash", "1k"))

    # ---- edit models ----
    def test_edit_model_request(self):
        tools = upload_tools({
            "images_generate": lambda a: {"creations": [
                {"identifier": "G1"}, {"identifier": "G2"}]},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, out = self.run_with(tools, self.request(
            model="imagen-nano-banana-2-flash", seed=1234, width=640, height=480))
        self.assertEqual(len(out), 2)
        gen = client.named("images_generate")
        self.assertEqual(len(gen), 1)
        args = gen[0]
        self.assertEqual(args["count"], 2)
        self.assertEqual(args["seed"], 1234)
        self.assertEqual((args["width"], args["height"]), (640, 480))
        self.assertNotIn("aspectRatio", args)
        self.assertEqual(args["mode"], "imagen-nano-banana-2-flash")
        self.assertEqual(args["resolution"], "1k")
        self.assertEqual(args["references"], [{"type": "image", "identifier": "PLATE"}])
        self.assertIn("a red door", args["prompt"])
        self.assertIn("exactly the same", args["prompt"])
        self.assertEqual(client.named("images_retouch"), [])

    # ---- region prompts (boxes) ----
    BOXES = [layout.Box(1, 0, 0, 512, 384, "a red umbrella"),
             layout.Box(2, 512, 192, 1024, 768, "a black dog")]

    def test_layout_styles(self):
        b = self.backend
        self.assertEqual(b.layout_style("flux-3-image"), "flux3")
        for mid in ("ideogram-4-5", "retouch-ideogram-4-5", "retouch-precise-ideogram-4-5"):
            self.assertEqual(b.layout_style(mid), "ideogram", mid)
        for mid in ("retouch-auto", "flux-2", "gpt-2"):
            self.assertIsNone(b.layout_style(mid), mid)

    def test_flux3_edit_sends_boxes_in_the_prompt(self):
        spec = self.backend.model_spec("flux-3-image")
        req = self.request(model="flux-3-image", prompt="In <ref_image_0>, a rainy street",
                           boxes=self.BOXES)
        prompt = self.backend._edit_args(spec, req, "PLATE", 2)["prompt"]
        caption, rows = prompt.split(" [{", 1)
        self.assertEqual(caption, "In <ref_image_0>, a rainy street" + base.REF_PROMPT_SUFFIX)
        self.assertEqual("[{" + rows,
                         '[{"id": "obj_1", "bbox": [0, 0, 500, 500], "desc": "a red umbrella"}, '
                         '{"id": "obj_2", "bbox": [250, 500, 1000, 1000], "desc": "a black dog"}]')

    def test_ideogram_retouch_sends_a_json_prompt(self):
        spec = self.backend.model_spec("retouch-ideogram-4-5")
        req = self.request(model=spec.id, prompt="<obj_1> and <obj_2>", boxes=self.BOXES)
        args = self.backend._retouch_args(spec, req, "PLATE", "MASK")
        self.assertEqual(args["mode"], "replace")
        data = json.loads(args["prompt"])
        self.assertEqual(data["high_level_description"], "(a red umbrella) and (a black dog)")
        self.assertEqual([e["bbox"] for e in data["compositional_deconstruction"]["elements"]],
                         [[0, 0, 500, 500], [250, 500, 1000, 1000]])
        # boxes with an empty main prompt are a replace, not an erase
        args = self.backend._retouch_args(spec, self.request(model=spec.id, prompt="",
                                                             boxes=self.BOXES))
        self.assertEqual(args["mode"], "replace")
        self.assertIn("a red umbrella; a black dog", args["prompt"])

    def test_ideogram_edit_sends_a_json_prompt(self):
        spec = self.backend.model_spec("ideogram-4-5")
        req = self.request(model=spec.id, prompt="a street", boxes=self.BOXES)
        data = json.loads(self.backend._edit_args(spec, req, "PLATE", 1)["prompt"])
        self.assertEqual(data["high_level_description"], "a street" + base.REF_PROMPT_SUFFIX)
        req = self.request(model=spec.id, prompt="", boxes=self.BOXES)
        data = json.loads(self.backend._edit_args(spec, req, "PLATE", 1)["prompt"])
        self.assertEqual(data["high_level_description"], base.REF_PROMPT_SUFFIX.strip())

    def test_no_boxes_and_other_models_unchanged(self):
        for mid in ("flux-3-image", "ideogram-4-5", "gpt-2"):
            spec = self.backend.model_spec(mid)
            plain = self.backend._edit_args(spec, self.request(model=mid), "PLATE", 2)
            boxed = self.backend._edit_args(spec, self.request(model=mid, boxes=[]), "PLATE", 2)
            self.assertEqual(json.dumps(plain), json.dumps(boxed))
            self.assertEqual(plain["prompt"], "a red door" + base.REF_PROMPT_SUFFIX)
        spec = self.backend.model_spec("gpt-2")
        boxed = self.backend._edit_args(spec, self.request(model="gpt-2", boxes=self.BOXES),
                                        "PLATE", 2)
        self.assertEqual(boxed["prompt"], "a red door" + base.REF_PROMPT_SUFFIX)
        spec = self.backend.model_spec("retouch-ideogram-4-5")
        rargs = self.backend._retouch_args(spec, self.request(model=spec.id))
        self.assertEqual(rargs["prompt"], "a red door")
        rargs = self.backend._retouch_args(spec, self.request(model=spec.id, prompt=""))
        self.assertEqual(rargs["mode"], "erase")

    def test_edit_model_aspect_ratio_and_zero_seed(self):
        tools = upload_tools({
            "images_generate": lambda a: {"creations": [{"identifier": "G1"}]},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, _ = self.run_with(tools, self.request(
            model="flux-kontext", width=1600, height=896, count=1, prompt=""))
        args = client.named("images_generate")[0]
        self.assertEqual(args["aspectRatio"], "16:9")
        self.assertNotIn("width", args)
        self.assertNotIn("seed", args)
        self.assertIn("Remove the object", args["prompt"])

    def test_edit_quality_passthrough(self):
        tools = upload_tools({
            "images_generate": lambda a: {"creations": [{"identifier": "G1"}]},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        for model, quality, sent in (("gpt-2-mini", "high", "high"),
                                     ("gpt-2-mini", "low", "low"),
                                     ("gpt-2", "medium", "medium"),
                                     ("gpt-2", "auto", None),
                                     ("ideogram-4-5", "high", "high"),
                                     ("imagen-nano-banana-2-flash", "high", None)):
            client, _ = self.run_with(tools, self.request(model=model, quality=quality,
                                                          count=1))
            args = client.named("images_generate")[0]
            self.assertEqual(args["mode"], model)
            self.assertEqual(args.get("quality"), sent, (model, quality))
        self.assertEqual(args["resolution"], "1k")

    def test_resolution_tiers(self):
        """The Resolution setting: sizes, the ``resolution`` argument and clamps."""
        b = self.backend
        w, h = b.model_size(4000, 2250, "gpt-2", "2K")          # custom size: the tier
        self.assertAlmostEqual(w * h / 4194304.0, 1.0, delta=0.02)
        w, h = b.model_size(4000, 2250, "flux-2", "2K")         # snapped ratio at the tier
        self.assertAlmostEqual(w / float(h), 16 / 9.0, delta=0.01)
        self.assertGreater(w * h, 4000000)
        plan = b.size_plan(4000, 2250, "flux-2", "4K")
        self.assertEqual(plan.note, "model max 2K")
        self.assertIn("model max 2K", plan.text())
        self.assertEqual(b.size_plan(4000, 2250, "flux-kontext", "2K").note, "model max 1K")
        self.assertEqual(b.size_plan(4000, 2250, "retouch-imagen-nano-banana-2", "1K").note,
                         "model min 2K")
        self.assertEqual(b.model_size(900, 700, "retouch-auto", "4K"), (896, 704))   # crop size
        tools = upload_tools({
            "images_generate": lambda a: {"creations": [{"identifier": "G1"}]},
            "images_retouch": lambda a: {"creation": {"identifier": "R1"}},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        for model, res, tool, sent in (
                ("gpt-2", "2K", "images_generate", "2k"),
                ("gpt-2", "4K", "images_generate", "4k"),
                ("gpt-2", "auto", "images_generate", "1k"),
                ("flux-2", "4K", "images_generate", "2k"),          # clamped to the max
                ("flux-kontext", "2K", "images_generate", None),    # fixed size: no argument
                ("seedream-4-5", "4K", "images_generate", "4k"),
                ("retouch-imagen-nano-banana-2-flash", "4K", "images_retouch", "4k"),
                ("retouch-imagen-nano-banana-2", "1K", "images_retouch", "2k"),
                ("retouch-auto", "4K", "images_retouch", None)):   # no resolution parameter
            w, h = b.model_size(5000, 2800, model, res)
            client, _ = self.run_with(tools, self.request(model=model, count=1, width=w,
                                                          height=h, resolution=res))
            args = client.named(tool)[0]
            self.assertEqual(args.get("resolution"), sent, (model, res))
            if model == "gpt-2" and res == "2K":
                self.assertEqual((args["width"], args["height"]), (w, h))
        # A crop smaller than the tier asks for the tier that holds it.
        client, _ = self.run_with(tools, self.request(model="gpt-2", count=1, width=1024,
                                                      height=768, resolution="4K"))
        self.assertEqual(client.named("images_generate")[0]["resolution"], "1k")

    def test_discovered_resolution_tiers(self):
        specs = magnific.edit_specs({"models": [
            {"slug": "x", "name": "X", "supportsReferences": True, "referenceTypes": ["image"],
             "resolutions": ["1.5k", "2k", "3k", "4k"]}]})
        self.assertEqual(specs[0].extra["resolutions"], ("2K", "4K"))
        self.assertEqual(specs[0].extra["resolution"], "1.5k")

    def test_edit_per_model_aspect_ratios(self):
        tools = upload_tools({
            "images_generate": lambda a: {"creations": [{"identifier": "G1"}]},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        # 21:9 is in the default list but not in Flux.2's, which snaps to 2:1 instead.
        client, _ = self.run_with(tools, self.request(model="flux-2", width=2100, height=900,
                                                      count=1))
        self.assertEqual(client.named("images_generate")[0]["aspectRatio"], "2:1")
        w, h = self.backend.model_size(2100, 900, "flux-2")
        self.assertAlmostEqual(float(w) / h, 2.0, places=1)
        w, h = self.backend.model_size(2100, 900, "flux-kontext")  # default list: 21:9
        self.assertGreater(float(w) / h, 2.2)
        client, _ = self.run_with(tools, self.request(model="seedream-5-flash", width=1250,
                                                      height=1000, count=1))
        args = client.named("images_generate")[0]
        self.assertEqual(args["aspectRatio"], "4:3")    # no 5:4 for Seedream 5 Flash
        self.assertEqual(args["resolution"], "1.5k")
        client, _ = self.run_with(tools, self.request(model="seedream-5-lite", count=1))
        self.assertEqual(client.named("images_generate")[0]["resolution"], "2k")

    def test_new_retouch_models(self):
        ids = iter(["R%d" % i for i in range(10)])
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": next(ids)}},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, _ = self.run_with(tools, self.request(model="retouch-inpaint-lab-gpt-2-mini",
                                                      count=1))
        args = client.named("images_retouch")[0]
        self.assertEqual((args["model"], args["mode"], args["resolution"]),
                         ("retouch-inpaint-lab-gpt-2-mini", "replace", "1k"))
        client, _ = self.run_with(tools, self.request(model="retouch-ideogram-4-5", count=1,
                                                      prompt=""))
        args = client.named("images_retouch")[0]
        self.assertEqual((args["model"], args["mode"]), ("retouch-ideogram-4-5", "erase"))
        self.assertNotIn("resolution", args)

    # ---- waiting ----
    def test_wait_loop_with_pending_round(self):
        rounds = [
            {"results": [{"identifier": "R1", "status": "processing", "poll_after_seconds": 2},
                         {"identifier": "R2", "status": "completed",
                          "results": {"url": "https://cdn/R2.png"}}], "allTerminal": False},
            done(["R1"]),
        ]
        ids = iter(["R1", "R2"])
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": next(ids)}},
            "creations_wait": rounds,
        })
        client, out = self.run_with(tools, self.request())
        waits = client.named("creations_wait")
        self.assertEqual(waits[0]["identifiers"], ["R1", "R2"])
        self.assertEqual(waits[1]["identifiers"], ["R1"])  # only the unfinished one
        self.assertEqual(out, [PNG + b"https://cdn/R1.png", PNG + b"https://cdn/R2.png"])

    def test_more_than_eight_ids_are_chunked(self):
        tools = upload_tools({
            "images_generate": lambda a: {"creations": [{"identifier": "G%d" % i}
                                                        for i in range(8)]},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, out = self.run_with(tools, self.request(model="seedream-5-pro", count=20))
        self.assertEqual(client.named("images_generate")[0]["count"], 8)
        self.assertEqual(len(out), 8)
        self.assertLessEqual(max(len(a["identifiers"]) for a in client.named("creations_wait")), 8)

    def test_failed_job(self):
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": "R1"}},
            "creations_wait": lambda a: {"results": [
                {"identifier": "R1", "status": "failed", "error": "content blocked"}],
                "allTerminal": True},
        })
        with self.assertRaises(base.BackendError) as cm:
            self.run_with(tools, self.request(count=1))
        self.assertIn("failed", str(cm.exception))
        self.assertIn("content blocked", str(cm.exception))

    def test_one_failed_job_keeps_the_others(self):
        ids = iter(["R1", "R2", "R3"])
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": next(ids)}},
            "creations_wait": lambda a: {"results": [
                {"identifier": "R1", "status": "completed", "results": {"url": "https://cdn/R1"}},
                {"identifier": "R2", "status": "failed", "error": "filtered"},
                {"identifier": "R3", "status": "completed", "results": {"url": "https://cdn/R3"}}],
                "allTerminal": True},
        })
        _, out = self.run_with(tools, self.request(count=3))
        self.assertEqual(out, [PNG + b"https://cdn/R1", PNG + b"https://cdn/R3"])

    def test_one_failed_download_keeps_the_others(self):
        ids = iter(["R1", "R2", "R3"])

        def dl(url, **k):
            if url.endswith("R1.png"):
                raise base.BackendError("Could not download the result")
            return PNG + url.encode()
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": next(ids)}},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        with mock.patch.object(magnific, "download", dl):
            _, out = self.run_with(tools, self.request(count=2))
        self.assertEqual(out, [PNG + b"https://cdn/R2.png"])
        with mock.patch.object(magnific, "download",
                               mock.Mock(side_effect=base.BackendError("gone"))):
            with self.assertRaises(base.BackendError) as cm:
                self.run_with(tools, self.request(count=1))
        self.assertIn("gone", str(cm.exception))

    def test_failed_submit_keeps_queued_jobs(self):
        answers = [{"creation": {"identifier": "R1"}}, "Error: insufficient credits"]
        tools = upload_tools({
            "images_retouch": lambda a: answers.pop(0),
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, out = self.run_with(tools, self.request(count=3))
        self.assertEqual(len(client.named("images_retouch")), 2)   # stops after the failure
        self.assertEqual(out, [PNG + b"https://cdn/R1.png"])

    def test_wait_error_after_progress_keeps_finished(self):
        rounds = [
            {"results": [{"identifier": "R1", "status": "completed",
                          "results": {"url": "https://cdn/R1.png"}},
                         {"identifier": "R2", "status": "processing"}], "allTerminal": False},
            "Error: server unavailable",
        ]
        ids = iter(["R1", "R2"])
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": next(ids)}},
            "creations_wait": rounds,
        })
        _, out = self.run_with(tools, self.request(count=2))
        self.assertEqual(out, [PNG + b"https://cdn/R1.png"])

    def test_negative_seed_is_wrapped_to_uint32(self):
        tools = upload_tools({
            "images_generate": lambda a: {"creations": [{"identifier": "G1"}]},
            "creations_wait": lambda a: done(a["identifiers"]),
        })
        client, _ = self.run_with(tools, self.request(model="flux-kontext", seed=-1, count=1))
        self.assertEqual(client.named("images_generate")[0]["seed"], 4294967295)
        client, _ = self.run_with(tools, self.request(model="flux-kontext", seed=2 ** 32 + 5,
                                                      count=1))
        self.assertEqual(client.named("images_generate")[0]["seed"], 5)

    def test_rounds_without_progress_pause(self):
        rounds = [
            {"results": [{"identifier": "R1", "status": "processing", "poll_after_seconds": 3}],
             "allTerminal": False},
            {"results": [{"identifier": "R1", "status": "processing", "poll_after_seconds": 60}],
             "allTerminal": False},
            {"results": [{"identifier": "R1", "status": "processing"}], "allTerminal": False},
            done(["R1"]),
        ]
        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": "R1"}},
            "creations_wait": rounds,
        })
        with mock.patch.object(magnific, "pause") as pause:
            self.run_with(tools, self.request(count=1))
        self.assertEqual([c[0][0] for c in pause.call_args_list], [3.0, 5.0, 1.0])
        # the real pause stays cancellable
        with self.assertRaises(base.Cancelled):
            magnific.pause(5.0, lambda: True)

    def test_non_json_result_is_backend_error(self):
        tools = upload_tools({"images_retouch": lambda a: "Error: insufficient credits " * 50})
        with self.assertRaises(base.BackendError) as cm:
            self.run_with(tools, self.request(count=1))
        self.assertIn("insufficient credits", str(cm.exception))
        self.assertLess(len(str(cm.exception)), 500)

    def test_cancel(self):
        state = {"n": 0}

        def cancelled():
            state["n"] += 1
            return state["n"] > 3

        tools = upload_tools({
            "images_retouch": lambda a: {"creation": {"identifier": "R1"}},
            "creations_wait": lambda a: {"results": [
                {"identifier": "R1", "status": "processing"}], "allTerminal": False},
        })
        with mock.patch("genfill.backends.cloud.time.sleep"):
            with self.assertRaises(base.Cancelled):
                self.run_with(tools, self.request(count=1), cancelled=cancelled)


GENERATION_TOOLS = ("images_retouch", "images_generate", "creations_request_upload",
                    "creations_finalize_upload", "creations_wait")


class MagnificCostTests(unittest.TestCase):
    """Cost preview and balance: simulate_cost / account_balance only, never a generation."""

    def setUp(self):
        self.backend = MagnificBackend()

    def quote(self, credits, **kw):
        args = dict(image_path=None, mask_path=None, mask_alpha_path=None, prompt="a red car",
                    width=1024, height=1024, count=3, model="retouch-auto", quality="auto")
        args.update(kw)
        client = FakeClient({"simulate_cost": lambda a: {"credits": credits,
                                                         "isUnlimited": False,
                                                         "certainty": "exact"}})
        with mock.patch.object(MagnificBackend, "quiet_client", return_value=client):
            q = self.backend.estimate(base.GenerationRequest(**args), "")
        for tool in GENERATION_TOOLS:
            self.assertEqual(client.named(tool), [], tool)
        return q, client.named("simulate_cost")

    def test_retouch_is_n_times_one_call(self):
        q, calls = self.quote(10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "images_retouch")
        self.assertEqual(calls[0]["arguments"],
                         {"mode": "replace", "model": "retouch-auto", "prompt": "a red car"})
        self.assertEqual((q.amount, q.unit, q.exact, q.count), (30, "credits", True, 3))
        self.assertEqual(q.text(), "30 credits for 3")

    def test_quote_with_boxes_and_no_prompt_is_no_erase(self):
        """The panel's quote picks erase or replace from the boxes, like Generate."""
        from genfill import pricing
        self.addCleanup(pricing.reset)
        client = FakeClient({"simulate_cost": lambda a: {"credits": 10, "isUnlimited": False,
                                                         "certainty": "exact"}})
        with mock.patch.object(MagnificBackend, "quiet_client", return_value=client):
            for boxes in (False, True):
                snap = pricing.snapshot(self.backend, "retouch-ideogram-4-5", "auto", "auto", 1,
                                        1024, 1024, "", "", auth=(False, False), boxes=boxes)
                pricing._fetch_quote(snap, pricing.key(snap))
        modes = [c["arguments"]["mode"] for c in client.named("simulate_cost")]
        self.assertEqual(modes, ["erase", "replace"])

    def test_retouch_erase_and_resolution(self):
        _, calls = self.quote(150, prompt="", model="retouch-inpaint-lab-nano-banana-pro",
                              resolution="4K", width=4096, height=4096, count=1)
        self.assertEqual(calls[0]["arguments"]["mode"], "erase")
        q, calls = self.quote(150, model="retouch-inpaint-lab-nano-banana-pro",
                              resolution="4K", width=4096, height=4096, count=1)
        self.assertEqual(calls[0]["arguments"]["resolution"], "4k")
        self.assertNotIn("creationIdentifier", calls[0]["arguments"])

    def test_edit_quotes_all_images_without_references(self):
        q, calls = self.quote(45, model="gpt-2", quality="low", resolution="1K")
        args = calls[0]["arguments"]
        self.assertEqual(calls[0]["tool"], "images_generate")
        self.assertEqual(args["count"], 3)
        self.assertEqual(args["mode"], "gpt-2")
        self.assertEqual(args["quality"], "low")
        self.assertEqual((args["width"], args["height"]), (1024, 1024))
        self.assertNotIn("references", args)
        self.assertEqual(q.amount, 45)     # one call covers every image

    def test_generate_args_unchanged(self):
        spec = self.backend.model_spec("gpt-2")
        req = base.GenerationRequest(None, None, None, "x", 1024, 1024, seed=5, count=2,
                                     model="gpt-2")
        args = self.backend._edit_args(spec, req, "PLATE", 2)
        self.assertEqual(args["references"], [{"type": "image", "identifier": "PLATE"}])
        self.assertEqual(args["seed"], 5)
        rargs = self.backend._retouch_args(self.backend.model_spec("retouch-auto"), req,
                                           "PLATE", "MASK")
        self.assertEqual((rargs["creationIdentifier"], rargs["maskCreationIdentifier"]),
                         ("PLATE", "MASK"))

    def test_balance(self):
        client = FakeClient({"account_balance": lambda a: {
            "plan": {"tier": "magnific", "productName": "Premium+", "isPaidPlan": True,
                     "isUnlimitedMode": False, "unlimitedAppliesHere": False},
            "credits": {"available": 243317, "totalPlan": 570000, "spent": 326683,
                        "hasExtraCredits": True, "scope": "personal"}}})
        with mock.patch.object(MagnificBackend, "quiet_client", return_value=client):
            b = self.backend.balance("")
        self.assertEqual(b.text(), "balance 243,317 (Premium+)")

    def test_quote_needs_sign_in_without_browser(self):
        from genfill.backends.oauth import LoginRequired
        provider = mock.Mock()
        provider.signed_in.return_value = False
        with mock.patch.object(MagnificBackend, "provider", return_value=provider), \
                mock.patch.object(MagnificBackend, "client") as client:
            with self.assertRaises(LoginRequired):
                self.backend.estimate(base.GenerationRequest(None, None, None, "", 64, 64), "")
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
