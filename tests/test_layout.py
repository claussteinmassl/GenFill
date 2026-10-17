"""Region prompt formatters (genfill.backends.layout), pure Python."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill.backends import base, layout  # noqa: E402
from genfill.backends.layout import Box  # noqa: E402

# A 1000 x 500 crop: box 1 in the top-left quarter, box 2 on the right half, lower part.
BOXES = [Box(1, 0, 0, 500, 250, "a red umbrella"), Box(2, 500, 125, 1000, 500, "a black dog")]


class Norm1000Tests(unittest.TestCase):
    def test_y_first_ints(self):
        self.assertEqual(layout.norm1000(BOXES[0], 1000, 500), [0, 0, 500, 500])
        self.assertEqual(layout.norm1000(BOXES[1], 1000, 500), [250, 500, 1000, 1000])

    def test_rounds_and_clamps(self):
        box = Box(1, -20, 1.4, 333.3, 900, "x")
        self.assertEqual(layout.norm1000(box, 1000, 600), [2, 0, 1000, 333])

    def test_never_empty_or_inverted(self):
        self.assertEqual(layout.norm1000(Box(1, 10, 10, 10, 10, "x"), 1000, 1000),
                         [10, 10, 11, 11])
        self.assertEqual(layout.norm1000(Box(1, 1000, 1000, 1000, 1000, "x"), 1000, 1000),
                         [999, 999, 1000, 1000])
        self.assertEqual(layout.norm1000(Box(1, 300, 400, 100, 200, "x"), 1000, 1000),
                         [200, 100, 400, 300])


class Flux3Tests(unittest.TestCase):
    def test_rows_appended_compact(self):
        text = layout.flux3_prompt("In <ref_image_0>, a rainy street", BOXES, 1000, 500)
        self.assertEqual(
            text,
            'In <ref_image_0>, a rainy street\n'
            '[{"id":"obj_1","bbox":[0,0,500,500],"desc":"a red umbrella"},'
            '{"id":"obj_2","bbox":[250,500,1000,1000],"desc":"a black dog"}]')

    def test_empty_prompt_is_just_the_rows(self):
        self.assertTrue(layout.flux3_prompt("", BOXES, 1000, 500).startswith('[{"id":"obj_1"'))

    def test_ids_follow_box_numbers(self):
        text = layout.flux3_prompt("p", [Box(3, 0, 0, 10, 10, "c")], 10, 10)
        self.assertEqual(json.loads(text.split("\n", 1)[1])[0]["id"], "obj_3")

    def test_fields_fractions_x_first(self):
        fields = layout.flux3_fields(BOXES, 1000, 500)
        self.assertEqual(fields, [
            {"label": "obj_1", "description": "a red umbrella",
             "box": {"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5}},
            {"label": "obj_2", "description": "a black dog",
             "box": {"x_min": 0.5, "y_min": 0.25, "x_max": 1.0, "y_max": 1.0}},
        ])
        self.assertEqual(list(fields[0]["box"]), ["x_min", "y_min", "x_max", "y_max"])

    def test_fields_clamped_and_rounded(self):
        box = layout.flux3_fields([Box(1, -5, 0, 1 / 3.0 * 900, 1200, "x")], 900, 1000)[0]["box"]
        self.assertEqual(box, {"x_min": 0.0, "y_min": 0.0, "x_max": 0.3333, "y_max": 1.0})


class IdeogramTests(unittest.TestCase):
    def test_json_prompt(self):
        text = layout.ideogram_prompt("A street with <obj_1> and <obj_2>, at dusk", BOXES,
                                      1000, 500)
        data = json.loads(text)
        self.assertEqual(list(data), ["high_level_description", "compositional_deconstruction"])
        self.assertEqual(data["high_level_description"],
                         "A street with (a red umbrella) and (a black dog), at dusk")
        self.assertEqual(data["compositional_deconstruction"], {
            "background": "Keep the existing image outside the boxes unchanged.",
            "elements": [
                {"type": "obj", "bbox": [0, 0, 500, 500], "desc": "a red umbrella"},
                {"type": "obj", "bbox": [250, 500, 1000, 1000], "desc": "a black dog"},
            ]})

    def test_unicode_kept_and_unknown_tag_dropped(self):
        text = layout.ideogram_prompt(u"Café sign <obj_7>", [Box(1, 0, 0, 5, 5, u"ü")],
                                      10, 10)
        self.assertIn(u"Café", text)
        self.assertIn(u'"desc":"ü"', text)
        self.assertEqual(json.loads(text)["high_level_description"], u"Café sign")

    def test_empty_prompt_describes_the_boxes(self):
        data = json.loads(layout.ideogram_prompt("", BOXES, 1000, 500))
        self.assertEqual(data["high_level_description"], "a red umbrella; a black dog")


class ApplyTests(unittest.TestCase):
    def test_no_boxes_leaves_prompt(self):
        for style in layout.STYLES + (None,):
            self.assertEqual(layout.apply(style, "keep me", [], 100, 100), ("keep me", {}))
            self.assertEqual(layout.apply(style, "keep me", None, 100, 100), ("keep me", {}))

    def test_no_style_leaves_prompt(self):
        self.assertEqual(layout.apply(None, "p", BOXES, 1000, 500), ("p", {}))
        self.assertEqual(layout.apply("unknown", "p", BOXES, 1000, 500), ("p", {}))

    def test_styles(self):
        self.assertEqual(layout.apply("flux3", "p", BOXES, 1000, 500),
                         (layout.flux3_prompt("p", BOXES, 1000, 500), {}))
        self.assertEqual(layout.apply("flux3_fields", "p <obj_1>", BOXES, 1000, 500),
                         ("p <obj_1>", {"bounding_boxes": layout.flux3_fields(BOXES, 1000, 500)}))
        self.assertEqual(layout.apply("ideogram", "p", BOXES, 1000, 500),
                         (layout.ideogram_prompt("p", BOXES, 1000, 500), {}))

    def test_boxes_without_text_are_skipped(self):
        boxes = [Box(1, 0, 0, 5, 5, "  "), Box(2, 0, 0, 5, 5, "b")]
        rows = json.loads(layout.flux3_prompt("p", boxes, 10, 10).split("\n")[1])
        self.assertEqual([r["id"] for r in rows], ["obj_2"])
        self.assertEqual(layout.apply("flux3", "p", boxes[:1], 10, 10), ("p", {}))

    def test_sends(self):
        self.assertTrue(layout.sends("ideogram", BOXES))
        self.assertFalse(layout.sends(None, BOXES))
        self.assertFalse(layout.sends("flux3", [Box(1, 0, 0, 1, 1, "")]))

    def test_infer_style(self):
        cases = {"flux-3-image": "flux3", "flux_3_image": "flux3", "comfy/flux3-klein": "flux3",
                 "ideogram/v4.5/edit": "ideogram", "retouch-ideogram-4-5": "ideogram",
                 "ideogram_4_5": "ideogram", "Ideogram-V4": "ideogram",
                 "fal-ai/ideogram/v3/edit": None, "fal-ai/flux-pro/v1/fill": None,
                 "flux-2": None}
        for model, style in cases.items():
            self.assertEqual(layout.infer_style(model), style, model)


class RequestAndBackendTests(unittest.TestCase):
    def test_request_boxes_default_empty(self):
        req = base.GenerationRequest("i", "m", "a", "p", 10, 10)
        self.assertEqual(req.boxes, [])
        req = base.GenerationRequest("i", "m", "a", "p", 10, 10, boxes=tuple(BOXES))
        self.assertEqual(req.boxes, BOXES)

    def test_layout_style_from_extra_or_discovered_id(self):
        class B(base.Backend):
            catalogue = (base.ModelSpec("flux-3-x", "F"),
                         base.ModelSpec("own", "O", layout="ideogram"),
                         base.ModelSpec("flux-3-off", "N", layout=None))
        b = B()
        self.assertIsNone(b.layout_style("flux-3-x"))       # curated: only explicit layouts
        self.assertEqual(b.layout_style("own"), "ideogram")
        self.assertIsNone(b.layout_style("flux-3-off"))
        self.assertIsNone(b.layout_style("typed-flux-3"))   # ad-hoc ids are not guessed
        found = base.ModelSpec("vendor/flux-3/edit", "D")
        found.discovered = True
        b.all_models = lambda: list(B.catalogue) + [found]
        self.assertEqual(b.layout_style("vendor/flux-3/edit"), "flux3")


if __name__ == "__main__":
    unittest.main()
