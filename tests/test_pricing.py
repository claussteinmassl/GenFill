"""Cost preview and balance: formatting, static tables, backend quotes and the cache.

Run: python -m unittest tests.test_pricing -v
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import pricing  # noqa: E402
from genfill.backends import comfy_cloud, fal, mock as mock_backend, openai_images  # noqa: E402
from genfill.backends import stability  # noqa: E402
from genfill.backends.base import BackendError, GenerationRequest  # noqa: E402
from genfill.backends.oauth import LoginRequired  # noqa: E402

COMFY_ESTIMATE = """2 paid API node(s):
  FluxProFillNode (node 3): 11 credits
  FluxProFillNode (node 5): 11 credits

Estimated total: 21 credits

Estimate only - nothing was submitted and no credits were spent."""


def req(**kw):
    args = dict(image_path=None, mask_path=None, mask_alpha_path=None, prompt="a car",
                width=1024, height=1024, count=1, model="", quality="auto")
    args.update(kw)
    return GenerationRequest(**args)


class FormatTests(unittest.TestCase):
    def test_quote_text(self):
        self.assertEqual(pricing.Quote(30, exact=True).text(), "30 credits")
        self.assertEqual(pricing.Quote(1, exact=True).text(), "1 credit")
        self.assertEqual(pricing.Quote(30).text(), u"≈ 30 credits")
        self.assertEqual(pricing.Quote(0.12, pricing.USD, count=3).text(),
                         u"≈ $0.12 for 3")
        self.assertEqual(pricing.Quote(0.006, pricing.USD).text(), u"≈ $0.006")
        self.assertEqual(pricing.Quote(0, pricing.FREE, note="free (your GPU)").text(),
                         "free (your GPU)")
        self.assertEqual(pricing.Quote(None).text(), "cost unknown")
        self.assertEqual(pricing.Quote(None, note="billed in Comfy credits").text(),
                         "billed in Comfy credits")
        self.assertEqual(pricing.Quote(1234567.5, exact=True).text(), "1,234,567.5 credits")

    def test_balance_text(self):
        self.assertEqual(pricing.Balance(243317, "Premium+").text(),
                         "balance 243,317 (Premium+)")
        self.assertEqual(pricing.Balance(143.97).text(), "balance 143.97")


class StaticTests(unittest.TestCase):
    def test_openai_token_estimate(self):
        q = pricing.openai_estimate("gpt-image-2", 1024, 1024, "high", 1)
        # 4160 output tokens at $30/M plus two 765-token inputs at $8/M
        self.assertAlmostEqual(q.amount, (2 * 765 * 8 + 4160 * 30) / 1e6)
        self.assertFalse(q.exact)
        three = pricing.openai_estimate("gpt-image-2", 1024, 1024, "high", 3)
        self.assertLess(three.amount, 3 * q.amount)        # input billed once
        low = pricing.openai_estimate("gpt-image-1-mini", 1024, 1024, "low", 1)
        self.assertLess(low.amount, 0.01)
        bigger = pricing.openai_estimate("gpt-image-2", 2048, 2048, "high", 1)
        self.assertGreater(bigger.amount, 3 * q.amount)
        self.assertIsNone(pricing.openai_estimate("chatgpt-image-latest", 1024, 1024, "high", 1))

    def test_openai_backend(self):
        b = openai_images.OpenAIBackend()
        auto = b.estimate(req(model="gpt-image-2", quality="auto"))
        high = b.estimate(req(model="gpt-image-2", quality="high"))
        self.assertEqual(auto.amount, high.amount)          # auto estimated as high
        fid = b.estimate(req(model="gpt-image-1", quality="high"))
        nofid = pricing.openai_estimate("gpt-image-1", 1024, 1024, "high", 1)
        self.assertGreater(fid.amount, nofid.amount)
        self.assertIsNone(b.balance("sk-x"))

    def test_fal_static(self):
        self.assertAlmostEqual(pricing.fal_static("fal-ai/nano-banana-pro/edit", 1024, 1024,
                                                  "auto", 2).amount, 0.30)
        self.assertAlmostEqual(pricing.fal_static("fal-ai/flux-pro/v1/erase", 2000, 1500,
                                                  "auto", 1).amount, 0.03 + 2 * 0.004)
        self.assertAlmostEqual(pricing.fal_static("ideogram/v4.5/edit", 1024, 1024,
                                                  "medium", 1).amount, 0.06)
        self.assertIsNone(pricing.fal_static("ideogram/v4.5/edit", 1024, 1024, "auto", 1))
        self.assertIsNone(pricing.fal_static("fal-ai/flux-pro/v1/fill", 1024, 1024, "auto", 1))
        klein = "blackforestlabs/flux-3/klein/edit-image"
        for (w, h), price in (((1024, 1024), 0.009), ((2048, 2048), 0.014),
                              ((4096, 4096), 0.036)):
            quote = pricing.fal_static(klein, w, h, "auto", 2)
            self.assertAlmostEqual(quote.amount, 2 * price, msg=w)
            self.assertFalse(quote.exact)
        gpt = pricing.fal_static("openai/gpt-image-2/edit", 1024, 1024, "high", 1)
        self.assertEqual(gpt.unit, pricing.USD)

    def test_fal_pricing_shapes(self):
        info = {"prices": [{"endpoint_id": "fal-ai/x", "unit_price": 0.05, "unit": "image",
                            "currency": "USD"}], "has_more": False}
        self.assertAlmostEqual(pricing.fal_pricing(info, "fal-ai/x", 1024, 1024, 3).amount,
                               0.15)
        mp = [{"endpoint_id": "fal-ai/x", "price": 0.02, "unit": "megapixels"}]
        self.assertAlmostEqual(pricing.fal_pricing(mp, "fal-ai/x", 2000, 1000, 1).amount, 0.04)
        self.assertIsNone(pricing.fal_pricing({"prices": [{"unit_price": 1, "unit": "second"}]},
                                              "fal-ai/x", 1, 1, 1))
        self.assertIsNone(pricing.fal_pricing({"detail": "nope"}, "fal-ai/x", 1, 1, 1))
        self.assertIsNone(pricing.fal_pricing({"prices": [{"endpoint_id": "other",
                                                           "unit_price": 1, "unit": "image"}]},
                                              "fal-ai/x", 1, 1, 1))


class BackendQuoteTests(unittest.TestCase):
    def test_fal_uses_pricing_api_with_key(self):
        b = fal.FalBackend()
        body = json.dumps({"prices": [{"endpoint_id": "fal-ai/flux-pro/v1/fill",
                                       "unit_price": 0.05, "unit": "megapixels",
                                       "currency": "USD"}]}).encode()
        with mock.patch.object(fal.base, "http_request", return_value=(200, {}, body)) as get:
            q = b.estimate(req(count=2, width=1536, height=1024), "KEY")
        url = get.call_args[0][1]
        self.assertTrue(url.startswith("https://api.fal.ai/v1/models/pricing?endpoint_id="))
        self.assertIn("fal-ai/flux-pro/v1/fill", url)
        self.assertEqual(get.call_args[0][0], "GET")
        self.assertAlmostEqual(q.amount, 2 * 2 * 0.05)

    def test_fal_falls_back_to_table(self):
        b = fal.FalBackend()
        with mock.patch.object(fal.base, "http_request", return_value=(401, {}, b"{}")):
            q = b.estimate(req(model="fal-ai/nano-banana-pro/edit", count=2), "KEY")
        self.assertAlmostEqual(q.amount, 0.30)
        with mock.patch.object(fal.base, "http_request") as get:
            self.assertIsNone(b.estimate(req(), ""))     # no key: unpriced default model
            erase = b.estimate(req(prompt=""), "")        # empty prompt -> FLUX Erase
        get.assert_not_called()
        self.assertAlmostEqual(erase.amount, 0.03)
        self.assertIsNone(b.balance("KEY"))

    def test_stability(self):
        b = stability.StabilityBackend()
        self.assertEqual(b.estimate(req(count=3)).amount, 15)
        with mock.patch.object(stability, "http_request",
                               return_value=(200, {}, b'{"credits": 42.5}')) as get:
            bal = b.balance("sk-x")
        self.assertEqual(get.call_args[0][1], "https://api.stability.ai/v1/user/balance")
        self.assertEqual(bal.text(), "balance 42.5")
        for answer in ((401, {}, b'{"credits": 1}'), (200, {}, b"<html>"),
                       (200, {}, b'{"balance": 3}')):
            with mock.patch.object(stability, "http_request", return_value=answer):
                self.assertIsNone(b.balance("sk-x"))
        self.assertIsNone(b.balance(""))

    def test_mock_is_free(self):
        q = mock_backend.MockBackend().estimate(req(count=4))
        self.assertEqual(q.unit, pricing.FREE)

    def test_comfy_cloud_graph_estimate(self):
        b = comfy_cloud.ComfyCloudBackend()
        client = mock.Mock()
        client.call_tool.return_value = COMFY_ESTIMATE
        with mock.patch.object(comfy_cloud.ComfyCloudBackend, "quiet_client",
                               return_value=client):
            q = b.estimate(req(model="flux-pro-fill", count=2), "")
        name, args = client.call_tool.call_args[0]
        self.assertEqual(name, "estimate_credits")
        types = sorted(n["class_type"] for n in args["workflow"].values())
        self.assertEqual(types.count("FluxProFillNode"), 2)
        self.assertEqual((q.amount, q.exact, q.count), (21, False, 2))

    def test_comfy_cloud_erase_and_partner(self):
        b = comfy_cloud.ComfyCloudBackend()
        client = mock.Mock()
        client.call_tool.return_value = "Estimated total: 6 credits"
        with mock.patch.object(comfy_cloud.ComfyCloudBackend, "quiet_client",
                               return_value=client):
            q = b.estimate(req(model="bria-genfill", prompt="", count=3), "")
            partner = b.estimate(req(model="nano-banana-2", count=2), "")
            masked_partner = b.estimate(req(model="gpt-image-2.5-flare"), "")
        graph = client.call_tool.call_args_list[0][0][1]["workflow"]
        self.assertEqual([n["class_type"] for n in graph.values()].count("BriaEraser"), 1)
        self.assertEqual(q.count, 1)                 # deterministic eraser: one image
        self.assertEqual(client.call_tool.call_count, 1)
        self.assertEqual(partner.text(), "billed in Comfy credits")
        self.assertIsNone(masked_partner.amount)
        self.assertIsNone(b.balance(""))

    def test_parse_estimate(self):
        self.assertEqual(comfy_cloud.parse_estimate(COMFY_ESTIMATE), 21)
        self.assertEqual(comfy_cloud.parse_estimate("Estimated total: 1,250.5 credits"),
                         1250.5)
        self.assertIsNone(comfy_cloud.parse_estimate("0 paid API nodes"))


class FakeBackend(object):
    id = "fake"

    def __init__(self, quote=None, balance=None, error=None):
        self.quote, self.bal, self.error = quote, balance, error
        self.estimates = []
        self.balances = 0

    def estimate(self, request, api_key=""):
        self.estimates.append(request)
        if self.error:
            raise self.error
        return self.quote

    def balance(self, api_key=""):
        self.balances += 1
        return self.bal


class CacheTests(unittest.TestCase):
    def setUp(self):
        pricing.reset()
        self.addCleanup(pricing.reset)

    def snap(self, backend, **kw):
        args = dict(backend=backend, model="m", quality="auto", resolution="2K", count=3,
                    width=2048, height=1152, prompt="a car", api_key="")
        args.update(kw)
        return pricing.snapshot(**args)

    def test_quote_and_balance_line(self):
        b = FakeBackend(pricing.Quote(30, count=3), pricing.Balance(243317, "Premium+"))
        snap = self.snap(b)
        self.assertEqual(pricing.lookup(snap), ("", ""))
        pricing.request(snap, threaded=False)
        text, tip = pricing.lookup(snap)
        self.assertEqual(text, u"≈ 30 credits for 3 · balance 243,317 (Premium+)")
        req0 = b.estimates[0]
        self.assertEqual((req0.width, req0.height, req0.count, req0.resolution),
                         (2048, 1152, 3, "2K"))
        self.assertIsNone(req0.image_path)

    def test_cache_ttls(self):
        b = FakeBackend(pricing.Quote(1), None)
        snap = self.snap(b)
        pricing.request(snap, now=1000, threaded=False)
        when = pricing._quotes[pricing.key(snap)]["when"]
        self.assertFalse(pricing.request(snap, now=when + 10, threaded=False))
        self.assertTrue(pricing.request(snap, now=when + pricing.BALANCE_TTL + 1,
                                        threaded=False))         # balance only
        self.assertEqual(len(b.estimates), 1)
        pricing.request(snap, now=when + pricing.QUOTE_TTL + 1, threaded=False)
        self.assertEqual(len(b.estimates), 2)
        # The prompt text is not part of the key; an empty prompt is.
        self.assertEqual(pricing.key(snap), pricing.key(self.snap(b, prompt="a dog")))
        self.assertNotEqual(pricing.key(snap), pricing.key(self.snap(b, prompt=" ")))
        self.assertNotEqual(pricing.key(snap), pricing.key(self.snap(b, count=4)))
        self.assertNotEqual(pricing.key(snap), pricing.key(self.snap(b, boxes=True)))

    def test_boxes_reach_the_quote_request(self):
        b = FakeBackend(pricing.Quote(1), None)
        pricing.request(self.snap(b, prompt=""), threaded=False)
        pricing.request(self.snap(b, prompt="", boxes=True), threaded=False)
        self.assertEqual(b.estimates[0].boxes, [])
        (box,) = b.estimates[1].boxes
        self.assertTrue(box.prompt)
        self.assertEqual((box.x0, box.y0, box.x1, box.y1), (0, 0, 2048, 1152))

    def test_forget_balance(self):
        b = FakeBackend(pricing.Quote(1), pricing.Balance(5))
        snap = self.snap(b)
        pricing.request(snap, threaded=False)
        pricing.forget_balance("fake")
        pricing.request(snap, threaded=False)
        self.assertEqual((len(b.estimates), b.balances), (1, 2))

    def test_auth_state_is_part_of_the_key(self):
        class Account(FakeBackend):
            signed = False

            def signed_in(self):
                return self.signed
        b = Account(error=LoginRequired("Not signed in"))
        snap = self.snap(b)
        self.assertEqual(snap["auth"], (False, False))
        pricing.request(snap, threaded=False)
        self.assertEqual(pricing.lookup(snap)[0], "sign in to see cost")
        b.signed, b.error, b.quote = True, None, pricing.Quote(5, exact=True, count=3)
        b.bal = pricing.Balance(9)
        signed = self.snap(b)
        self.assertNotEqual(pricing.key(signed), pricing.key(snap))
        self.assertEqual(pricing.lookup(signed), ("", ""))   # nothing cached for the new state
        pricing.request(signed, threaded=False)
        self.assertEqual(pricing.lookup(signed)[0], "5 credits for 3 \u00b7 balance 9")
        keyed = self.snap(b, api_key="k")
        self.assertEqual(keyed["auth"], (True, True))
        self.assertNotEqual(pricing.key(keyed), pricing.key(signed))

    def test_error_retried_by_repeated_requests(self):
        b = FakeBackend(error=BackendError("down"))
        snap = self.snap(b)
        pricing.request(snap, threaded=False)
        when = pricing._quotes[pricing.key(snap)]["when"]
        pricing.request(snap, now=when + 5, threaded=False)
        self.assertEqual(len(b.estimates), 1)
        b.error, b.quote = None, pricing.Quote(3, exact=True, count=3)
        pricing.request(snap, now=when + pricing.ERROR_TTL + 1, threaded=False)
        self.assertEqual(pricing.lookup(snap)[0], "3 credits for 3")

    def test_stale_balance_discarded_after_forget(self):
        import threading
        started, gate = threading.Event(), threading.Event()

        class Slow(FakeBackend):
            def balance(self, api_key=""):
                self.balances += 1
                if self.balances == 1:
                    started.set()
                    gate.wait(5)
                    return pricing.Balance(100)      # read before the generation
                return pricing.Balance(70)
        b = Slow(pricing.Quote(1))
        snap = self.snap(b)
        old = threading.Thread(target=pricing.request, args=(snap,),
                               kwargs={"threaded": False})
        old.start()
        started.wait(5)
        pricing.forget_balance("fake")              # a generation finished meanwhile
        pricing.request(snap, threaded=False)       # new fetch starts right away
        self.assertIn("balance 70", pricing.lookup(snap)[0])
        gate.set()
        old.join(5)
        self.assertIn("balance 70", pricing.lookup(snap)[0])   # stale 100 dropped
        self.assertNotIn("fake", pricing._balance_running)

    def test_errors(self):
        snap = self.snap(FakeBackend(error=LoginRequired("Not signed in")))
        pricing.request(snap, threaded=False)
        self.assertEqual(pricing.lookup(snap)[0], "sign in to see cost")
        snap = self.snap(FakeBackend(error=BackendError("boom"), balance=pricing.Balance(7)))
        pricing.reset()
        pricing.request(snap, threaded=False)
        self.assertEqual(pricing.lookup(snap), (u"cost unknown · balance 7", "boom"))
        snap = self.snap(FakeBackend(quote=None))
        pricing.reset()
        pricing.request(snap, threaded=False)
        self.assertEqual(pricing.lookup(snap)[0], "cost unknown")

    def test_balance_error_hidden(self):
        class Broken(FakeBackend):
            def balance(self, api_key=""):
                raise BackendError("no")
        snap = self.snap(Broken(pricing.Quote(2, exact=True, count=3)))
        pricing.request(snap, threaded=False)
        self.assertEqual(pricing.lookup(snap)[0], "2 credits for 3")

    def test_threaded_request_never_blocks(self):
        import threading
        gate = threading.Event()

        class Slow(FakeBackend):
            def estimate(self, request, api_key=""):
                gate.wait(5)
                return pricing.Quote(9, exact=True)
        snap = self.snap(Slow(), count=1)
        self.assertTrue(pricing.request(snap))
        self.assertEqual(pricing.lookup(snap)[0], u"estimating cost…")
        self.assertFalse(pricing.request(snap))       # already running: no second thread
        gate.set()
        for t in threading.enumerate():
            if t.name.startswith("GenFill cost"):
                t.join(5)
        self.assertEqual(pricing.lookup(snap)[0], "9 credits")


if __name__ == "__main__":
    unittest.main()
