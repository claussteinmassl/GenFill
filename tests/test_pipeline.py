"""End-to-end test of node build + generate using a fake ``nuke`` module and the
offline mock backend. Verifies the Python plumbing, not Nuke's rendering."""
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

from genfill import engine, node as gfnode, pngio, state  # noqa: E402


def _join_workers():
    for t in threading.enumerate():
        if t.name.startswith("GenFill-"):
            t.join(30)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = os.path.join(self.tmp, "shot.nk")
        fake_nuke.EXECUTED[:] = []
        fake_nuke.EXPRESSION_ERRORS[:] = []
        self.plate = nuke.nodes.Read(name="Plate")
        self.g = gfnode.create()
        self.g.setInput(0, self.plate)
        fake_nuke.add_shape(self.g.node(gfnode.ROTO))
        mock_label = [l for l in gfnode.backend_labels() if "Offline" in l][0]
        self.g["gf_backend"].setValue(mock_label)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_build(self):
        g = self.g
        self.assertEqual(fake_nuke.EXPRESSION_ERRORS, [])
        for name in (gfnode.READ_RESULT, gfnode.COMP, gfnode.OUT_SWITCH, gfnode.PROBE_WRITE,
                     gfnode.WRITE_SRC, gfnode.WRITE_MASK, gfnode.WRITE_MASK_A):
            self.assertIsNotNone(g.node(name), name)
        for name in gfnode.WRITE_NODES:
            self.assertTrue(g.node(name)["disable"].value(), "%s must start disabled" % name)
        self.assertEqual(g.node(gfnode.OUT_SWITCH)["which"].expression,
                         "parent.gf_has_result ? parent.gf_output : 2")
        self.assertEqual(g.node(gfnode.COMP).input(2), g.node(gfnode.FEATHER))
        # The selection Roto lives inside the node; nothing is created outside it.
        self.assertIsNone(g.input(1))
        roto = g.node(gfnode.ROTO)
        self.assertEqual(roto.Class(), "Roto")
        self.assertTrue(roto["replace"].value())
        self.assertIs(roto.input(0), g.node(gfnode.SRC_IN))
        sw = g.node(gfnode.SEL_SWITCH)
        self.assertIs(sw.input(gfnode.SEL_SHAPES), roto)
        self.assertIs(sw.input(gfnode.SEL_MASK), g.node(gfnode.MASK_IN))
        self.assertIs(sw.input(gfnode.SEL_ALPHA), g.node(gfnode.SRC_IN))
        self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_SHAPES)
        self.assertEqual(g["gf_shapes"].link, "gf_roto.curves")
        self.assertIn("gf_draw", g.knobs())
        self.assertEqual(g["gf_variations"].value(), 3)

    def test_layer_output_has_source_format(self):
        g = self.g
        sw = g.node(gfnode.OUT_SWITCH)
        layer = g.node(gfnode.LAYER)
        self.assertIs(sw.input(1), layer)
        self.assertEqual(layer["operation"].value(), "over")
        self.assertIs(layer.input(1), g.node(gfnode.PREMULT))           # A: premultiplied fill
        blank = layer.input(0)                                           # B: Source, emptied
        self.assertEqual(blank.Class(), "Remove")
        self.assertIs(blank.input(0), g.node(gfnode.SRC_IN))
        self.assertEqual(g["gf_output"].values(), ["Comp", "Layer", "Bypass"])
        # the alpha that premultiplies the fill is the feathered, expanded selection
        self.assertIs(g.node(gfnode.COPY_ALPHA).input(1), g.node(gfnode.FEATHER))

    def test_old_nodes_are_upgraded(self):
        g = self.g
        with g:
            fake_nuke.delete(g.node(gfnode.LAYER))
            fake_nuke.delete(g.node(gfnode.BLANK))
        g.node(gfnode.OUT_SWITCH).setInput(1, g.node(gfnode.PREMULT))
        g["gf_output"].setValues(["Comp", "Fill only (premult)", "Bypass"])
        g["gf_output"].setValue(1)
        self.assertTrue(gfnode.ensure_current(g))
        self.assertIs(g.node(gfnode.OUT_SWITCH).input(1), g.node(gfnode.LAYER))
        self.assertEqual(g["gf_output"].value(), "Layer")
        self.assertFalse(gfnode.ensure_current(g))      # idempotent

    def test_old_nodes_get_the_resolution_knob(self):
        g = self.g
        self.assertEqual(g["gf_resolution"].values(), ["auto", "1K", "2K", "4K"])
        del g._knobs["gf_resolution"]
        self.assertEqual(engine.resolution(g), "auto")
        self.assertTrue(gfnode.ensure_current(g))
        self.assertEqual(g["gf_resolution"].value(), "auto")
        self.assertFalse(gfnode.ensure_current(g))

    def test_generate_at_2k(self):
        """The Resolution knob reaches the backend: tier size, request and state."""
        from unittest import mock
        from genfill.backends import base
        g = self.g
        g["gf_resolution"].setValue("2K")
        g["gf_variations"].setValue(1)
        seen = []
        mock_backend = gfnode.backend_for(g)
        real = mock_backend.generate
        # Small stand-in tiers keep the pure-Python fake renders fast; the plumbing is the same.
        tiers = {"1K": 64 * 64, "2K": 128 * 128, "4K": 256 * 256}
        def spy(self, req, *a, **k):
            seen.append(req)
            return real(req, *a, **k)

        with mock.patch.dict(base.TIER_PIXELS, tiers), \
                mock.patch.object(type(mock_backend), "generate", spy):
            self.assertTrue(engine.generate(g, "a crate", blocking=True), engine.status(g))
            plan = engine.size_plan(g)
        gen = engine.get_state(g)["generations"][0]
        mw, mh = gen["model_size"]
        self.assertAlmostEqual(mw * mh / float(tiers["2K"]), 1.0, delta=0.15)
        self.assertEqual(gen["resolution"], "2K")
        self.assertEqual((seen[0].resolution, seen[0].width, seen[0].height), ("2K", mw, mh))
        self.assertEqual((plan.width, plan.height, plan.tier), (mw, mh, "2K"))
        self.assertIn("(2K)", plan.text())
        src = pngio.read(fake_nuke.EXECUTED[1][1])
        self.assertEqual((src.width, src.height), (mw, mh))

    def test_2k_never_upscales_a_small_region(self):
        g = self.g
        g["gf_resolution"].setValue("2K")
        g["gf_variations"].setValue(1)
        self.assertTrue(engine.generate(g, "a crate", blocking=True), engine.status(g))
        gen = engine.get_state(g)["generations"][0]
        _x, _y, w, h = gen["region"]
        mw, mh = gen["model_size"]
        self.assertLess(w * h, 2048 * 2048)
        self.assertLessEqual(abs(mw - w), 8)          # the crop's own size, on the 8 px grid
        self.assertLessEqual(abs(mh - h), 8)
        self.assertIn("crop is smaller than 2K", engine.size_plan(g).text())

    def test_size_plan_before_the_first_generation_uses_the_plate(self):
        g = self.g
        g["gf_resolution"].setValue("1K")
        plan = engine.size_plan(g)
        self.assertAlmostEqual(plan.width * plan.height / 1048576.0, 1.0, delta=0.03)
        g["gf_resolution"].setValue("auto")
        self.assertIn("(auto)", engine.size_plan(g).text())

    def test_empty_shapes_explains_what_to_do(self):
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        self.assertFalse(engine.generate(g, "x", blocking=True))
        self.assertIn("Draw Selection", engine.status(g)["error"])

    def test_mask_input_switches_selection_mode(self):
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        matte = nuke.nodes.Read(name="Matte")
        g.setInput(1, matte)
        n = gfnode.nuke          # the fake module node.py imported
        n.thisNode = lambda: g
        n.thisKnob = lambda: g["inputChange"]
        try:
            gfnode.knob_changed()
            self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_MASK)
            g.setInput(1, None)
            gfnode.knob_changed()
            self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_SHAPES)
            # Shapes already drawn: connecting a mask doesn't take them away.
            fake_nuke.add_shape(g.node(gfnode.ROTO))
            g.setInput(1, matte)
            gfnode.knob_changed()
            self.assertEqual(g["gf_selection"].getValue(), gfnode.SEL_SHAPES)
        finally:
            del n.thisNode, n.thisKnob

    def test_pre_07_selection_is_upgraded(self):
        g = self.g
        sw = g.node(gfnode.SEL_SWITCH)
        with g:
            fake_nuke.delete(g.node(gfnode.ROTO))
        sw.setInput(0, g.node(gfnode.MASK_IN))
        sw.setInput(1, g.node(gfnode.SRC_IN))
        sw.setInput(2, None)
        for name in ("gf_shapes", "gf_draw"):
            del g._knobs[name]
        g["gf_selection"].setValues(["Mask input", "Source alpha"])
        g["gf_selection"].setValue(1)                  # old "Source alpha"
        self.assertTrue(gfnode.ensure_current(g))
        roto = g.node(gfnode.ROTO)
        self.assertIsNotNone(roto)
        self.assertIs(sw.input(gfnode.SEL_SHAPES), roto)
        self.assertIs(sw.input(gfnode.SEL_MASK), g.node(gfnode.MASK_IN))
        self.assertIs(sw.input(gfnode.SEL_ALPHA), g.node(gfnode.SRC_IN))
        self.assertEqual(g["gf_selection"].value(), "Source alpha")
        self.assertIn("gf_shapes", g.knobs())
        self.assertFalse(gfnode.ensure_current(g))

    def test_old_backend_menu_is_upgraded_by_label(self):
        g = self.g
        old = ["Local", "Stability", "OpenAI", "Offline Preview (no AI)"]
        g["gf_backend"].setValues(old)
        g["gf_backend"].setValue(3)                    # Offline Preview, index 3 in old menus
        self.assertTrue(gfnode.ensure_current(g))
        self.assertEqual(g["gf_backend"].values(), gfnode.backend_labels())
        self.assertEqual(g["gf_backend"].value(), "Offline Preview (no AI)")
        self.assertFalse(gfnode.ensure_current(g))

    def test_local_backend_moves_to_comfyui(self):
        g = self.g
        old = ["Local: FLUX.1 Fill (free, runs on your GPU)", "Stability", "OpenAI"]
        g["gf_backend"].setValues(old)
        g["gf_backend"].setValue(0)
        g["gf_model"].setValue("flux-fill-dev")
        self.assertTrue(gfnode.ensure_current(g))
        self.assertEqual(g["gf_backend"].value(), "ComfyUI (local, network or cloud)")
        self.assertEqual(g["gf_model"].value(), "")
        self.assertFalse(gfnode.ensure_current(g))

    def test_model_cleared_when_not_in_new_backend(self):
        from genfill import backends
        self.assertEqual(gfnode.model_for_backend("gpt-image-1.5", backends.get("fal")), "")
        fal = backends.get("fal")
        keep = fal.model_ids()[0]
        self.assertEqual(gfnode.model_for_backend(keep, fal), keep)
        self.assertEqual(gfnode.model_for_backend("", fal), "")

    def test_generate_and_select(self):
        g = self.g
        self.assertTrue(engine.generate(g, "a red vintage car"))
        _join_workers()
        st = engine.get_state(g)
        self.assertEqual(len(st["generations"]), 1, engine.status(g))
        gen = st["generations"][0]
        self.assertEqual(len(gen["files"]), 3)
        self.assertEqual(gen["frame"], 1001)
        self.assertEqual(gen["prompt"], "a red vintage car")

        # Selection box in the fake probe: x 40-60%, y 30-60% from top of 1920x1080.
        x, y, w, h = gen["region"]
        self.assertLessEqual(x, 0.4 * 1920)
        self.assertGreaterEqual(x + w, 0.6 * 1920)
        self.assertLessEqual(y, (1 - 0.6) * 1080)
        self.assertGreaterEqual(y + h, (1 - 0.3) * 1080)
        mw, mh = gen["model_size"]
        self.assertAlmostEqual(float(w) / h, float(mw) / mh, delta=0.03)

        # Crops were rendered at model size and in order; writes re-disabled.
        names = [e[0] for e in fake_nuke.EXECUTED]
        self.assertEqual(names, [gfnode.PROBE_WRITE, gfnode.WRITE_SRC, gfnode.WRITE_MASK,
                                 gfnode.WRITE_MASK_A])
        src = pngio.read(fake_nuke.EXECUTED[1][1])
        self.assertEqual((src.width, src.height), (mw, mh))
        for name in gfnode.WRITE_NODES:
            self.assertTrue(g.node(name)["disable"].value())

        # Result wiring.
        read = g.node(gfnode.READ_RESULT)
        self.assertTrue(read["file"].value().endswith(gen["files"][0]))
        self.assertTrue(os.path.exists(read["file"].value()))
        self.assertEqual(g.node(gfnode.REFORMAT_RECV)["box_width"].value(), w)
        self.assertEqual(g.node(gfnode.TRANSFORM_RECV)["translate"].value(), [x, y])
        self.assertTrue(g["gf_has_result"].value())
        self.assertIn("(1/3)", g["gf_info"].value())
        self.assertTrue(read["file"].value().startswith(
            os.path.join(self.tmp, "genfill_cache").replace("\\", "/")))

        # Second generation, then navigate and delete.
        self.assertTrue(engine.generate(g, ""))
        _join_workers()
        thumbs = engine.thumbnails(g)
        self.assertEqual(len(thumbs), 6)
        self.assertEqual(thumbs[0]["prompt"], "")          # newest first, selected
        self.assertTrue(thumbs[0]["selected"])
        engine.step(g, 1)
        self.assertIn("(2/6)", g["gf_info"].value())
        engine.select(g, 0, 2)
        self.assertTrue(g.node(gfnode.READ_RESULT)["file"].value().endswith("var_03.png"))
        path = engine.thumbnails(g)[5]["path"]
        engine.delete_variation(g, 0, 2)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(engine.thumbnails(g)), 5)

    def test_state_knob_has_no_tcl_characters(self):
        engine.generate(self.g, "a [bracketed] {prompt} $var", blocking=True)
        raw = self.g["gf_state"].value()
        self.assertTrue(raw.startswith("b64:"))
        for ch in "[]{}$":
            self.assertNotIn(ch, raw)
            self.assertNotIn(ch, self.g["gf_info"].value())
        self.assertEqual(engine.get_state(self.g)["generations"][0]["prompt"],
                         "a [bracketed] {prompt} $var")
        # Old plain-JSON state still loads.
        self.assertEqual(engine.decode_state('{"generations": []}')["generations"], [])

    def test_multiline_prompt_round_trip(self):
        prompt = "a [red] car\n\n{on} a $road\nat dusk"
        self.assertEqual(self.g["gf_prompt"].kind, "Multiline_Eval_String_Knob")
        self.assertEqual(self.g["gf_negative"].kind, "Multiline_Eval_String_Knob")
        self.assertTrue(engine.generate(self.g, prompt, blocking=True))
        self.assertEqual(self.g["gf_prompt"].value(), prompt)
        self.assertEqual(engine.get_state(self.g)["generations"][0]["prompt"], prompt)
        raw = self.g["gf_state"].value()
        for ch in "[]{}$\n":
            self.assertNotIn(ch, raw)
        info = self.g["gf_info"].value()
        self.assertNotIn("\n", info)
        self.assertTrue(info.startswith("a (red) car"), info)
        self.assertNotIn("road", info)
        # Leading blank lines are skipped.
        engine.generate(self.g, "\n  \nsecond line first", blocking=True)
        self.assertTrue(self.g["gf_info"].value().startswith("second line first"))

    def test_pipeline_env_sets_folder_and_names(self):
        from unittest import mock
        env = {"SHOW": "demo", "SHOT": "sh010", "TASK": "comp",
               "GENFILL_CACHE": os.path.join(self.tmp, "jobs", "$SHOW", "${SHOT}", "{node}"),
               "GENFILL_NAME": "${SHOT}_${TASK}_genfill_v{gen:03d}",
               "GENFILL_VARIATION_NAME": "{name}_var{var:02d}"}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(engine.generate(self.g, "crate", blocking=True), engine.status(self.g))
            folder = os.path.join(self.tmp, "jobs", "demo", "sh010", self.g.name())
            # A folder that already exists is skipped: the number goes up, nothing is overwritten.
            os.makedirs(os.path.join(folder, "sh010_comp_genfill_v002"))
            self.assertTrue(engine.generate(self.g, "crate", blocking=True), engine.status(self.g))
        gens = engine.get_state(self.g)["generations"]
        self.assertEqual([g["id"] for g in gens],
                         ["sh010_comp_genfill_v001", "sh010_comp_genfill_v003"])
        self.assertEqual([g["number"] for g in gens], [1, 3])
        self.assertEqual(gens[0]["files"][0], "sh010_comp_genfill_v001/sh010_comp_genfill_v001_var01.png")
        for f in gens[1]["files"]:
            self.assertTrue(os.path.exists(os.path.join(folder, f)), f)
        self.assertNotIn("var_names", gens[0])
        read = self.g.node(gfnode.READ_RESULT)["file"].value()
        self.assertTrue(read.startswith(folder.replace("\\", "/")), read)

    def test_bad_name_pattern_stops_before_generating(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"GENFILL_NAME": "{shot}_v{gen}"}):
            self.assertFalse(engine.generate(self.g, "x", blocking=True))
        self.assertIn("Unknown token {shot}", engine.status(self.g)["error"])
        self.assertEqual(fake_nuke.EXECUTED, [])
        self.assertFalse(engine.is_running(self.g))

    def test_missing_key_reports_instead_of_silently_returning(self):
        from unittest import mock
        import genfill.ui
        stability_label = [l for l in gfnode.backend_labels() if "Stability" in l][0]
        self.g["gf_backend"].setValue(stability_label)
        fake_settings = mock.MagicMock()
        fake_settings.show.return_value = False
        with mock.patch.dict(sys.modules, {"genfill.ui.settings": fake_settings}), \
                mock.patch.object(genfill.ui, "settings", fake_settings, create=True), \
                mock.patch("genfill.config.get_api_key", return_value=""):
            self.assertFalse(engine.generate(self.g, "x"))
        self.assertTrue(fake_settings.show.called)
        self.assertIn("No API key", engine.status(self.g)["error"])
        self.assertFalse(engine.is_running(self.g))

    def test_worker_never_leaves_node_running(self):
        from unittest import mock
        with mock.patch.object(fake_nuke, "executeInMainThread", lambda fn, args=(): None):
            engine.generate(self.g, "x")      # result never gets dispatched back
            _join_workers()
        # dispatch "happened" (it was swallowed), so status is still running here;
        # simulate a crash inside the worker instead:
        engine._set_status(engine._uid(self.g), running=False, error="")
        with mock.patch.object(engine.backends.get("mock"), "generate",
                               side_effect=SystemError("boom")):
            engine.generate(self.g, "x", blocking=True)
        self.assertFalse(engine.is_running(self.g))
        self.assertIn("boom", engine.status(self.g)["error"])

    def test_log_file_written(self):
        from genfill import log as gflog
        engine.generate(self.g, "logged prompt", blocking=True)
        for h in gflog.get().handlers:
            h.flush()
        text = open(gflog.log_path(), encoding="utf-8").read()
        self.assertIn("Generate clicked", text)
        self.assertIn("logged prompt", text)

    def test_empty_selection_is_reported(self):
        fake_nuke.SELECTION_BOX[:] = [0, 0, 0, 0]
        try:
            self.assertFalse(engine.generate(self.g, "x"))
            self.assertIn("selection is empty", engine.status(self.g)["error"])
            self.assertFalse(self.g["gf_has_result"].value())
        finally:
            fake_nuke.SELECTION_BOX[:] = [0.4, 0.3, 0.6, 0.6]

    # ---- region prompt boxes --------------------------------------------------

    def _generate_with_layout(self, style, prompt="a crate"):
        """Generate one variation with the mock backend reporting ``style``; the request."""
        from unittest import mock
        g = self.g
        g["gf_variations"].setValue(1)
        backend = gfnode.backend_for(g)
        seen = []
        real = backend.generate

        def spy(self, req, *a, **k):
            seen.append(req)
            return real(req, *a, **k)

        with mock.patch.object(type(backend), "layout_style", lambda self, model="": style), \
                mock.patch.object(type(backend), "generate", spy):
            ok = engine.generate(g, prompt, blocking=True)
        self.assertTrue(ok, engine.status(g))
        return seen[0], engine.get_state(g)["generations"][-1]

    @staticmethod
    def _to_crop(rect, gen):
        x, y, w, h = gen["region"]
        mw, mh = gen["model_size"]
        bx, by, br, bt = rect
        return ((bx - x) * mw / float(w), (y + h - bt) * mh / float(h),
                (br - x) * mw / float(w), (y + h - by) * mh / float(h))

    def test_boxes_reach_the_request_in_crop_pixels(self):
        from genfill import boxes
        rect = (800, 450, 1000, 600)                 # inside the fake selection
        boxes.add_box(self.g, rect)
        boxes.set_prompt(self.g, 1, "a red balloon")
        req, gen = self._generate_with_layout("ideogram")
        self.assertEqual(req.prompt, "a crate")        # Ideogram: the backend formats it
        self.assertEqual(len(req.boxes), 1)
        b = req.boxes[0]
        self.assertEqual((b.n, b.prompt), (1, "a red balloon"))
        for got, want in zip((b.x0, b.y0, b.x1, b.y1), self._to_crop(rect, gen)):
            self.assertAlmostEqual(got, want, places=1)
        self.assertLess(b.y0, b.y1)                    # top-left origin: t is the smaller y
        self.assertEqual(gen["boxes"], [{"n": 1, "x0": b.x0, "y0": b.y0, "x1": b.x1,
                                         "y1": b.y1, "prompt": "a red balloon"}])
        self.assertEqual(gen["layout"], "ideogram")
        self.assertEqual(len(gen["selection"]), 4)

    def test_region_grows_to_include_every_enabled_box(self):
        from genfill import boxes
        boxes.add_box(self.g, (60, 40, 200, 160))      # far from the selection (bottom-left)
        boxes.set_prompt(self.g, 1, "a dog")
        boxes.add_box(self.g, (1800, 1000, 1900, 1070))
        boxes.set_prompt(self.g, 2, "a bird")
        boxes.set_enabled(self.g, 2, False)            # disabled: neither in the region nor sent
        self.g["gf_context"].setValue(0.0)             # tight region: union + minimum pad
        self.g["gf_min_size"].setValue(16)
        req, gen = self._generate_with_layout("ideogram")
        x, y, w, h = gen["region"]
        self.assertLessEqual(x, 60)
        self.assertLessEqual(y, 40)
        self.assertLess(x + w, 1800)
        self.assertEqual([b.n for b in req.boxes], [1])

    def test_boxes_are_ignored_by_models_without_layouts(self):
        from genfill import boxes
        boxes.add_box(self.g, (60, 40, 200, 160))
        boxes.set_prompt(self.g, 1, "a dog")
        req, gen = self._generate_with_layout(None)
        self.assertEqual(req.boxes, [])
        self.assertEqual(req.prompt, "a crate")
        self.assertNotIn("boxes", gen)
        self.assertGreater(gen["region"][0], 60)

    def test_box_without_text_widens_the_region_but_is_not_sent(self):
        from genfill import boxes
        boxes.add_box(self.g, (60, 40, 200, 160))
        req, gen = self._generate_with_layout("ideogram")
        self.assertEqual(req.boxes, [])
        self.assertLessEqual(gen["region"][0], 60)

    def test_boxes_alone_are_the_selection(self):
        from genfill import boxes
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        fake_nuke.SELECTION_BOX[:] = [0, 0, 0, 0]      # the probe renders nothing
        try:
            boxes.add_box(g, (300, 300, 700, 600))
            boxes.set_prompt(g, 1, "a lamp")
            req, gen = self._generate_with_layout("flux3")
        finally:
            fake_nuke.SELECTION_BOX[:] = [0.4, 0.3, 0.6, 0.6]
        self.assertTrue(gen["box_selection"])
        self.assertEqual(int(g.node(gfnode.BOX_MASK_SWITCH)["which"].value()), 1)
        self.assertEqual(gen["selection"], [300, 300, 700, 600])
        x, y, w, h = gen["region"]
        self.assertTrue(x <= 300 and y <= 300 and x + w >= 700 and y + h >= 600)
        # explicit boxes: the prompt is sent as typed, prefixed for reference-edit FLUX 3
        self.assertEqual(req.prompt, "In <ref_image_0>, a crate")
        self.assertEqual([b.n for b in req.boxes], [1])
        # A normal generation switches the box mask off again.
        fake_nuke.add_shape(g.node(gfnode.ROTO))
        req, gen = self._generate_with_layout(None)
        self.assertFalse(gen.get("box_selection", False))
        self.assertEqual(int(g.node(gfnode.BOX_MASK_SWITCH)["which"].value()), 0)
        engine.select(g, 0, 0)                          # back to the box generation
        self.assertEqual(int(g.node(gfnode.BOX_MASK_SWITCH)["which"].value()), 1)

    def _box_generation(self):
        """Generate once with the boxes as the selection; the box mask Switch is then on."""
        from genfill import boxes
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        fake_nuke.SELECTION_BOX[:] = [0, 0, 0, 0]
        try:
            boxes.add_box(g, (300, 300, 700, 600))
            boxes.set_prompt(g, 1, "a lamp")
            _req, gen = self._generate_with_layout("flux3")
        finally:
            fake_nuke.SELECTION_BOX[:] = [0.4, 0.3, 0.6, 0.6]
        self.assertTrue(gen["box_selection"])
        self.assertEqual(int(g.node(gfnode.BOX_MASK_SWITCH)["which"].value()), 1)

    def test_box_mask_restored_when_the_selection_is_empty(self):
        from genfill import boxes
        g = self.g
        self._box_generation()
        boxes.set_enabled(g, 1, False)
        fake_nuke.add_shape(g.node(gfnode.ROTO))
        fake_nuke.SELECTION_BOX[:] = [0, 0, 0, 0]       # the shape renders nothing
        try:
            self.assertFalse(engine.generate(g, "x", blocking=True))
        finally:
            fake_nuke.SELECTION_BOX[:] = [0.4, 0.3, 0.6, 0.6]
        self.assertIn("selection is empty", engine.status(g)["error"])
        self.assertEqual(int(g.node(gfnode.BOX_MASK_SWITCH)["which"].value()), 1)

    def test_box_mask_restored_when_a_mask_write_fails(self):
        from unittest import mock
        g = self.g
        self._box_generation()
        fake_nuke.add_shape(g.node(gfnode.ROTO))
        real = nuke.execute

        def failing(w, *a):
            if w.name() == gfnode.WRITE_MASK_A:
                raise RuntimeError("render failed")
            return real(w, *a)

        with mock.patch.object(nuke, "execute", failing):
            self.assertFalse(engine.generate(g, "x", blocking=True))
        self.assertIn("render failed", engine.status(g)["error"])
        self.assertEqual(int(g.node(gfnode.BOX_MASK_SWITCH)["which"].value()), 1)

    def test_box_mask_restored_when_the_backend_fails_or_is_cancelled(self):
        from unittest import mock
        from genfill.backends import BackendError, Cancelled
        g = self.g
        fake_nuke.add_shape(g.node(gfnode.ROTO))
        self._generate_with_layout(None)                 # shown: a normal generation (0)
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        from genfill import boxes
        boxes.add_box(g, (300, 300, 700, 600))           # next one: the boxes are the selection
        boxes.set_prompt(g, 1, "a lamp")
        backend = gfnode.backend_for(g)
        for exc in (BackendError("boom"), Cancelled()):
            def boom(self, *a, **k):
                raise exc
            fake_nuke.SELECTION_BOX[:] = [0, 0, 0, 0]
            try:
                with mock.patch.object(type(backend), "layout_style",
                                       lambda self, model="": "flux3"), \
                        mock.patch.object(type(backend), "generate", boom), \
                        mock.patch.object(engine, "refresh_result", lambda node: None):
                    self.assertFalse(engine.generate(g, "x", blocking=True))
            finally:
                fake_nuke.SELECTION_BOX[:] = [0.4, 0.3, 0.6, 0.6]
            self.assertEqual(int(g.node(gfnode.BOX_MASK_SWITCH)["which"].value()), 0, exc)

    def test_box_overlays_are_off_in_every_render(self):
        """Renders GenFill runs itself never see any GenFill node's box outlines."""
        from unittest import mock
        from genfill import boxes
        g = self.g
        g2 = gfnode.create()                              # downstream: renders through g
        g2.setInput(0, g)
        fake_nuke.add_shape(g2.node(gfnode.ROTO))
        outer = nuke.nodes.Group(name="Outer")
        with outer:
            g3 = gfnode.create()                          # inside a Group
        for n in (g, g2, g3):
            n["gf_backend"].setValue(g["gf_backend"].value())     # the offline mock
            gfnode.ensure_current(n)
            boxes.add_box(n, (800, 450, 1000, 600))
        overlays = [n.node(gfnode.BOX_OVERLAY) for n in (g, g2, g3)]
        for ov in overlays:
            self.assertFalse(ov["disable"].value())
        seen = []
        real = nuke.execute

        def spy(w, *a):
            seen.append([bool(ov["disable"].value()) for ov in overlays])
            return real(w, *a)

        with mock.patch.object(nuke, "execute", spy):
            self.assertTrue(engine.generate(g2, "x", blocking=True), engine.status(g2))
        self.assertEqual(len(seen), 4)                    # probe, source, mask, mask alpha
        self.assertTrue(all(all(s) for s in seen), seen)
        for ov in overlays:
            self.assertFalse(ov["disable"].value())       # restored afterwards
        # The $gui expression stays for Viewer display and Write-node renders.
        self.assertEqual(overlays[0]["which"].expression, gfnode.BOX_OVERLAY_EXPR)

    def test_no_selection_and_no_boxes_still_explains(self):
        from genfill import boxes
        g = self.g
        g.node(gfnode.ROTO)["curves"].rootLayer[:] = []
        boxes.add_box(g, (300, 300, 700, 600))
        boxes.set_prompt(g, 1, "a lamp")
        from unittest import mock
        backend = gfnode.backend_for(g)
        with mock.patch.object(type(backend), "layout_style", lambda self, model="": None):
            self.assertFalse(engine.generate(g, "x", blocking=True))   # no layout support
        self.assertIn("Draw Selection", engine.status(g)["error"])
        boxes.set_enabled(g, 1, False)
        with mock.patch.object(type(backend), "layout_style", lambda self, model="": "flux3"):
            self.assertFalse(engine.generate(g, "x", blocking=True))   # only a disabled box
        self.assertIn("Draw Selection", engine.status(g)["error"])

    def test_box_outside_the_plate_is_dropped_with_a_note(self):
        from genfill import boxes
        boxes.add_box(self.g, (2500, 100, 2700, 300))   # right of the 1920 px plate
        boxes.set_prompt(self.g, 1, "a ghost")
        req, gen = self._generate_with_layout("ideogram")
        self.assertEqual(req.boxes, [])
        self.assertIn("outside", engine.status(self.g)["message"])
        self.assertLess(gen["region"][0] + gen["region"][2], 1921)

    def test_implicit_flux3_box_is_the_selection(self):
        req, gen = self._generate_with_layout("flux3")
        self.assertEqual(req.prompt, "In <ref_image_0>, a crate <obj_1>")
        self.assertEqual(len(req.boxes), 1)
        b = req.boxes[0]
        self.assertEqual((b.n, b.prompt), (1, "a crate"))
        for got, want in zip((b.x0, b.y0, b.x1, b.y1), self._to_crop(gen["selection"], gen)):
            self.assertAlmostEqual(got, want, places=1)
        self.assertEqual(gen["prompt"], "a crate")             # history: what was typed
        self.assertEqual(gen["sent_prompt"], "In <ref_image_0>, a crate <obj_1>")
        self.assertEqual(gen["boxes"][0]["prompt"], "a crate")

    def test_implicit_box_for_fal_klein_has_no_prefix(self):
        req, _gen = self._generate_with_layout("flux3_fields")
        self.assertEqual(req.prompt, "a crate <obj_1>")
        self.assertEqual([b.n for b in req.boxes], [1])

    def test_no_implicit_box_for_ideogram_or_an_empty_prompt(self):
        req, gen = self._generate_with_layout("ideogram")
        self.assertEqual((req.prompt, req.boxes), ("a crate", []))
        req, gen = self._generate_with_layout("flux3", prompt="")
        self.assertEqual((req.prompt, req.boxes), ("", []))
        self.assertNotIn("sent_prompt", gen)

    def test_explicit_flux3_prompt_citing_the_reference_is_kept(self):
        from genfill import boxes
        boxes.add_box(self.g, (800, 450, 1000, 600))
        boxes.set_prompt(self.g, 1, "a hat")
        req, _gen = self._generate_with_layout("flux3", prompt="Put <obj_1> on <ref_image_0>")
        self.assertEqual(req.prompt, "Put <obj_1> on <ref_image_0>")
        req, _gen = self._generate_with_layout("flux3_fields", prompt="Put <obj_1> on him")
        self.assertEqual(req.prompt, "Put <obj_1> on him")

    def test_missing_source(self):
        self.g.setInput(0, None)
        self.assertFalse(engine.generate(self.g, "x"))
        self.assertIn("Source", engine.status(self.g)["error"])


class DevtoolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = ""

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_smoke_test_runs_and_reports(self):
        import json
        from genfill import devtools
        out = os.path.join(self.tmp, "out")
        passed = devtools.run_smoke_test(out, show_result=False)
        with open(os.path.join(out, "report.json")) as fh:
            report = json.load(fh)
        failed = [s for s in report["steps"] if not s["ok"]]
        self.assertTrue(passed, failed)
        for name in ("comp.png", "fill.png", "source.png", "mask.png", "var_01.png"):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        self.assertIn("knob_inventory", report)
        self.assertTrue(report["values"]["gf_has_result"])
        # Running twice replaces the container instead of piling up.
        devtools.run_smoke_test(out, show_result=False)
        names = [n.name() for n in nuke.allNodes()]
        self.assertEqual(names.count(devtools.CONTAINER), 1)

    def test_build_info_dev_and_installed(self):
        import genfill
        self.assertIn("live from repo", genfill.build_label())
        path = genfill.BUILD_INFO_PATH
        try:
            with open(path, "w") as fh:
                fh.write('{"commit": "abc1234", "dirty": true, "built": "2026-10-12 20:41",'
                         ' "mode": "copy", "source": "%s"}' % self.tmp.replace("\\", "/"))
            self.assertEqual(genfill.build_label(), "GenFill %s (abc1234*, 2026-10-12 20:41)"
                             % genfill.__version__)
            self.assertEqual(genfill.repo_root(), self.tmp.replace("\\", "/"))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
