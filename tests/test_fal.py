"""fal.ai backend tests with a scripted fake in place of base.http_request."""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import pngio  # noqa: E402
from genfill.backends import base, fal  # noqa: E402


def _png(w=8, h=6, value=255):
    img = pngio.Image(w, h, 3)
    for row in img.rows:
        row[:] = bytes(bytearray([value] * (w * 3)))
    return pngio.encode(img)


def _soft_mask(w=8, h=6):
    img = pngio.Image(w, h, 3)
    for row in img.rows:
        row[:] = bytes(bytearray([10, 10, 10] + [200, 200, 200] * (w - 1)))
    return pngio.encode(img)


class FakeFal(object):
    """Scripted fal.ai: records calls, serves uploads, queue, status and result."""

    def __init__(self, status_script=None, result=None, upload="cdn", submit_status=200,
                 submit_body=None, result_status=200, result_body=None):
        self.calls = []
        self.status_script = list(status_script or [{"status": "COMPLETED"}])
        self.result = result
        self.upload = upload
        self.submit_status = submit_status
        self.submit_body = submit_body
        self.result_status = result_status
        self.result_body = result_body
        self.counter = 0
        self.downloads = {}

    def jbody(self, call):
        return json.loads(call["body"].decode("utf-8"))

    def find(self, method, needle):
        return [c for c in self.calls if c["method"] == method and needle in c["url"]]

    def __call__(self, method, url, body=None, headers=None, timeout=180):
        self.calls.append({"method": method, "url": url, "body": body, "headers": headers or {}})
        out = lambda s, d: (s, {}, json.dumps(d).encode("utf-8") if not isinstance(d, bytes) else d)
        if "storage/auth/token" in url:
            if self.upload == "cdn":
                return out(200, {"token": "T", "token_type": "Bearer",
                                 "base_url": "https://v3b.fal.media"})
            return out(500, {"detail": "no"})
        if url == fal.CDN_UPLOAD_URL:
            self.counter += 1
            return out(200, {"access_url": "https://v3b.fal.media/files/b/%d.png" % self.counter})
        if "storage/upload/initiate" in url:
            if self.upload in ("cdn", "initiate"):
                self.counter += 1
                return out(200, {"upload_url": "https://signed/%d" % self.counter,
                                 "file_url": "https://v3.fal.media/files/i/%d.png" % self.counter})
            return out(500, {"detail": "no"})
        if method == "PUT" and url.startswith("https://signed/"):
            return out(200, {})
        if method == "PUT" and url.endswith("/cancel"):
            return out(202, {"status": "CANCELLATION_REQUESTED"})
        if method == "POST" and url.startswith(fal.QUEUE_ROOT + "/"):
            if self.submit_status != 200:
                return out(self.submit_status, self.submit_body or {})
            self.counter += 1
            ep = url[len(fal.QUEUE_ROOT) + 1:]
            root = "%s/%s/requests/r%d" % (fal.QUEUE_ROOT, "/".join(ep.split("/")[:2]), self.counter)
            return out(200, {"request_id": "r%d" % self.counter, "status_url": root + "/status",
                             "response_url": root, "cancel_url": root + "/cancel"})
        if url.split("?")[0].endswith("/status"):
            step = self.status_script.pop(0) if len(self.status_script) > 1 else self.status_script[0]
            return out(200, step)
        if "/requests/" in url and method == "GET":
            if self.result_status != 200:
                return out(self.result_status, self.result_body or {})
            return out(200, self.result if self.result is not None else
                       {"images": [{"url": "https://v3b.fal.media/out/%s.png" % url[-2:]}]})
        if method == "GET" and url.startswith("https://v3b.fal.media/out/"):
            return 200, {}, self.downloads.get(url, _png(8, 6, 7))
        raise AssertionError("unexpected request %s %s" % (method, url))


class FalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = {}
        for name, data in (("image", _png()), ("mask", _soft_mask())):
            p = os.path.join(self.tmp, name + ".png")
            with open(p, "wb") as fh:
                fh.write(data)
            self.paths[name] = p
        self.backend = fal.FalBackend()
        patcher = mock.patch.object(fal.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch("genfill.log.warning")   # partial-result notes
        quiet.start()
        self.addCleanup(quiet.stop)

    def plate(self):
        with open(self.paths["image"], "rb") as fh:
            return fh.read()

    def run_fal(self, fake, prompt="a red door", count=1, model="", cancelled=None, seed=100,
                quality="auto", negative=""):
        req = base.GenerationRequest(self.paths["image"], self.paths["mask"], self.paths["mask"],
                                     prompt, 8, 6, seed=seed, count=count, model=model,
                                     negative_prompt=negative, quality=quality)
        with mock.patch.object(base, "http_request", fake):
            return self.backend.generate(req, "id:secret", cancelled=cancelled)

    # ----------------------------------------------------------------- basics

    def test_identity(self):
        b = self.backend
        self.assertEqual((b.id, b.label, b.env_var, b.needs_key, b.default_model),
                         ("fal", "fal.ai", "FAL_KEY", True, "fal-ai/flux-pro/v1/fill"))
        self.assertEqual(len(b.catalogue), 20)
        self.assertEqual(b.model_spec("").erase, "fal-ai/flux-pro/v1/erase")

    def test_submit_poll_result_and_cdn_upload(self):
        fake = FakeFal(status_script=[{"status": "IN_QUEUE"}, {"status": "IN_PROGRESS"},
                                      {"status": "COMPLETED"}])
        out = self.run_fal(fake, count=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(pngio.size_of(out[0]), (8, 6))
        submit = fake.find("POST", "queue.fal.run/fal-ai/flux-pro/v1/fill")[0]
        self.assertEqual(submit["headers"]["Authorization"], "Key id:secret")
        body = fake.jbody(submit)
        self.assertEqual(body["image_url"], "https://v3b.fal.media/files/b/1.png")
        self.assertEqual(body["mask_url"], "https://v3b.fal.media/files/b/2.png")
        self.assertEqual((body["prompt"], body["num_images"], body["seed"], body["output_format"]),
                         ("a red door", 1, 100, "png"))
        self.assertNotIn("webp", json.dumps(body))
        # polling used the returned status url with logs=0 and the Key header
        polls = [c for c in fake.calls if c["url"].split("?")[0].endswith("/status")]
        self.assertEqual(len(polls), 3)
        self.assertTrue(polls[0]["url"].endswith("/fal-ai/flux-pro/requests/r3/status?logs=0"))
        self.assertEqual(polls[0]["headers"]["Authorization"], "Key id:secret")
        self.assertEqual(self.sleep.call_count, 2)
        # uploads: token once, two files, upload header uses the token
        self.assertEqual(len(fake.find("POST", "storage/auth/token")), 1)
        ups = [c for c in fake.calls if c["url"] == fal.CDN_UPLOAD_URL]
        self.assertEqual([c["headers"]["Authorization"] for c in ups], ["Bearer T", "Bearer T"])

    def test_mask_is_binarized_opaque(self):
        fake = FakeFal()
        self.run_fal(fake)
        ups = [c for c in fake.calls if c["url"] == fal.CDN_UPLOAD_URL]
        mask = pngio.read(ups[1]["body"])
        self.assertEqual(set(mask.rows[0]), {0, 255})
        self.assertEqual(mask.rows[0][0], 0)
        self.assertEqual(mask.rows[0][3], 255)

    def test_upload_fallback_initiate(self):
        fake = FakeFal(upload="initiate")
        self.run_fal(fake)
        body = fake.jbody(fake.find("POST", "queue.fal.run/")[0])
        self.assertTrue(body["image_url"].startswith("https://v3.fal.media/files/i/"))
        puts = fake.find("PUT", "https://signed/")
        self.assertEqual(len(puts), 2)
        self.assertNotIn("Authorization", puts[0]["headers"])
        self.assertEqual(puts[0]["body"], self.plate())

    def test_upload_fallback_data_uri(self):
        fake = FakeFal(upload="none")
        self.run_fal(fake)
        body = fake.jbody(fake.find("POST", "queue.fal.run/")[0])
        for key in ("image_url", "mask_url"):
            self.assertTrue(body[key].startswith("data:image/png;base64,"))
        img = base64.b64decode(body["image_url"].split(",", 1)[1])
        self.assertEqual(img, self.plate())

    def test_uploads_reused_for_all_variations(self):
        fake = FakeFal()
        out = self.run_fal(fake, count=6)  # flux fill max 4 -> two calls (4 + 2)
        self.assertEqual(len(out), 2)  # the fake returns one image per call
        subs = [fake.jbody(c) for c in fake.find("POST", "queue.fal.run/")]
        self.assertEqual([s["num_images"] for s in subs], [4, 2])
        self.assertEqual([s["seed"] for s in subs], [100, 101])
        self.assertEqual(subs[0]["image_url"], subs[1]["image_url"])
        self.assertEqual(len([c for c in fake.calls if c["url"] == fal.CDN_UPLOAD_URL]), 2)

    def test_data_uri_result_and_single_image_key(self):
        data = _png(8, 6, 3)
        uri = "data:image/png;base64," + base64.b64encode(data).decode()
        fake = FakeFal(result={"image": {"url": uri}})
        self.assertEqual(self.run_fal(fake), [data])

    # ------------------------------------------------------- per-endpoint body

    def body_for(self, model, prompt="x", **kw):
        fake = FakeFal()
        self.run_fal(fake, prompt=prompt, model=model, **kw)
        return fake.jbody(fake.find("POST", "queue.fal.run/")[0]), fake

    def test_empty_prompt_uses_erase_endpoint(self):
        body, fake = self.body_for("", prompt="  ", count=2, seed=5)
        subs = fake.find("POST", "queue.fal.run/")
        self.assertTrue(subs[0]["url"].endswith("/fal-ai/flux-pro/v1/erase"))
        self.assertEqual(len(subs), 2)  # no num_images -> one request per variation
        self.assertEqual(body, {"image_url": body["image_url"], "mask_url": body["mask_url"],
                                "dilate_pixels": 10, "output_format": "png"})
        # status/result use the owner/alias base, not the sub path
        self.assertTrue(any("/fal-ai/flux-pro/requests/" in c["url"] for c in fake.calls))

    def test_ideogram(self):
        b, _ = self.body_for("fal-ai/ideogram/v3/edit", count=8)
        self.assertEqual((b["num_images"], b["rendering_speed"], b["expand_prompt"]),
                         (8, "QUALITY", False))
        self.assertNotIn("output_format", b)

    def test_ideogram_4_5_inverted_mask_and_quality(self):
        b, fake = self.body_for("ideogram/v4.5/edit", count=8, quality="low", prompt="")
        self.assertEqual(b["image_url"], "https://v3b.fal.media/files/b/1.png")
        self.assertEqual(b["mask_url"], "https://v3b.fal.media/files/b/2.png")
        self.assertEqual((b["num_images"], b["quality"], b["seed"]), (8, "low", 100))
        self.assertEqual(b["prompt"], fal.REMOVAL_PROMPT)
        for key in ("output_format", "image_size", "rendering_speed"):
            self.assertNotIn(key, b)
        ups = [c for c in fake.calls if c["url"] == fal.CDN_UPLOAD_URL]
        mask = pngio.read(ups[1]["body"])
        self.assertEqual(set(mask.rows[0]), {0, 255})
        self.assertEqual(mask.rows[0][0], 255)   # keep area -> white ("black edits")
        self.assertEqual(mask.rows[0][3], 0)     # fill area -> black
        b, _ = self.body_for("ideogram/v4.5/edit")
        self.assertNotIn("quality", b)           # auto = provider default (medium)

    def test_gpt_image_2_5_mask_edit(self):
        for model in ("openai/gpt-image-2.5/flare/edit", "openai/gpt-image-2.5/sunburst/edit"):
            b, fake = self.body_for(model, count=3, quality="low")
            self.assertTrue(fake.find("POST", "queue.fal.run/")[0]["url"].endswith(model))
            self.assertEqual(b["image_urls"], ["https://v3b.fal.media/files/b/1.png"])
            self.assertEqual(b["mask_url"], "https://v3b.fal.media/files/b/2.png")
            self.assertEqual((b["num_images"], b["quality"]), (3, "low"))
            self.assertEqual(b["image_size"], {"width": 8, "height": 6})
            ups = [c for c in fake.calls if c["url"] == fal.CDN_UPLOAD_URL]
            self.assertEqual(pngio.read(ups[1]["body"]).rows[0][3], 255)   # white = fill
            b, _ = self.body_for(model)
            self.assertEqual(b["quality"], "high")

    def test_new_reference_edit_endpoints(self):
        b, _ = self.body_for("google/nano-banana-2.1/edit", count=2)
        self.assertEqual(len(b["image_urls"]), 2)       # crop + mask as second image
        self.assertNotIn("mask_url", b)
        self.assertEqual((b["num_images"], b["resolution"], b["aspect_ratio"], b["seed"]),
                         (2, "1K", "auto", 100))
        b, _ = self.body_for("bytedance/seedream/v5/lite/edit")
        self.assertEqual(b["image_size"], "auto_2K")
        self.assertNotIn("output_format", b)
        b, _ = self.body_for("bytedance/seedream/v5/flash/edit")
        self.assertEqual((b["image_size"], b["output_format"]), ("auto_1K", "png"))
        b, _ = self.body_for("xai/grok-imagine-image/v2.0/edit", quality="high")
        self.assertNotIn("quality", b)                  # grok only knows low/medium
        self.assertNotIn("seed", b)

    def test_gpt_image_list_field_and_size(self):
        b, _ = self.body_for("openai/gpt-image-2/edit", quality="medium")
        self.assertIsInstance(b["image_urls"], list)
        self.assertEqual(b["mask_url"], "https://v3b.fal.media/files/b/2.png")
        self.assertEqual(b["image_size"], {"width": 8, "height": 6})
        self.assertEqual(b["quality"], "medium")
        b, _ = self.body_for("openai/gpt-image-2/edit")
        self.assertEqual(b["quality"], "high")

    def test_qwen_inpaint_negative_prompt(self):
        b, _ = self.body_for("fal-ai/qwen-image-edit/inpaint", negative="blur")
        self.assertEqual((b["negative_prompt"], b["strength"]), ("blur", 0.93))

    def test_flux_lora_fill_and_onereward_optional_prompt(self):
        b, _ = self.body_for("fal-ai/flux-lora-fill", prompt="")
        self.assertNotIn("prompt", b)
        self.assertTrue(b["paste_back"] and b["resize_to_original"])
        b, _ = self.body_for("fal-ai/onereward", prompt="")
        self.assertNotIn("prompt", b)
        b, _ = self.body_for("fal-ai/onereward", prompt="cat")
        self.assertEqual(b["prompt"], "cat")

    def test_required_prompt_gets_removal_text(self):
        b, _ = self.body_for("fal-ai/bria/genfill", prompt="")
        self.assertEqual(b["prompt"], fal.REMOVAL_PROMPT)
        self.assertNotIn("output_format", b)

    def test_finegrain_seed_range_and_no_prompt(self):
        body, fake = self.body_for("fal-ai/finegrain-eraser/mask", prompt="ignored",
                                   count=2, seed=998)
        subs = [fake.jbody(c) for c in fake.find("POST", "queue.fal.run/")]
        self.assertEqual([s["seed"] for s in subs], [998, 999])
        self.assertNotIn("prompt", body)
        self.assertEqual(body["mode"], "standard")

    def test_reference_edit_sends_mask_as_second_image(self):
        b, _ = self.body_for("fal-ai/nano-banana-pro/edit", prompt="a door")
        self.assertEqual(len(b["image_urls"]), 2)
        self.assertNotIn("mask_url", b)
        self.assertIn("a door", b["prompt"])
        self.assertIn(base.REF_PROMPT_SUFFIX, b["prompt"])
        self.assertEqual((b["resolution"], b["aspect_ratio"]), ("1K", "auto"))
        b, _ = self.body_for("blackforestlabs/flux-3/edit-image", prompt="")
        self.assertIn(base.REF_EMPTY_PROMPT, b["prompt"])
        self.assertNotIn("num_images", b)
        self.assertNotIn("seed", b)
        b, _ = self.body_for("bytedance/seedream/v5/pro/edit", count=1)
        self.assertEqual((b["image_size"], b["num_images"]), ("auto_1K", 1))

    def test_every_catalogue_entry_builds(self):
        for spec in self.backend.catalogue:
            b, _ = self.body_for(spec.id)
            self.assertTrue("image_url" in b or "image_urls" in b, spec.id)
            self.assertNotIn("webp", json.dumps(b), spec.id)

    def test_model_size(self):
        w, h = self.backend.model_size(1920, 1080, "")
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertAlmostEqual(w / float(h), 16 / 9.0, delta=0.1)
        self.assertLessEqual(w * h, 1.1 * 1024 * 1024)

    # ------------------------------------------------------------------ errors

    def test_401_on_submit(self):
        fake = FakeFal(submit_status=401, submit_body={"detail": "Invalid key"})
        with self.assertRaisesRegex(base.BackendError, "authentication failed.*Invalid key"):
            self.run_fal(fake)

    def test_401_on_token_stops_early(self):
        fake = FakeFal()
        orig = fake.__call__
        def deny(method, url, *a, **k):
            if "storage/auth/token" in url:
                fake.calls.append({"method": method, "url": url, "body": None, "headers": {}})
                return 401, {}, b'{"detail": "bad key"}'
            return orig(method, url, *a, **k)
        with self.assertRaisesRegex(base.BackendError, "authentication failed"):
            self.run_fal(deny)
        self.assertEqual(fake.find("POST", "queue.fal.run/"), [])

    def test_422_detail_list(self):
        fake = FakeFal(result_status=422, result_body={"detail": [
            {"loc": ["body", "image_url"], "msg": "image too large", "type": "image_too_large"},
            {"loc": ["body", "prompt"], "msg": "field required", "type": "missing"}]})
        with self.assertRaisesRegex(base.BackendError,
                                    "image_url: image too large; prompt: field required"):
            self.run_fal(fake)

    def test_content_policy_on_result(self):
        fake = FakeFal(result_status=422, result_body={"detail": [
            {"loc": ["body"], "msg": "flagged", "type": "content_policy_violation"}]})
        with self.assertRaisesRegex(base.BackendError, "content filter"):
            self.run_fal(fake)

    def test_completed_with_error(self):
        fake = FakeFal(status_script=[{"status": "COMPLETED", "error": "boom",
                                       "error_type": "runner_error"}])
        with self.assertRaisesRegex(base.BackendError, "boom"):
            self.run_fal(fake)
        fake = FakeFal(status_script=[{"status": "COMPLETED", "error": "x",
                                       "error_type": "content_policy_violation"}])
        with self.assertRaisesRegex(base.BackendError, "content filter"):
            self.run_fal(fake)

    def test_nsfw_flag_drops_image(self):
        fake = FakeFal(result={"images": [{"url": "https://v3b.fal.media/out/a.png"}],
                               "has_nsfw_concepts": [True]})
        with self.assertRaisesRegex(base.BackendError, "content filter"):
            self.run_fal(fake)

    def test_webp_result_rejected(self):
        fake = FakeFal()
        fake.downloads = {}
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8 "
        fake.result = {"images": [{"url": "https://v3b.fal.media/out/w.png"}]}
        fake.downloads["https://v3b.fal.media/out/w.png"] = webp
        with self.assertRaisesRegex(base.BackendError, "WebP"):
            self.run_fal(fake)

    # ------------------------------------------------------------------ cancel

    def test_cancel_while_polling_puts_cancel_url(self):
        fake = FakeFal(status_script=[{"status": "IN_PROGRESS"}])
        state = {"n": 0}

        def cancelled():
            state["n"] += 1
            return state["n"] > 4  # past uploads and submit, during the first poll round

        with self.assertRaises(base.Cancelled):
            self.run_fal(fake, cancelled=cancelled)
        puts = fake.find("PUT", "/cancel")
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0]["url"].endswith("/fal-ai/flux-pro/requests/r3/cancel"))

    def test_cancel_before_start_makes_no_requests(self):
        fake = FakeFal()
        with self.assertRaises(base.Cancelled):
            self.run_fal(fake, cancelled=lambda: True)
        self.assertEqual(fake.calls, [])

    def test_timeout_cancels(self):
        fake = FakeFal(status_script=[{"status": "IN_QUEUE"}])
        times = iter([0, 0, 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6])
        with mock.patch.object(fal.time, "monotonic", lambda: next(times)):
            with self.assertRaisesRegex(base.BackendError, "too long"):
                self.run_fal(fake)
        self.assertEqual(len(fake.find("PUT", "/cancel")), 1)

    # ------------------------------------------------------- partial results

    def _two_jobs(self, script):
        """FakeFal whose status answers come from ``script[request_id]`` (a list per job)."""
        fake = FakeFal()
        orig = fake.__call__

        def call(method, url, *a, **k):
            if url.split("?")[0].endswith("/status"):
                fake.calls.append({"method": method, "url": url, "body": None, "headers": {}})
                rid = url.split("/requests/")[1].split("/")[0]
                steps = script[rid]
                step = steps.pop(0) if len(steps) > 1 else steps[0]
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, int):
                    return step, {}, b'{"detail": "busy"}'
                return 200, {}, json.dumps(step).encode("utf-8")
            return orig(method, url, *a, **k)
        call.fake = fake
        return call

    def test_one_failed_job_returns_the_others(self):
        # uploads take r1/r2 of the counter (cdn), the two queue requests are r3 and r4
        run = self._two_jobs({"r3": [{"status": "COMPLETED", "error": "boom"}],
                              "r4": [{"status": "IN_PROGRESS"}, {"status": "COMPLETED"}]})
        out = self.run_fal(run, model="fal-ai/flux-pro/v1/erase", prompt="", count=2)
        self.assertEqual(len(out), 1)
        self.assertEqual(run.fake.find("PUT", "/cancel"), [])

    def test_all_jobs_failed_raises_combined(self):
        run = self._two_jobs({"r3": [{"status": "COMPLETED", "error": "boom"}],
                              "r4": [{"status": "COMPLETED", "error": "bang"}]})
        with self.assertRaisesRegex(base.BackendError, "boom.*bang"):
            self.run_fal(run, model="fal-ai/flux-pro/v1/erase", prompt="", count=2)

    def test_transient_poll_errors_are_retried(self):
        net = base.BackendError("Network error talking to queue.fal.run: reset")
        run = self._two_jobs({"r3": [503, 429, net, {"status": "COMPLETED"}],
                              "r4": [{"status": "COMPLETED"}]})
        out = self.run_fal(run, model="fal-ai/flux-pro/v1/erase", prompt="", count=2)
        self.assertEqual(len(out), 2)
        # ... but not forever: that job gives up, the other one still delivers
        run = self._two_jobs({"r3": [503], "r4": [{"status": "COMPLETED"}]})
        out = self.run_fal(run, model="fal-ai/flux-pro/v1/erase", prompt="", count=2)
        self.assertEqual(len(out), 1)

    def test_unexpected_error_cancels_every_running_job(self):
        run = self._two_jobs({"r3": [{"status": "IN_PROGRESS"}, RuntimeError("bug")],
                              "r4": [{"status": "IN_PROGRESS"}]})
        with self.assertRaises(RuntimeError):
            self.run_fal(run, model="fal-ai/flux-pro/v1/erase", prompt="", count=2)
        puts = sorted(c["url"] for c in run.fake.find("PUT", "/cancel"))
        self.assertEqual(len(puts), 2)
        self.assertTrue(puts[0].endswith("/r3/cancel") and puts[1].endswith("/r4/cancel"))

    def test_cancel_cancels_every_running_job(self):
        run = self._two_jobs({"r3": [{"status": "IN_PROGRESS"}], "r4": [{"status": "IN_PROGRESS"}]})
        state = {"n": 0}

        def cancelled():
            state["n"] += 1
            return state["n"] > 6
        with self.assertRaises(base.Cancelled):
            self.run_fal(run, model="fal-ai/flux-pro/v1/erase", prompt="", count=2,
                         cancelled=cancelled)
        self.assertEqual(len(run.fake.find("PUT", "/cancel")), 2)

    def test_timeout_keeps_finished_and_cancels_the_rest(self):
        run = self._two_jobs({"r3": [{"status": "COMPLETED"}], "r4": [{"status": "IN_QUEUE"}]})
        times = iter([0, 0, 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6])
        with mock.patch.object(fal.time, "monotonic", lambda: next(times)):
            out = self.run_fal(run, model="fal-ai/flux-pro/v1/erase", prompt="", count=2)
        self.assertEqual(len(out), 1)
        puts = run.fake.find("PUT", "/cancel")
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0]["url"].endswith("/r4/cancel"))

    def test_failed_second_submit_keeps_the_first(self):
        fake = FakeFal()
        orig = fake.__call__
        submits = []

        def call(method, url, *a, **k):
            if method == "POST" and url.startswith(fal.QUEUE_ROOT + "/"):
                submits.append(url)
                if len(submits) == 2:
                    return 500, {}, b'{"detail": "overloaded"}'
            return orig(method, url, *a, **k)
        out = self.run_fal(call, model="fal-ai/flux-pro/v1/erase", prompt="", count=3)
        self.assertEqual(len(submits), 2)   # no third submit after the failure
        self.assertEqual(len(out), 1)

    def test_one_failed_download_keeps_the_others(self):
        fake = FakeFal(result={"images": [{"url": "https://v3b.fal.media/out/a.png"},
                                          {"url": "https://v3b.fal.media/out/b.png"}]})
        fake.downloads["https://v3b.fal.media/out/a.png"] = b"<html>not an image"
        out = self.run_fal(fake, count=2)
        self.assertEqual(len(out), 1)

    def test_missing_key(self):
        with self.assertRaisesRegex(base.BackendError, "FAL_KEY"):
            self.backend.generate(base.GenerationRequest("a", "b", "c", "p", 8, 6), "")


if __name__ == "__main__":
    unittest.main()
