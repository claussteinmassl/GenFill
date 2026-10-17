"""The panel's NukeController (no Qt needed: the Qt shim is stubbed for the import)."""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.dirname(__file__))
import fake_nuke  # noqa: E402

nuke = fake_nuke.install()


class _Stub(object):
    """Stands in for any Qt class, enum or signal at import time."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *args, **kwargs):
        return _Stub()


class _Namespace(object):
    def __getattr__(self, name):
        return type(name, (_Stub,), {})


def _import_panel():
    import genfill.ui
    if "genfill.ui.panel" in sys.modules:
        return sys.modules["genfill.ui.panel"]
    real = sys.modules.get("genfill.ui.qt")
    qt = types.ModuleType("genfill.ui.qt")
    qt.QtCore, qt.QtGui, qt.QtWidgets, qt.Qt = _Namespace(), _Namespace(), _Namespace(), _Stub()
    qt.QT6 = False
    qt.exec_ = lambda obj: None
    sys.modules["genfill.ui.qt"] = qt
    try:
        import genfill.ui.panel as panel
    finally:
        if real is not None:
            sys.modules["genfill.ui.qt"] = real
        else:
            del sys.modules["genfill.ui.qt"]
        if hasattr(genfill.ui, "qt"):
            delattr(genfill.ui, "qt")
    return panel


panel = _import_panel()

from genfill import boxes, node as gfnode  # noqa: E402


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = os.path.join(self.tmp, "shot.nk")
        self.g = gfnode.create()
        self.g["gf_backend"].setValue([l for l in gfnode.backend_labels() if "Offline" in l][0])
        self.ctl = panel.NukeController(self.g)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_signature_follows_the_source_plate(self):
        empty = self.ctl.signature()
        self.assertIsNone(self.ctl.source_format())
        plate = nuke.nodes.Read(name="Plate")
        self.g.setInput(0, plate)
        connected = self.ctl.signature()
        hd_size = self.ctl.size_text()
        self.assertNotEqual(empty, connected)
        self.assertTrue(hd_size)
        self.assertEqual(self.ctl.source_format(), ("Plate", 1920, 1080))
        plate._format = fake_nuke.Format(4096, 2160)
        self.assertNotEqual(self.ctl.signature(), connected)
        self.assertNotEqual(self.ctl.size_text(), hd_size)

    def test_model_catalogue_entries_carry_the_family(self):
        self.g["gf_backend"].setValue([l for l in gfnode.backend_labels() if "Magnific" in l][0])
        default, entries = self.ctl.model_catalogue()
        self.assertTrue(entries)
        self.assertTrue(all(len(e) == 4 for e in entries))
        fam = dict((e[0], e[3]) for e in entries)
        self.assertEqual(fam["retouch-auto"], "Magnific")
        self.assertEqual(fam["retouch-imagen-nano-banana-2"], "Google")
        self.assertEqual(fam["seedream-5-pro"], "ByteDance")

    def test_cost_snapshot_uses_the_typed_prompt(self):
        self.g.setInput(0, nuke.nodes.Read(name="Plate"))
        self.g["gf_prompt"].setValue("")
        self.assertEqual(self.ctl.cost_snapshot()["prompt"], "")
        self.assertEqual(self.ctl.cost_snapshot("a red crate")["prompt"], "a red crate")
        self.g["gf_prompt"].setValue("saved")
        self.assertEqual(self.ctl.cost_snapshot()["prompt"], "saved")
        self.assertEqual(self.ctl.cost_snapshot("")["prompt"], "")


class BoxControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake_nuke._root.script_path = os.path.join(self.tmp, "shot.nk")
        self.g = gfnode.create()
        self.g.setInput(0, nuke.nodes.Read(name="Plate"))
        self.g["gf_backend"].setValue([l for l in gfnode.backend_labels() if "fal" in l][0])
        self.g["gf_model"].setValue("blackforestlabs/flux-3/edit-image")
        self.ctl = panel.NukeController(self.g)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_add_edit_toggle_remove(self):
        self.assertEqual(self.ctl.boxes(), [])
        self.assertEqual(self.ctl.add_box(), 1)
        self.assertEqual(self.ctl.add_box(), 2)
        self.assertTrue(self.ctl.set_box_prompt(2, "a red balloon"))
        self.assertTrue(self.ctl.set_box_enabled(1, False))
        listed = [(b.n, b.prompt, b.enabled) for b in self.ctl.boxes()]
        self.assertEqual(listed, [(1, "", False), (2, "a red balloon", True)])
        self.assertTrue(self.ctl.remove_box(1))
        self.assertEqual([b.n for b in self.ctl.boxes()], [2])
        self.assertFalse(self.ctl.set_box_prompt(1, "gone"))
        self.assertFalse(self.ctl.set_box_enabled(1, True))
        self.assertFalse(self.ctl.remove_box(1))
        self.assertEqual(self.ctl.box_colour(2), boxes.palette_hex(2))

    def test_unchanged_text_is_not_written(self):
        self.ctl.add_box()
        self.ctl.set_box_prompt(1, "sky")
        knob = self.g.node("GenFillBox1")["gf_box_prompt"]
        with mock.patch.object(knob, "setValue") as set_value:
            self.ctl.set_box_prompt(1, "sky")
        set_value.assert_not_called()

    def test_signature_follows_the_box_list(self):
        before = self.ctl.signature()
        self.ctl.add_box()
        added = self.ctl.signature()
        self.assertNotEqual(before, added)
        self.ctl.set_box_prompt(1, "a cat")
        self.assertNotEqual(self.ctl.signature(), added)
        texted = self.ctl.signature()
        self.ctl.set_box_enabled(1, False)
        self.assertNotEqual(self.ctl.signature(), texted)
        disabled = self.ctl.signature()
        self.ctl.set_show_boxes(False)
        self.assertNotEqual(self.ctl.signature(), disabled)

    def test_show_boxes_toggle(self):
        self.assertTrue(self.ctl.show_boxes())
        self.ctl.set_show_boxes(False)
        self.assertFalse(self.g["gf_show_boxes"].value())
        self.assertFalse(self.ctl.show_boxes())

    def test_layout_supported_follows_backend_and_model(self):
        self.assertTrue(self.ctl.layout_supported())
        self.g["gf_model"].setValue("ideogram/v4.5/edit")
        self.assertTrue(self.ctl.layout_supported())
        self.g["gf_model"].setValue("fal-ai/nano-banana-pro/edit")
        self.assertFalse(self.ctl.layout_supported())
        self.g["gf_backend"].setValue([l for l in gfnode.backend_labels() if "Offline" in l][0])
        self.g["gf_model"].setValue("")
        self.assertFalse(self.ctl.layout_supported())


if __name__ == "__main__":
    unittest.main()
