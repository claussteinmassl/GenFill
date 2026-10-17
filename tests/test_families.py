"""Model family inference and the grouped menu rows."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.join(HERE, "..", "nuke", "python"))

from genfill import backends  # noqa: E402
from genfill.backends import families  # noqa: E402


class FamilyTests(unittest.TestCase):
    def test_magnific_catalogue_maps_by_provider(self):
        cat = backends.get("magnific").catalogue
        expect = [("nano-banana", "Google"), ("gpt", "OpenAI"), ("flux", "Black Forest Labs"),
                  ("seedream", "ByteDance"), ("ideogram", "Ideogram")]
        for needle, fam in expect:
            ids = [m for m in cat if needle in m.id]
            self.assertTrue(ids, needle)
            for m in ids:
                self.assertEqual(families.family(m.id, m.label), fam, m.id)
        for mid in ("retouch-auto", "retouch-pro", "retouch-classic", "retouch-erase"):
            self.assertEqual(families.family(mid), "Magnific", mid)

    def test_curated_entries_are_never_other_except_comfyui_and_mock(self):
        for b in backends.all_backends():
            if b.id in ("comfyui", "mock"):
                continue
            for m in b.catalogue:
                fam = families.family(m.id, m.label, m.extra.get("family", ""))
                if fam == families.OTHER:
                    # Only providers outside the table may stay Other.
                    self.assertTrue(any(w in (m.id + m.label).lower()
                                        for w in ("onereward", "finegrain")), (b.id, m.id))

    def test_fal_and_comfy_examples(self):
        self.assertEqual(families.family("fal-ai/flux-pro/v1/fill"), "Black Forest Labs")
        self.assertEqual(families.family("flux_fill", "FLUX.1 Fill [dev] (local)"),
                         "Black Forest Labs")
        self.assertEqual(families.family("api_bria_eraser", "Bria Eraser (Comfy API)"), "Bria")
        self.assertEqual(families.family("retouch-imagen-nano-banana-2", "Retouch Nano Banana Pro"),
                         "Google")
        self.assertEqual(families.family("whatever"), families.OTHER)

    def test_explicit_family_wins(self):
        self.assertEqual(families.family("flux_fill", "FLUX", explicit="My Studio"), "My Studio")
        self.assertEqual(families.family("flux_fill", "FLUX", explicit="  "), "Black Forest Labs")

    def test_word_boundaries(self):
        self.assertEqual(families.family("swan-lake"), families.OTHER)
        self.assertEqual(families.family("wan-2.1"), "Alibaba")
        self.assertEqual(families.family("seed-edit"), "ByteDance")
        self.assertEqual(families.family("seedling"), families.OTHER)

    def test_grouped_headers_in_first_appearance_order(self):
        entries = [("a", "A", "n", "Google"), ("b", "B", "", "OpenAI"),
                   ("c", "C", "", "Google")]
        self.assertEqual(families.grouped(entries), [
            ("header", "Google"), ("model", "a", "A", "n"), ("model", "c", "C", ""),
            ("header", "OpenAI"), ("model", "b", "B", "")])

    def test_grouped_without_headers_for_one_family(self):
        entries = [("a", "A", "", "Stability"), ("b", "B", "", "Stability")]
        self.assertEqual(families.grouped(entries), [("model", "a", "A", ""),
                                                      ("model", "b", "B", "")])
        self.assertEqual(families.grouped([]), [])


if __name__ == "__main__":
    unittest.main()
