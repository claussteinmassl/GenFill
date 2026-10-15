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
            return {"uploads": [{"upload_url": "https://up/x?sig=1", "media_id": "m1",
                                 "content_type": "image/png"}]}
        if name == "media_confirm":
            return {"results": [{"media_id": "m1", "status": "uploaded"}]}
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
