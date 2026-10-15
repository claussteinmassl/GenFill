"""Colour pipeline: encoding choice, ACEScct maths, and the per-variation
encode/decode wiring inside the node (with the fake nuke)."""
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.dirname(__file__))
import fake_nuke  # noqa: E402

nuke = fake_nuke.install()

from genfill import colorspace as cs, engine, node as gfnode  # noqa: E402


class PureColorTests(unittest.TestCase):
    def test_acescct_roundtrip_and_range(self):
        for v in (-0.005, 0.0, 0.004, 0.18, 1.0, 7.5, 150.0):
            self.assertAlmostEqual(cs.acescct_decode(cs.acescct_encode(v)), v, places=6)
        self.assertAlmostEqual(cs.acescct_decode(1.0), 222.86, places=1)   # 0..1 keeps ~222
        self.assertLess(cs.acescct_encode(16.0), 1.0)

    def test_parse_and_classify(self):
        self.assertEqual(cs.parse_colorspace_value("default (matte_paint)"), "matte_paint")
        self.assertEqual(cs.parse_colorspace_value("Output/Output - Rec.709"), "Output - Rec.709")
        self.assertEqual(cs.classify("ACES - ACEScg", ".exr"), "linear")
        self.assertEqual(cs.classify("scene_linear"), "linear")
        self.assertEqual(cs.classify("matte_paint", ".png"), "display")
        self.assertEqual(cs.classify("ARRI LogC4"), "display")          # log plates are fine as-is
        self.assertEqual(cs.classify("ACES - ACEScct"), "display")
        self.assertEqual(cs.classify("", ".exr"), "linear")

    def test_pick_display(self):
        names = ["ACES/ACES - ACEScg", "Output/Output - sRGB", "Output/Output - Rec.709"]
        self.assertEqual(cs.pick_display_colorspace(names), "Output - Rec.709")
        self.assertEqual(cs.pick_display_colorspace(["sRGB - Display", "ACEScg"]), "sRGB - Display")
        self.assertIsNone(cs.pick_display_colorspace(["linear", "ACEScg"]))

    def test_resolve_modes(self):
        d = "Output - Rec.709"
        png = cs.resolve(cs.MODE_AUTO, "default (matte_paint)", ".png", display=d)
        self.assertEqual((png["mode"], png["colorspace"]), ("ocio", "matte_paint"))
        exr = cs.resolve(cs.MODE_AUTO, "ACES - ACEScg", ".exr", display=d)
        self.assertEqual((exr["mode"], exr["colorspace"]), ("ocio", d))
        self.assertEqual(cs.resolve(cs.MODE_AUTO, "ACES - ACEScg", ".exr", display=None)["mode"], "log")
        self.assertEqual(cs.resolve(cs.MODE_DISPLAY, "matte_paint", ".png", display=d)["colorspace"], d)
        self.assertEqual(cs.resolve(cs.MODE_LOG, "matte_paint", ".png", display=d)["mode"], "log")
        self.assertEqual(cs.resolve(cs.MODE_INPUT, "ARRI LogC4", ".mov")["colorspace"], "ARRI LogC4")
        self.assertEqual(cs.resolve(cs.MODE_CUSTOM, custom="Output - P3-D65")["colorspace"],
                         "Output - P3-D65")
        self.assertEqual(cs.preview_kind({"mode": "log"}), "log")
        self.assertEqual(cs.preview_kind(png), "display")


def _join():
    for t in threading.enumerate():
        if t.name.startswith("GenFill-"):
            t.join(30)


class NodeColorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = os.path.join(self.tmp, "shot.nk")
        self.plate = nuke.nodes.Read(name="Plate")
        self.g = gfnode.create()
        fake_nuke.add_shape(self.g.node(gfnode.ROTO))
        self.g.setInput(0, self.plate)
        self.g["gf_backend"].setValue([l for l in gfnode.backend_labels() if "Offline" in l][0])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _gen(self):
        self.assertTrue(engine.generate(self.g, "x", blocking=True), engine.status(self.g))
        return engine.get_state(self.g)["generations"][-1]

    def test_png_plate_round_trips_in_its_own_colorspace(self):
        self.plate["colorspace"].setValue("default (matte_paint)")
        self.plate["file"].setValue("C:/shots/plate.png")
        gen = self._gen()
        self.assertEqual(gen["transfer"]["mode"], "ocio")
        self.assertEqual(gen["transfer"]["colorspace"], "matte_paint")
        g = self.g
        self.assertEqual(g.node(gfnode.TO_MODEL)["which"].value(), cs.SWITCH_INDEX["ocio"])
        self.assertEqual(g.node(gfnode.TO_MODEL_OCIO)["in_colorspace"].value(), "scene_linear")
        self.assertEqual(g.node(gfnode.TO_MODEL_OCIO)["out_colorspace"].value(), "matte_paint")
        # decode is the exact inverse, and the result is read raw
        self.assertEqual(g.node(gfnode.FROM_MODEL)["which"].value(), cs.SWITCH_INDEX["ocio"])
        self.assertEqual(g.node(gfnode.FROM_MODEL_OCIO)["in_colorspace"].value(), "matte_paint")
        self.assertEqual(g.node(gfnode.FROM_MODEL_OCIO)["out_colorspace"].value(), "scene_linear")
        self.assertTrue(g.node(gfnode.READ_RESULT)["raw"].value())
        self.assertTrue(g.node(gfnode.WRITE_SRC)["raw"].value())

    def test_exr_plate_uses_display_transform(self):
        self.plate["colorspace"].setValue("ACES - ACEScg")
        self.plate["file"].setValue("C:/shots/plate.####.exr")
        self.g["gf_display"].setValue("Output - Rec.709")
        gen = self._gen()
        self.assertEqual(gen["transfer"]["colorspace"], "Output - Rec.709")
        self.assertEqual(self.g.node(gfnode.FROM_MODEL_OCIO)["in_colorspace"].value(),
                         "Output - Rec.709")

    def test_log_mode_and_per_variation_decode(self):
        self.g["gf_color_mode"].setValue(cs.MODE_LOG)
        log_gen = self._gen()
        self.assertEqual(log_gen["transfer"]["mode"], "log")
        self.g["gf_color_mode"].setValue(cs.MODE_CUSTOM)
        self.g["gf_transfer"].setValue("Output - sRGB")
        self._gen()
        self.assertEqual(self.g.node(gfnode.FROM_MODEL)["which"].value(), cs.SWITCH_INDEX["ocio"])
        engine.select(self.g, 0, 0)                     # back to the log variation
        self.assertEqual(self.g.node(gfnode.FROM_MODEL)["which"].value(), cs.SWITCH_INDEX["log"])
        self.assertEqual(self.g.node(gfnode.FROM_MODEL_LOG)["expr0"].value(), cs.DECODE_EXPRS[0])

    def test_legacy_generation_decodes_the_old_way(self):
        gen = self._gen()
        st = engine.get_state(self.g)
        del st["generations"][0]["transfer"]              # as saved by GenFill <= 0.5
        self.g["gf_state"].setValue(engine.encode_state(st))
        engine.refresh_result(self.g)
        self.assertFalse(self.g.node(gfnode.READ_RESULT)["raw"].value())
        self.assertEqual(self.g.node(gfnode.FROM_MODEL)["which"].value(), cs.SWITCH_INDEX["none"])

    def test_source_goes_out_as_8bit_png(self):
        fake_nuke.EXECUTED[:] = []
        gen = self._gen()
        names = [e[0] for e in fake_nuke.EXECUTED]
        self.assertIn(gfnode.WRITE_SRC, names)
        self.assertIsNone(self.g.node(gfnode.WRITE_SRC_F))     # no float TIFF Write any more
        self.assertTrue(gen["files"][0].endswith(".png"))

    def test_old_node_with_float_write_still_works(self):
        # Nodes made before 0.9 contain the float TIFF Write; it is left alone and unused.
        with self.g:
            nuke.nodes.Write(name=gfnode.WRITE_SRC_F, file_type="tiff")["disable"].setValue(True)
        gfnode.ensure_current(self.g)
        self.assertFalse(gfnode.ensure_current(self.g))
        self.assertIsNotNone(self.g.node(gfnode.WRITE_SRC_F))
        fake_nuke.EXECUTED[:] = []
        gen = self._gen()
        names = [e[0] for e in fake_nuke.EXECUTED]
        self.assertNotIn(gfnode.WRITE_SRC_F, names)
        self.assertIn(gfnode.WRITE_SRC, names)
        self.assertTrue(gen["files"][0].endswith(".png"))

if __name__ == "__main__":
    unittest.main()
