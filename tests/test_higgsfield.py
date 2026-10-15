"""Higgsfield backend against a scripted MCP client (no network).

Run: python -m unittest tests.test_higgsfield -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill.backends import base, higgsfield  # noqa: E402
from genfill.backends.base import BackendError, Cancelled, GenerationRequest  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class FakeClient(object):
    def __init__(self, wait_script=None, submit=None):
        self.calls = []
        self.wait_script = list(wait_script or [])
        self.submit = submit

    def call_tool(self, name, args, timeout=None):
        self.calls.append((name, args))
        if name == "media_upload":
            mid = "m%d" % len(self.named("media_upload"))   # m1, m2, ...
            return {"uploads": [{"upload_url": "https://up/x?sig=1", "media_id": mid,
                                 "content_type": "image/png"}]}
        if name == "media_confirm":
            return {"results": [{"media_id": args["media_ids"][0], "status": "uploaded"}]}
        if name == "generate_image_batch":
            if self.submit is not None:
                return self.submit
            return {"jobs": [{"index": r["index"], "job_id": "j%d" % r["index"],
                              "status": "pending"} for r in args["requests"]]}
        if name == "jobs_wait":
            return self.wait_script.pop(0)
        raise AssertionError(name)

    def named(self, name):
        return [a for n, a in self.calls if n == name]


def done(*statuses):
    jobs = [{"index": i, "job_id": "j%d" % i, "status": s,
             "result_url": "https://cdn/r%d.png" % i if s == "completed" else None}
            for i, s in enumerate(statuses)]
    return {"jobs": jobs, "all_terminal": True,
            "summary": {"completed": statuses.count("completed"),
                        "failed": len(statuses) - statuses.count("completed")}}


class HiggsfieldTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".png")
        os.write(fd, PNG)
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        self.backend = higgsfield.HiggsfieldBackend()
        self.put = mock.patch.object(higgsfield, "put_bytes").start()
        self.dl = mock.patch.object(higgsfield, "download", return_value=PNG).start()
        mock.patch("time.sleep").start()
        mock.patch("genfill.log.warning").start()
        self.addCleanup(mock.patch.stopall)

    def request(self, **kw):
        args = dict(image_path=self.path, mask_path=self.path, mask_alpha_path=self.path,
                    prompt="a red car", width=1168, height=880, seed=7, count=2,
                    model="gpt_image_2_5", quality="auto")
        args.update(kw)
        return GenerationRequest(**args)

    def run_generate(self, client, request=None, cancelled=None):
        with mock.patch.object(higgsfield.HiggsfieldBackend, "client", return_value=client):
            return self.backend.generate(request or self.request(), "", None, cancelled)

    def test_identity(self):
        self.assertEqual(self.backend.server_url, "https://mcp.higgsfield.ai/mcp")
        self.assertEqual(self.backend.default_model, "gpt_image_2_5")
        self.assertTrue(all(m.kind == "edit" for m in self.backend.catalogue))

    def test_upload_and_generate_shape(self):
        client = FakeClient([done("completed", "completed")])
        out = self.run_generate(client)
        self.assertEqual(out, [PNG, PNG])
        self.assertEqual(client.named("media_upload")[0]["content_type"], "image/png")
        self.put.assert_called_once()
        self.assertEqual(self.put.call_args[0][0], "https://up/x?sig=1")
        self.assertEqual(self.put.call_args[0][2], "image/png")
        self.assertEqual(client.named("media_confirm")[0],
                         {"type": "image", "media_ids": ["m1"]})
        reqs = client.named("generate_image_batch")[0]["requests"]
        self.assertEqual([r["index"] for r in reqs], [0, 1])
        p = reqs[0]["params"]
        self.assertEqual(p["model"], "gpt_image_2_5")
        self.assertEqual(p["medias"], [{"role": "image_references", "value": "m1"}])
        self.assertEqual(p["aspect_ratio"], "4:3")
        self.assertIs(p["use_unlim"], False)
        self.assertTrue(p["prompt"].startswith("a red car"))
        self.assertNotIn("quality", p)      # "auto" = model default
        self.assertNotIn("seed", p)         # gpt_image_2_5 has no seed
        self.assertNotIn("mask", str(p))
        self.assertEqual(client.named("jobs_wait")[0]["timeout_seconds"], 15)

    def test_quality_and_seed(self):
        client = FakeClient([done("completed")])
        self.run_generate(client, self.request(count=1, quality="high"))
        self.assertEqual(client.named("generate_image_batch")[0]["requests"][0]
                         ["params"]["quality"], "high")
        client = FakeClient([done("completed", "completed")])
        self.run_generate(client, self.request(model="nano_banana_2_1", quality="high"))
        reqs = client.named("generate_image_batch")[0]["requests"]
        self.assertEqual([r["params"]["seed"] for r in reqs], [7, 8])
        self.assertNotIn("quality", reqs[0]["params"])

    def test_resolution_tiers(self):
        b = self.backend
        w, h = b.model_size(5000, 2800, "gpt_image_2_5", "2K")
        self.assertAlmostEqual(w * h / 4194304.0, 1.0, delta=0.03)
        self.assertEqual(b.size_plan(5000, 2800, "kling_omni_image", "4K").note, "model max 2K")
        self.assertEqual(b.size_plan(5000, 2800, "nano_banana", "2K").note, "fixed size")
        self.assertEqual(b.model_size(5000, 2800, "nano_banana", "2K"),
                         b.model_size(5000, 2800, "nano_banana"))
        for model, res, sent in (("gpt_image_2_5", "2K", "2k"), ("gpt_image_2_5", "4K", "4k"),
                                 ("gpt_image_2_5", "auto", "1k"),
                                 ("kling_omni_image", "4K", "2k"),
                                 ("nano_banana_2_lite", "4K", "1k"),
                                 ("nano_banana", "4K", None)):
            w, h = b.model_size(5000, 2800, model, res)
            client = FakeClient([done("completed")])
            self.run_generate(client, self.request(model=model, count=1, width=w, height=h,
                                                   resolution=res))
            p = client.named("generate_image_batch")[0]["requests"][0]["params"]
            self.assertEqual(p.get("resolution"), sent, (model, res))

    def test_discovered_resolution_tiers(self):
        specs = higgsfield.explore_specs([{
            "id": "x_model", "name": "X", "provider_name": "P", "description": "d",
            "aspect_ratios": ["1:1", "16:9"],
            "medias": [{"type": "image", "roles": ["image_references"]}],
            "parameters": [{"name": "resolution", "options": ["1k", "1.5k", "2k"]}]}])
        self.assertEqual(specs[0].extra["resolutions"], ("1K", "2K"))
        self.assertEqual(specs[0].extra["resolution"], "1k")

    def test_wait_loop_until_terminal(self):
        pending = {"jobs": [], "all_terminal": False, "poll_after_seconds": 1,
                   "summary": {"completed": 0, "failed": 0}}
        client = FakeClient([pending, pending, done("completed", "completed")])
        self.assertEqual(len(self.run_generate(client)), 2)
        self.assertEqual(len(client.named("jobs_wait")), 3)

    def test_partial_failure_returns_completed(self):
        client = FakeClient([done("completed", "failed")])
        self.assertEqual(len(self.run_generate(client)), 1)

    def test_all_failed(self):
        with self.assertRaises(BackendError) as cm:
            self.run_generate(FakeClient([done("failed", "failed")]))
        self.assertIn("did not produce", str(cm.exception))
        with self.assertRaises(BackendError) as cm:
            self.run_generate(FakeClient([done("nsfw", "nsfw")]))
        self.assertIn("content filter", str(cm.exception))

    def test_one_failed_download_keeps_the_others(self):
        def dl(url, **k):
            if url.endswith("r0.png"):
                raise BackendError("Could not download the result")
            return PNG
        self.dl.side_effect = dl
        out = self.run_generate(FakeClient([done("completed", "completed")]))
        self.assertEqual(out, [PNG])
        self.dl.side_effect = BackendError("gone")
        with self.assertRaises(BackendError) as cm:
            self.run_generate(FakeClient([done("completed", "completed")]))
        self.assertIn("gone", str(cm.exception))

    def test_wait_timeout_keeps_finished_jobs(self):
        part = done("completed", "pending")
        part.update(all_terminal=False)
        client = FakeClient([part] * 50)
        with mock.patch.object(higgsfield, "RESULT_TIMEOUT", -1):
            out = self.run_generate(client)
        self.assertEqual(out, [PNG])
        with mock.patch.object(higgsfield, "RESULT_TIMEOUT", -1):
            with self.assertRaises(BackendError) as cm:
                self.run_generate(FakeClient([{"jobs": [], "all_terminal": False}] * 5))
        self.assertIn("Timed out", str(cm.exception))

    def test_submit_rejected(self):
        with self.assertRaises(BackendError):
            self.run_generate(FakeClient(submit={"jobs": [], "failed_count": 2}))
        with self.assertRaises(BackendError):
            self.run_generate(FakeClient(submit="Submitted 0/2: submission_failed"))

    def test_cancel(self):
        client = FakeClient([done("completed", "completed")])
        with self.assertRaises(Cancelled):
            self.run_generate(client, cancelled=lambda: True)
        self.assertEqual(client.named("generate_image_batch"), [])

    def test_cancel_while_waiting(self):
        flag = {"n": 0}

        def cancelled():
            flag["n"] += 1
            return client.named("jobs_wait") != []
        client = FakeClient([{"jobs": [], "all_terminal": False,
                              "summary": {"completed": 0, "failed": 0}}])
        with self.assertRaises(Cancelled):
            self.run_generate(client, cancelled=cancelled)

    def test_non_image_result(self):
        self.dl.return_value = b"<html>"
        with self.assertRaises(BackendError):
            self.run_generate(FakeClient([done("completed", "completed")]))

    def test_media_role_per_model(self):
        client = FakeClient([done("completed")])
        self.run_generate(client, self.request(count=1, model="gpt_image_2"))
        p = client.named("generate_image_batch")[0]["requests"][0]["params"]
        self.assertEqual(p["medias"], [{"role": "image", "value": "m1"}])
        for m in self.backend.catalogue:
            want = "image" if m.id == "gpt_image_2" else "image_references"
            self.assertEqual(m.extra["media_role"], want, m.id)

    def test_mask_role_models_stay_reference_edits_until_live_tested(self):
        self.assertFalse(higgsfield.MASK_ROLE_LIVE)
        for mid in ("nano_banana_2", "nano_banana_2_1", "nano_banana_2_lite", "ideogram_4_5"):
            spec = self.backend.model_spec(mid)
            self.assertEqual((spec.kind, spec.extra["mask"]), ("edit", False), mid)
        for mid in ("openai_hazel", "grok_image_2_0", "grok_image", "kling_omni_image"):
            self.assertIn(mid, self.backend.model_ids())

    def masked_backend(self, spec):
        backend = higgsfield.HiggsfieldBackend()
        backend.catalogue = (spec,)
        return backend

    def test_mask_media_for_mask_role_models(self):
        spec = higgsfield._spec("nano_banana_2", "Nano Banana 2", higgsfield._CLASSIC,
                                mask=True, inpaint_flag=True)
        self.assertEqual(spec.kind, "inpaint")
        with open(self.path, "wb") as fh:
            fh.write(PNG + b"plate")
        fd, mask_path = tempfile.mkstemp(suffix=".png")
        os.write(fd, PNG + b"mask")
        os.close(fd)
        self.addCleanup(os.remove, mask_path)
        self.backend = self.masked_backend(spec)
        client = FakeClient([done("completed", "completed")])
        out = self.run_generate(client, self.request(model="nano_banana_2", mask_path=mask_path,
                                                     prompt=""))
        self.assertEqual(len(out), 2)
        uploads = client.named("media_upload")
        self.assertEqual([u["filename"] for u in uploads],
                         ["genfill_plate.png", "genfill_mask.png"])
        self.assertEqual([c["media_ids"] for c in client.named("media_confirm")],
                         [["m1"], ["m2"]])
        self.assertEqual([c[0][1] for c in self.put.call_args_list],
                         [PNG + b"plate", PNG + b"mask"])     # the RGB mask, white = fill
        for r in client.named("generate_image_batch")[0]["requests"]:
            p = r["params"]
            self.assertEqual(p["medias"], [{"role": "image_references", "value": "m1"},
                                           {"role": "mask", "value": "m2"}])
            self.assertIs(p["is_inpaint"], True)
            self.assertIn("masked area", p["prompt"])      # mask wording, not the ref wording

    def test_mask_media_without_inpaint_flag(self):
        spec = higgsfield._spec("ideogram_4_5", "Ideogram 4.5", higgsfield._IDEOGRAM,
                                seed=True, quality={"high": "high"}, mask=True)
        self.backend = self.masked_backend(spec)
        client = FakeClient([done("completed")])
        self.run_generate(client, self.request(model="ideogram_4_5", count=1, quality="high"))
        p = client.named("generate_image_batch")[0]["requests"][0]["params"]
        self.assertEqual([m["role"] for m in p["medias"]], ["image_references", "mask"])
        self.assertNotIn("is_inpaint", p)
        self.assertEqual((p["quality"], p["seed"]), ("high", 7))
        self.assertTrue(p["prompt"].startswith("a red car Only change the masked area"))

    def test_seedream_never_picks_unsupported_ratio(self):
        for model in ("seedream_5_0_flash", "seedream_v5_lite", "seedream_v4_5"):
            ratios = self.backend.model_spec(model).extra["ratios"]
            self.assertNotIn("4:5", ratios)
            self.assertNotIn("5:4", ratios)
            for w, h in ((1000, 1250), (1250, 1000), (1000, 1000), (1920, 1080), (1000, 1500)):
                mw, mh = self.backend.model_size(w, h, model)
                got = higgsfield.nearest_ratio(mw, mh, ratios)
                self.assertIn(got, ratios)
                self.assertNotIn(got, ("4:5", "5:4"))
        lite = self.backend.model_spec("seedream_v5_lite").extra["ratios"]
        self.assertNotIn("3:2", lite)
        self.assertNotIn("2:3", lite)
        self.assertNotIn("21:9", self.backend.model_spec("ideogram_4_5").extra["ratios"])

    def test_model_size_matches_model_ratio(self):
        for model in self.backend.model_ids():
            ratios = self.backend.model_spec(model).extra["ratios"]
            for w, h in ((1000, 1000), (1920, 1080), (900, 1300), (4000, 500)):
                mw, mh = self.backend.model_size(w, h, model)
                got = higgsfield.nearest_ratio(mw, mh, ratios)
                a, b = (float(x) for x in got.split(":"))
                self.assertLess(abs(mw / float(mh) - a / b) / (a / b), 0.03, (model, w, h))
        self.assertEqual(higgsfield.nearest_ratio(1168, 880, ["1:1", "4:3", "16:9"]), "4:3")


if __name__ == "__main__":
    unittest.main()
