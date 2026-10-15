"""Magnific MCP backend against a scripted fake MCP client (no network).

Run: python -m unittest tests.test_magnific -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))
sys.path.insert(0, os.path.dirname(__file__))

from genfill.backends import base, magnific  # noqa: E402
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
        self.assertEqual(len(ids), 18)
        self.assertNotIn("retouch-reference", ids)
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


if __name__ == "__main__":
    unittest.main()
