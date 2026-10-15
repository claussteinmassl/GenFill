"""Live model discovery: parsers, cache, merge rules and the generic generate path.

Fixtures in tests/fixtures/discovery are trimmed real responses (2026-10-15) of Magnific
``retouch_models_list`` / ``images_models_list``, Higgsfield ``models_explore`` and fal's
public model API. No network.

Run: python -m unittest tests.test_discovery -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nuke", "python"))
sys.path.insert(0, HERE)

from genfill.backends import base, discovery, fal, higgsfield, magnific  # noqa: E402
from genfill.backends.base import BackendError, GenerationRequest  # noqa: E402
from genfill.backends.oauth import LoginRequired  # noqa: E402

import test_fal  # noqa: E402
import test_higgsfield  # noqa: E402
import test_magnific  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "discovery")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "r") as fh:
        return fh.read() if name.endswith(".txt") else json.load(fh)


class CacheMixin(object):
    """Points the discovery cache at a temp folder and resets the in-memory state."""

    def use_temp_cache(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "genfill", discovery.CACHE_NAME)
        p = mock.patch.object(discovery, "cache_path", lambda: self.cache)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch("genfill.log.info")
        q.start()
        self.addCleanup(q.stop)
        discovery.reset()
        self.addCleanup(discovery.reset)


class FakeBackend(base.Backend):
    id = "fakeprov"
    discoverable = True
    catalogue = (base.ModelSpec("curated-a", "Curated A"),)

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else []
        self.error = error
        self.calls = []

    def discover(self, api_key=""):
        self.calls.append(api_key)
        if self.error:
            raise self.error
        return list(self.result)


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

class ParserTests(unittest.TestCase):
    def test_parse_toon_retouch_list(self):
        data = discovery.parse_toon(fixture("magnific_retouch_models.txt"))
        models = data["models"]
        self.assertEqual(len(models), 6)
        self.assertEqual(models[0]["slug"], "retouch-auto")
        self.assertEqual(models[0]["retouchModes"], ["replace", "erase"])
        self.assertIs(models[0]["beta"], False)
        self.assertEqual(models[0]["expectedGenerationTime"], 10)
        self.assertEqual(models[2]["resolutions"], ["2k", "4k"])
        self.assertIs(models[3]["private"], True)
        self.assertTrue(data["instruction"].startswith("Only offer"))

    def test_helpers(self):
        self.assertEqual(discovery.ratio_list(["auto", "16:9", "2.35:1", "x", "0:1"]),
                         ["16:9", "2.35:1"])
        self.assertEqual(discovery.smallest_resolution(["2k", "1.5k", "4k"]), "1.5k")
        self.assertEqual(discovery.smallest_resolution(["768sq", "1k", "2k"]), "768sq")
        self.assertIsNone(discovery.smallest_resolution(None))
        self.assertEqual(discovery.quality_map(["very_low", "low", "medium", "high"]),
                         {"low": "low", "medium": "medium", "high": "high"})
        self.assertIsNone(discovery.quality_map(["turbo", "mid"]))

    def test_magnific_retouch_specs(self):
        specs = dict((s.id, s) for s in magnific.retouch_specs(
            fixture("magnific_retouch_models.txt")))
        self.assertNotIn("retouch-lora", specs)        # needs a LoRA GenFill doesn't have
        self.assertIsNone(specs["retouch-auto"].erase)
        self.assertTrue(specs["retouch-erase"].extra["erase_only"])
        nbp = specs["retouch-imagen-nano-banana-2"]
        self.assertEqual(nbp.erase, "retouch-auto")     # replace only: erase through Auto
        self.assertEqual(nbp.extra["resolution"], "2k")
        self.assertEqual(nbp.label, "Retouch Nano Banana Pro")
        precise = specs["retouch-precise-nano-banana-pro"]
        self.assertEqual(precise.label, "Retouch Nano Banana Pro Precise (beta)")
        self.assertEqual(precise.note, magnific.BETA_NOTE)
        self.assertEqual(specs["retouch-inpaint-lab-seedream-5-pro"].extra["resolution"], "1.5k")
        self.assertTrue(all(s.kind == "inpaint" and s.max_count == 8 for s in specs.values()))

    def test_magnific_edit_specs(self):
        specs = dict((s.id, s) for s in magnific.edit_specs(
            fixture("magnific_images_models.json")))
        # auto router, extraSettings models and models without an image reference are out
        for gone in ("auto", "cinematic", "magnific-one", "flux-dev", "z-image"):
            self.assertNotIn(gone, specs)
        krea = specs["krea-2"]
        self.assertEqual(krea.kind, "edit")
        self.assertEqual(krea.extra["quality"], {"medium": "medium", "high": "high"})
        self.assertIn("2.35:1", krea.extra["ratios"])
        self.assertNotIn("custom_size", krea.extra)
        grok = specs["grok-imagine-2"]
        self.assertEqual(grok.label, "Grok Imagine 2.0 (beta)")
        self.assertNotIn("auto", grok.extra["ratios"])
        self.assertEqual(specs["flux-2"].extra["resolution"], "1k")

    def test_higgsfield_explore_specs(self):
        specs = dict((s.id, s) for s in higgsfield.explore_specs(
            fixture("higgsfield_models_explore.json")["items"]))
        for gone in ("soul_2", "ms_image", "z_image", "nano_banana_2_shots",
                     "flux_2_pro_outpaint", "recraft_v4_1"):
            self.assertNotIn(gone, specs)
        gpt = specs["gpt_image_2"]
        self.assertEqual(gpt.extra["media_role"], "image")
        self.assertEqual(gpt.extra["resolution"], "1k")
        self.assertEqual(gpt.extra["quality"], {"low": "low", "medium": "medium", "high": "high"})
        ideo = specs["ideogram_4_5"]
        self.assertTrue(ideo.extra["seed"])
        self.assertNotIn("auto", ideo.extra["ratios"])
        nb2 = specs["nano_banana_2"]
        self.assertEqual(nb2.extra["mask"], higgsfield.MASK_ROLE_LIVE)
        self.assertEqual(nb2.kind, "inpaint" if higgsfield.MASK_ROLE_LIVE else "edit")
        self.assertIsNone(specs["grok_image"].extra["quality"])   # std/quality, not low..high

    def test_fal_spec_from_openapi(self):
        specs = dict((m["endpoint_id"], fal.spec_from_openapi(m))
                     for m in fixture("fal_models_expanded.json")["models"])
        ideo = specs["ideogram/v4.5/edit"]           # same mapping as the curated entry
        self.assertEqual(ideo.kind, "inpaint")
        self.assertEqual((ideo.extra["image_field"], ideo.extra["mask_field"]),
                         ("image_url", "mask_url"))
        self.assertTrue(ideo.extra["mask_invert"])   # "Black edits, white preserves"
        self.assertEqual(ideo.extra["quality_field"], "quality")
        self.assertFalse(ideo.extra["output_format"])
        self.assertEqual(ideo.max_count, 8)
        z = specs["fal-ai/z-image/turbo/inpaint"]
        self.assertEqual(z.extra["mask_field"], "mask_image_url")
        self.assertNotIn("mask_invert", z.extra)
        eraser = specs["fal-ai/bria/eraser"]
        self.assertEqual(eraser.kind, "erase")
        self.assertIsNone(eraser.extra["prompt_field"])
        qwen = specs["alibaba/qwen-image-3/edit"]
        self.assertEqual(qwen.kind, "edit")
        self.assertTrue(qwen.extra["mask_as_image"])
        self.assertEqual(qwen.extra["negative_field"], "negative_prompt")
        self.assertTrue(qwen.extra["output_format"])
        self.assertTrue(qwen.label.endswith("(reference edit)"))
        luma = specs["luma/agent/uni-1/v1/edit"]
        self.assertEqual((luma.extra["image_field"], luma.max_count), ("image_url", 1))
        self.assertNotIn("mask_as_image", luma.extra)

    def test_fal_candidates(self):
        listed = dict((m["endpoint_id"], fal._candidate(m))
                      for m in fixture("fal_models_page1.json")["models"])
        self.assertTrue(listed["meta/muse-image/edit"])
        self.assertFalse(listed["fal-ai/bria/background/remove"])
        self.assertFalse(listed["fal-ai/flux-2/lora/edit"])


# --------------------------------------------------------------------------
# cache, merge and refresh
# --------------------------------------------------------------------------

class CacheTests(CacheMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_cache()

    def test_roundtrip_and_file_shape(self):
        spec = base.ModelSpec("new-1", "New One", kind="inpaint", max_count=3, note="n",
                              ratios=("1:1", "16:9"), quality={"low": "low"})
        discovery.store("fakeprov", [spec], now=1760000000)
        with open(self.cache) as fh:
            data = json.load(fh)
        self.assertEqual(data["fakeprov"]["fetched"], "2025-10-09T08:53:20Z")
        self.assertEqual(data["fakeprov"]["models"][0]["id"], "new-1")
        discovery.reset()   # a new session reads the file again
        back = discovery.cached("fakeprov")[0]
        self.assertEqual((back.id, back.label, back.kind, back.max_count, back.note),
                         ("new-1", "New One", "inpaint", 3, "n"))
        self.assertEqual(back.extra["ratios"], ["1:1", "16:9"])
        self.assertTrue(back.discovered)
        self.assertEqual(oct(os.stat(self.cache).st_mode & 0o777), "0o600")

    def test_corrupt_or_missing_cache(self):
        self.assertEqual(discovery.cached("fakeprov"), [])
        os.makedirs(os.path.dirname(self.cache))
        with open(self.cache, "w") as fh:
            fh.write("{not json")
        discovery.reset()
        self.assertEqual(discovery.cached("fakeprov"), [])
        self.assertFalse(discovery.is_fresh("fakeprov"))

    def test_ttl(self):
        discovery.store("fakeprov", [], now=1000.0)
        self.assertTrue(discovery.is_fresh("fakeprov", now=1000.0 + 3600))
        self.assertFalse(discovery.is_fresh("fakeprov", now=1000.0 + 25 * 3600))

    def test_curated_wins_and_new_entries_are_added(self):
        b = FakeBackend()
        discovery.store(b.id, [base.ModelSpec("curated-a", "Renamed by provider", kind="erase"),
                               base.ModelSpec("brand-new", "Brand New")])
        self.assertEqual(b.model_ids(), ["curated-a", "brand-new"])
        self.assertEqual(b.model_spec("curated-a").label, "Curated A")
        self.assertEqual(b.model_spec("curated-a").kind, "inpaint")
        self.assertFalse(b.model_spec("curated-a").discovered)
        self.assertTrue(b.model_spec("brand-new").discovered)
        self.assertEqual(b.model_spec("typed-id").label, "typed-id")   # ad-hoc still works

    def test_duplicate_labels_get_the_id(self):
        merged = discovery.merge([base.ModelSpec("a", "Wan")],
                                 [base.ModelSpec("b", "Wan"), base.ModelSpec("c", "Wan")])
        self.assertEqual([m.label for m in merged], ["Wan", "Wan (b)", "Wan (c)"])

    def test_not_discoverable_ignores_cache(self):
        b = FakeBackend()
        b.discoverable = False
        discovery.store(b.id, [base.ModelSpec("brand-new", "Brand New")])
        self.assertEqual(b.model_ids(), ["curated-a"])
        self.assertIsNone(discovery.refresh(b, force=True))
        self.assertEqual(b.calls, [])

    def test_refresh_respects_ttl_and_force(self):
        b = FakeBackend([base.ModelSpec("brand-new", "Brand New")])
        self.assertEqual(discovery.refresh(b, "key-123"), 1)
        self.assertEqual(b.calls, ["key-123"])
        self.assertIsNone(discovery.refresh(b))            # fresh: no second fetch
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(discovery.refresh(b, force=True), 1)
        self.assertEqual(len(b.calls), 2)
        self.assertTrue(discovery.state(b.id)["ok"])
        with open(self.cache) as fh:
            self.assertNotIn("key-123", fh.read())         # never a secret in the cache

    def test_failure_keeps_cache(self):
        discovery.store("fakeprov", [base.ModelSpec("old", "Old")], now=1.0)
        for error in (LoginRequired("Not signed in to X."), BackendError("Network error"),
                      ValueError("bad answer")):
            b = FakeBackend(error=error)
            self.assertIsNone(discovery.refresh(b, force=True))
            self.assertEqual([m.id for m in discovery.cached("fakeprov")], ["old"])
            st = discovery.state("fakeprov")
            self.assertFalse(st["ok"])
            self.assertIn(str(error), st["message"])

    def test_worker_thread_and_revision(self):
        b = FakeBackend([base.ModelSpec("brand-new", "Brand New")])
        rev = discovery.revision()
        self.assertTrue(discovery.start(b, force=True))
        discovery.wait(b.id, 5)
        self.assertFalse(discovery.busy(b.id))
        self.assertGreater(discovery.revision(), rev)
        self.assertIn("brand-new", b.model_ids())

    def test_maybe_start_once_per_day(self):
        b = FakeBackend(error=BackendError("offline"))
        now = time.time()
        self.assertTrue(discovery.maybe_start(b, now=now))
        discovery.wait(b.id, 5)
        self.assertFalse(discovery.maybe_start(b, now=now + 60))   # failed: not again today
        self.assertTrue(discovery.maybe_start(b, now=now + 25 * 3600))
        discovery.wait(b.id, 5)
        fresh = FakeBackend([])
        fresh.id = "freshprov"
        discovery.store(fresh.id, [], now=now)
        self.assertFalse(discovery.maybe_start(fresh, now=now + 60))  # fresh cache: no fetch
        self.assertEqual(fresh.calls, [])


# --------------------------------------------------------------------------
# backend discover() against fakes
# --------------------------------------------------------------------------

class DiscoverTests(CacheMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_cache()

    def test_magnific_discover_is_non_interactive(self):
        b = magnific.MagnificBackend()
        client = test_magnific.FakeClient({
            "retouch_models_list": lambda a: fixture("magnific_retouch_models.txt"),
            "images_models_list": lambda a: fixture("magnific_images_models.json"),
        })
        seen = {}

        def fake_client(self, api_key="", progress=None, cancelled=None, interactive=True):
            seen["interactive"] = interactive
            return client

        with mock.patch.object(magnific.MagnificBackend, "client", fake_client), \
                mock.patch.object(b, "signed_in", lambda: True), \
                mock.patch.object(b.provider(), "signed_in", lambda: True):
            self.assertGreater(discovery.refresh(b, force=True), 0)
        self.assertIs(seen["interactive"], False)
        self.assertEqual([n for n, _ in client.calls], ["retouch_models_list",
                                                        "images_models_list"])
        self.assertIn("krea-2", b.model_ids())
        self.assertEqual(b.model_spec("flux-2").label, "Flux.2 Pro")   # curated label kept

    def test_magnific_not_signed_in_keeps_cache(self):
        b = magnific.MagnificBackend()
        with mock.patch.object(b.provider(), "signed_in", lambda: False), \
                mock.patch.object(magnific.MagnificBackend, "client",
                                  side_effect=AssertionError("no network when signed out")):
            self.assertIsNone(discovery.refresh(b, force=True))
        self.assertIn("Not signed in", discovery.state(b.id)["message"])
        self.assertEqual(b.model_ids(), [m.id for m in magnific.CATALOGUE])

    def test_higgsfield_discover(self):
        b = higgsfield.HiggsfieldBackend()
        calls = []

        class Client(object):
            def call_tool(self, name, args, timeout=None):
                calls.append((name, args))
                return fixture("higgsfield_models_explore.json")

        with mock.patch.object(b.provider(), "signed_in", lambda: True), \
                mock.patch.object(higgsfield.HiggsfieldBackend, "client",
                                  lambda self, *a, **k: Client()):
            discovery.refresh(b, force=True)
        self.assertEqual(calls, [("models_explore",
                                  {"action": "list", "type": "image", "limit": 100})])
        self.assertIn("cinematic_studio_2_5", b.model_ids())
        self.assertTrue(b.model_spec("cinematic_studio_2_5").discovered)
        self.assertFalse(b.model_spec("gpt_image_2").discovered)

    def test_fal_discover_pages_and_batches(self):
        b = fal.FalBackend()
        pages = [fixture("fal_models_page1.json"), fixture("fal_models_page2.json")]
        expanded = dict((m["endpoint_id"], m)
                        for m in fixture("fal_models_expanded.json")["models"])
        urls = []

        def fake(method, url, body=None, headers=None, timeout=180):
            urls.append((method, url, dict(headers or {})))
            if "expand=openapi-3.0" in url:
                ids = [p.split("=", 1)[1] for p in url.split("?", 1)[1].split("&")
                       if p.startswith("endpoint_id=")]
                assert len(ids) <= 10
                body = {"models": [expanded[i] for i in ids if i in expanded],
                        "has_more": False, "next_cursor": None}
            else:
                body = pages.pop(0)
            return 200, {}, json.dumps(body).encode("utf-8")

        with mock.patch.object(base, "http_request", fake), \
                mock.patch.object(fal.time, "sleep"):
            discovery.refresh(b, "", force=True)
        lists = [u for m, u, h in urls if "expand" not in u]
        self.assertEqual(lists[0], fal.MODELS_API + "?category=image-to-image&limit=100")
        self.assertIn("&cursor=Mg", lists[1])
        self.assertNotIn("Authorization", urls[0][2])          # keyless without a key
        expand = [u for m, u, h in urls if "expand" in u][0]
        self.assertNotIn("ideogram/v4.5/edit", expand)         # curated: not looked up
        self.assertNotIn("background/remove", expand)          # not a fill model
        ids = b.model_ids()
        for new in ("meta/muse-image/edit", "fal-ai/z-image/turbo/inpaint",
                    "fal-ai/bria/eraser"):
            self.assertIn(new, ids)
        self.assertEqual(ids[:len(fal.CATALOGUE)], [m.id for m in fal.CATALOGUE])

    def test_fal_rate_limit_keeps_cache(self):
        b = fal.FalBackend()
        discovery.store(b.id, [base.ModelSpec("x/old", "Old")], now=1.0)
        with mock.patch.object(base, "http_request",
                               lambda *a, **k: (429, {}, b'{"detail": "slow down"}')):
            self.assertIsNone(discovery.refresh(b, force=True))
        self.assertIn("x/old", b.model_ids())
        self.assertIn("rate limited", discovery.state(b.id)["message"])


# --------------------------------------------------------------------------
# discovered models through each backend's generic generate path
# --------------------------------------------------------------------------

class GenerateDiscoveredTests(CacheMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_cache()
        self.img = os.path.join(self.tmp, "i.png")
        with open(self.img, "wb") as fh:
            fh.write(test_fal._png())
        self.msk = os.path.join(self.tmp, "m.png")
        with open(self.msk, "wb") as fh:
            fh.write(test_fal._soft_mask())
        quiet = mock.patch("genfill.log.warning")
        quiet.start()
        self.addCleanup(quiet.stop)

    def request(self, model, **kw):
        args = dict(prompt="a red door", width=1024, height=768, seed=5, count=2, model=model,
                    quality="high")
        args.update(kw)
        return GenerationRequest(self.img, self.msk, self.msk, **args)

    def test_magnific_discovered_edit_and_retouch(self):
        b = magnific.MagnificBackend()
        specs = (magnific.edit_specs(fixture("magnific_images_models.json"))
                 + magnific.retouch_specs(fixture("magnific_retouch_models.txt")))
        discovery.store(b.id, specs)
        b.catalogue = tuple(m for m in magnific.CATALOGUE
                            if m.id != "retouch-precise-nano-banana-pro")
        tools = test_magnific.upload_tools({
            "images_generate": lambda a: {"creations": [{"identifier": "G1"},
                                                        {"identifier": "G2"}]},
            "images_retouch": lambda a: {"creation": {"identifier": "R%d" % len(a)}},
            "creations_wait": lambda a: test_magnific.done(a["identifiers"]),
        })
        client = test_magnific.FakeClient(tools)
        with mock.patch.object(magnific.MagnificBackend, "client", lambda *a, **k: client), \
                mock.patch.object(magnific, "put_bytes", lambda *a, **k: None), \
                mock.patch.object(magnific, "download", lambda url, **k: test_fal._png()):
            out = b.generate(self.request("krea-2"), "")
            self.assertEqual(len(out), 2)
            args = client.named("images_generate")[0]
            self.assertEqual(args["mode"], "krea-2")
            self.assertEqual(args["quality"], "high")
            self.assertIn(args["aspectRatio"], fixture("magnific_images_models.json")["models"][6]
                          ["aspectRatios"])
            self.assertNotIn("width", args)
            b.generate(self.request("retouch-precise-nano-banana-pro", count=1), "")
            rt = client.named("images_retouch")[0]
            self.assertEqual((rt["model"], rt["mode"]),
                             ("retouch-precise-nano-banana-pro", "replace"))
            b.generate(self.request("retouch-precise-nano-banana-pro", count=1, prompt=""), "")
            self.assertEqual(client.named("images_retouch")[1]["mode"], "erase")

    def test_higgsfield_discovered_model(self):
        b = higgsfield.HiggsfieldBackend()
        discovery.store(b.id, higgsfield.explore_specs(
            fixture("higgsfield_models_explore.json")["items"]))
        client = test_higgsfield.FakeClient(wait_script=[test_higgsfield.done("completed",
                                                                              "completed")])
        with mock.patch.object(higgsfield.HiggsfieldBackend, "client", return_value=client), \
                mock.patch.object(higgsfield, "put_bytes"), \
                mock.patch.object(higgsfield, "download", return_value=test_higgsfield.PNG):
            out = b.generate(self.request("cinematic_studio_2_5"), "")
        self.assertEqual(len(out), 2)
        params = client.named("generate_image_batch")[0]["requests"][0]["params"]
        self.assertEqual(params["model"], "cinematic_studio_2_5")
        self.assertEqual(params["medias"], [{"role": "image", "value": "m1"}])
        self.assertEqual(params["resolution"], "1k")
        self.assertNotIn("quality", params)
        self.assertIn(params["aspect_ratio"], b.model_spec("cinematic_studio_2_5").extra["ratios"])

    def test_fal_discovered_inpaint_and_edit(self):
        b = fal.FalBackend()
        discovery.store(b.id, [fal.spec_from_openapi(m)
                               for m in fixture("fal_models_expanded.json")["models"]])
        fake = test_fal.FakeFal()
        req = GenerationRequest(self.img, self.msk, self.msk, "a red door", 8, 6, seed=9,
                                count=2, model="fal-ai/z-image/turbo/inpaint")
        with mock.patch.object(base, "http_request", fake), mock.patch.object(fal.time, "sleep"):
            b.generate(req, "id:secret")
        body = fake.jbody(fake.find("POST", fal.QUEUE_ROOT + "/fal-ai/z-image")[0])
        self.assertEqual(body["prompt"], "a red door")
        self.assertIn("mask_image_url", body)
        self.assertIn("image_url", body)
        self.assertEqual((body["num_images"], body["seed"], body["output_format"]),
                         (2, 9, "png"))

        fake = test_fal.FakeFal()
        req = GenerationRequest(self.img, self.msk, self.msk, "", 8, 6, seed=9, count=1,
                                model="alibaba/qwen-image-3/edit")
        with mock.patch.object(base, "http_request", fake), mock.patch.object(fal.time, "sleep"):
            b.generate(req, "id:secret")
        body = fake.jbody(fake.find("POST", fal.QUEUE_ROOT + "/alibaba/qwen-image-3/edit")[0])
        self.assertEqual(len(body["image_urls"]), 2)          # plate + mask as second image
        self.assertTrue(body["prompt"].startswith(fal.MASK_NOTE))


if __name__ == "__main__":
    unittest.main()
