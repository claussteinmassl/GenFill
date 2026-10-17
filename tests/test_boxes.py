"""Region prompt boxes inside the GenerativeFill node (genfill/boxes.py) against fake_nuke."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
import fake_nuke  # noqa: E402

nuke = fake_nuke.install()

from genfill import boxes, node as gfnode  # noqa: E402


class PaletteTests(unittest.TestCase):
    def test_eight_distinct_colours_that_wrap(self):
        colours = [boxes.palette(n) for n in range(1, 9)]
        self.assertEqual(len(set(colours)), 8)
        self.assertEqual(boxes.palette(9), boxes.palette(1))
        self.assertEqual(boxes.palette(1), (1.0, 0.25, 0.25))     # red first
        for rgb in colours:
            self.assertTrue(all(0.0 <= c <= 1.0 for c in rgb))

    def test_hex_and_packed(self):
        self.assertEqual(boxes.palette_hex(1), "#ff4040")
        self.assertEqual(boxes.palette_uint(1), 0xFF4040FF)
        self.assertEqual(boxes.palette_hex(10), boxes.palette_hex(2))


class BoxNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = os.path.join(self.tmp, "shot.nk")
        fake_nuke.EXPRESSION_ERRORS[:] = []
        self.g = gfnode.create()
        self.g.setInput(0, nuke.nodes.Read(name="Plate"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_add_box_creates_a_coloured_crop_on_the_source(self):
        n = boxes.add_box(self.g, (100, 200, 500, 600))
        self.assertEqual(n, 1)
        crop = self.g.node("GenFillBox1")
        self.assertEqual(crop.Class(), "Crop")
        self.assertIs(crop.input(0), self.g.node(gfnode.SRC_IN))
        self.assertEqual(list(crop["box"].value()), [100, 200, 500, 600])
        self.assertEqual(crop["gf_box_prompt"].value(), "")
        self.assertTrue(crop["gf_box_enabled"].value())
        self.assertEqual(crop["gl_color"].value(), boxes.palette_uint(1))
        self.assertEqual(crop["tile_color"].value(), boxes.palette_uint(1))
        self.assertEqual(fake_nuke.EXPRESSION_ERRORS, [])

    def test_lowest_free_number_and_list(self):
        for _ in range(3):
            boxes.add_box(self.g, (0, 0, 10, 10))
        boxes.remove_box(self.g, 2)
        self.assertIsNone(self.g.node("GenFillBox2"))
        self.assertEqual([b.n for b in boxes.list_boxes(self.g)], [1, 3])
        self.assertEqual(boxes.add_box(self.g, (0, 0, 10, 10)), 2)
        self.assertEqual(boxes.add_box(self.g, (0, 0, 10, 10)), 4)
        info = boxes.list_boxes(self.g)
        self.assertEqual([b.n for b in info], [1, 2, 3, 4])
        self.assertEqual(info[3].node_name, "GenFillBox4")
        self.assertEqual(info[3].rgb, boxes.palette(4))
        self.assertFalse(boxes.remove_box(self.g, 9))

    def test_prompt_and_enabled(self):
        boxes.add_box(self.g, (10, 20, 30, 40))
        boxes.set_prompt(self.g, 1, "a red balloon")
        boxes.set_enabled(self.g, 1, False)
        b = boxes.list_boxes(self.g)[0]
        self.assertEqual((b.prompt, b.enabled, b.rect), ("a red balloon", False, (10.0, 20.0, 30.0, 40.0)))
        self.assertFalse(boxes.set_prompt(self.g, 5, "x"))

    def test_rect_is_read_at_a_frame(self):
        boxes.add_box(self.g, (10, 20, 30, 40))
        knob = self.g.node("GenFillBox1")["box"]
        with mock.patch.object(knob, "valueAt", side_effect=lambda t, i=-1: [1, 2, 3, 4][i] + t):
            self.assertEqual(boxes.list_boxes(self.g, frame=100)[0].rect, (101.0, 102.0, 103.0, 104.0))

    def test_default_rect_is_the_centre_of_the_source(self):
        boxes.add_box(self.g)
        x, y, r, t = boxes.list_boxes(self.g)[0].rect
        self.assertEqual((x, y, r, t), (672.0, 378.0, 1248.0, 702.0))     # centre 30 % of 1920x1080

    def test_default_rect_is_the_last_selection(self):
        from genfill import engine
        st = {"generations": [{"selection": [50, 60, 300, 400], "files": []}], "selected": None}
        self.g["gf_state"].setValue(engine.encode_state(st))
        boxes.add_box(self.g)
        self.assertEqual(boxes.list_boxes(self.g)[0].rect, (50.0, 60.0, 300.0, 400.0))

    def test_overlay_outlines_every_box_in_the_gui_only(self):
        g = self.g
        sw = g.node(gfnode.BOX_OVERLAY)
        self.assertEqual(sw["which"].expression, "$gui && parent.gf_show_boxes")
        out = [c for c in g.nodes() if c.Class() == "Output"][0]
        self.assertIs(out.input(0).input(0), sw)          # through the anchor Switch
        self.assertIs(sw.input(0), g.node(gfnode.OUT_SWITCH))
        self.assertIs(sw.input(1), g.node(gfnode.OUT_SWITCH))      # no boxes: pass-through
        boxes.add_box(g, (0, 0, 10, 10))
        boxes.add_box(g, (0, 0, 10, 10))
        over2 = sw.input(1)
        self.assertEqual(over2.name(), "gf_boxov_over2")
        self.assertEqual(over2["operation"].value(), "over")
        self.assertEqual(over2["disable"].expression, "!GenFillBox2.gf_box_enabled")
        over1 = over2.input(0)
        self.assertEqual(over1.name(), "gf_boxov_over1")
        self.assertIs(over1.input(0), g.node(gfnode.OUT_SWITCH))
        ring = over2.input(1)
        self.assertEqual(ring["operation"].value(), "stencil")
        outer, inner = ring.input(0), ring.input(1)
        self.assertEqual(outer.Class(), "Rectangle")
        self.assertEqual(outer["color"].value(), list(boxes.palette(2)) + [1.0])
        self.assertEqual(outer["area"].expressions,
                         {0: "GenFillBox2.box.x", 1: "GenFillBox2.box.y",
                          2: "GenFillBox2.box.r", 3: "GenFillBox2.box.t"})
        self.assertEqual(inner["area"].expressions[0],
                         "min(GenFillBox2.box.x + 3, (GenFillBox2.box.x + GenFillBox2.box.r) / 2)")
        self.assertEqual(inner["area"].expressions[3],
                         "max(GenFillBox2.box.t - 3, (GenFillBox2.box.y + GenFillBox2.box.t) / 2)")

        def area(rect, box):
            """Evaluate the area expressions for a Crop box (x, y, r, t)."""
            import re
            values = dict(zip("xyrt", box))
            return [eval(re.sub(r"GenFillBox2\.box\.([xyrt])",
                                lambda m: str(values[m.group(1)]), e),
                         {"min": min, "max": max})
                    for e in (rect["area"].expressions[i] for i in range(4))]

        self.assertEqual(area(inner, (100, 50, 200, 90)), [103, 53, 197, 87])
        x, y, r, t = area(inner, (100, 50, 104, 52))         # smaller than 2 x OUTLINE
        self.assertLessEqual(x, r)
        self.assertLessEqual(y, t)
        for rect in (outer, inner):
            self.assertEqual(rect["output"].value(), "rgba")
            self.assertEqual(rect["premult"].value(), "none")
            self.assertEqual(rect["softness"].value(), 0)
        # Removing a box rebuilds the chain; nothing of box 1 is left behind.
        boxes.remove_box(g, 1)
        names = [c.name() for c in g.nodes()]
        self.assertFalse([nm for nm in names if nm.endswith("1") and nm.startswith("gf_box")])
        self.assertEqual(sw.input(1).name(), "gf_boxov_over2")
        self.assertIs(sw.input(1).input(0), g.node(gfnode.OUT_SWITCH))
        self.assertEqual(fake_nuke.EXPRESSION_ERRORS, [])

    def test_anchor_puts_every_box_crop_upstream_of_the_output(self):
        g = self.g
        out = [c for c in g.nodes() if c.Class() == "Output"][0]
        anchor = g.node(gfnode.BOX_ANCHOR)
        self.assertIs(out.input(0), anchor)
        self.assertIsNone(anchor["which"].expression)       # fixed, never expression-driven
        self.assertEqual(int(anchor["which"].value()), 0)
        self.assertIs(anchor.input(0), g.node(gfnode.BOX_OVERLAY))   # the image path
        self.assertEqual(anchor.inputs(), 1)
        for _ in range(3):
            boxes.add_box(g, (0, 0, 10, 10))
        self.assertEqual([anchor.input(i).name() for i in range(1, anchor.inputs())],
                         ["GenFillBox1", "GenFillBox2", "GenFillBox3"])
        boxes.remove_box(g, 2)
        self.assertEqual([anchor.input(i).name() for i in range(1, anchor.inputs())],
                         ["GenFillBox1", "GenFillBox3"])
        self.assertIs(anchor.input(0), g.node(gfnode.BOX_OVERLAY))

        def upstream(n, seen):
            for i in range(n.inputs()):
                up = n.input(i)
                if up is not None and up not in seen:
                    seen.add(up)
                    upstream(up, seen)
            return seen

        ups = upstream(out, set())
        for b in boxes.list_boxes(g):
            self.assertIn(g.node(b.node_name), ups)

    def test_box_mask_is_merged_max_into_the_selection(self):
        g = self.g
        bm = g.node(gfnode.BOX_MASK_SWITCH)
        sel = g.node(gfnode.SEL_SWITCH)
        self.assertIs(g.node(gfnode.GROW).input(0), bm)
        self.assertIs(bm.input(0), sel)
        self.assertEqual(int(bm["which"].value() or 0), 0)
        boxes.add_box(g, (0, 0, 10, 10))
        m = bm.input(1)
        self.assertEqual(m.name(), "gf_boxmask_max1")
        self.assertEqual(m["operation"].value(), "max")
        self.assertIs(m.input(0), sel)
        self.assertEqual(m["disable"].expression, "!GenFillBox1.gf_box_enabled")
        rect = m.input(1)
        self.assertEqual(rect["color"].value(), [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(rect["area"].expressions[2], "GenFillBox1.box.r")
        self.assertEqual((rect["output"].value(), rect["softness"].value()), ("rgba", 0))

    def test_viewer_tools_open_hidden_floating_panels(self):
        boxes.add_box(self.g, (0, 0, 10, 10))
        boxes.add_box(self.g, (0, 0, 10, 10))
        boxes.set_enabled(self.g, 2, False)
        b1, b2 = self.g.node("GenFillBox1"), self.g.node("GenFillBox2")
        self.assertEqual(boxes.attach_box_tools(self.g), 0)          # no GUI: nothing opened
        self.assertFalse(getattr(b1, "panel_open", False))
        with mock.patch.object(boxes.nuke, "GUI", True), \
                mock.patch.object(gfnode.nuke, "GUI", True), \
                mock.patch.object(gfnode, "_hide_floating_panels", return_value=1) as hide:
            self.assertEqual(boxes.attach_box_tools(self.g), 1)
            self.assertTrue(b1.panel_open and b1.panel_float)
            self.assertFalse(getattr(b2, "panel_open", False))
            hide.assert_called_with(["GenFillBox1"])
            boxes.detach_box_tools(self.g)
        self.assertFalse(b1.panel_open)

    def test_show_panel_attaches_roto_and_box_tools(self):
        g = self.g
        boxes.add_box(g, (0, 0, 10, 10))
        n = gfnode.nuke
        n.thisNode = lambda: g
        try:
            with mock.patch.object(gfnode, "attach_roto_tools") as roto, \
                    mock.patch.object(boxes, "attach_box_tools") as attach, \
                    mock.patch.object(boxes, "detach_box_tools") as detach, \
                    mock.patch.object(gfnode, "detach_roto_tools") as roto_off:
                n.thisKnob = lambda: g["showPanel"]
                gfnode.knob_changed()
                self.assertTrue(roto.called)
                attach.assert_called_with(g)
                n.thisKnob = lambda: g["hidePanel"]
                gfnode.knob_changed()
                detach.assert_called_with(g)
                self.assertTrue(roto_off.called)
        finally:
            del n.thisNode, n.thisKnob

    def test_old_nodes_get_the_box_switches_and_knob(self):
        g = self.g
        out = [c for c in g.nodes() if c.Class() == "Output"][0]
        with g:
            fake_nuke.delete(g.node(gfnode.BOX_OVERLAY))
            fake_nuke.delete(g.node(gfnode.BOX_ANCHOR))
            fake_nuke.delete(g.node(gfnode.BOX_MASK_SWITCH))
        out.setInput(0, g.node(gfnode.OUT_SWITCH))
        g.node(gfnode.GROW).setInput(0, g.node(gfnode.SEL_SWITCH))
        del g._knobs["gf_show_boxes"]
        self.assertTrue(gfnode.ensure_current(g))
        self.assertTrue(g["gf_show_boxes"].value())
        sw = g.node(gfnode.BOX_OVERLAY)
        anchor = g.node(gfnode.BOX_ANCHOR)
        self.assertIs(out.input(0), anchor)
        self.assertIs(anchor.input(0), sw)
        self.assertEqual(anchor.inputs(), 1)
        self.assertIs(sw.input(0), g.node(gfnode.OUT_SWITCH))
        self.assertIs(sw.input(1), g.node(gfnode.OUT_SWITCH))
        bm = g.node(gfnode.BOX_MASK_SWITCH)
        self.assertIs(g.node(gfnode.GROW).input(0), bm)
        self.assertIs(bm.input(1), g.node(gfnode.SEL_SWITCH))
        self.assertEqual(fake_nuke.EXPRESSION_ERRORS, [])
        self.assertFalse(gfnode.ensure_current(g))

    def test_nodes_from_before_the_anchor_get_it(self):
        g = self.g
        out = [c for c in g.nodes() if c.Class() == "Output"][0]
        boxes.add_box(g, (0, 0, 10, 10))
        with g:
            fake_nuke.delete(g.node(gfnode.BOX_ANCHOR))
        out.setInput(0, g.node(gfnode.BOX_OVERLAY))
        self.assertTrue(gfnode.ensure_current(g))
        anchor = g.node(gfnode.BOX_ANCHOR)
        self.assertIs(anchor.input(1), g.node("GenFillBox1"))
        self.assertIs(out.input(0), anchor)
        self.assertIs(anchor.input(0), g.node(gfnode.BOX_OVERLAY))
        self.assertFalse(gfnode.ensure_current(g))


if __name__ == "__main__":
    unittest.main()
