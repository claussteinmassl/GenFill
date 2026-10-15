"""The panel's NukeController (no Qt needed: the Qt shim is stubbed for the import)."""
import os
import shutil
import sys
import tempfile
import types
import unittest

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

from genfill import node as gfnode  # noqa: E402


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

    def test_cost_snapshot_uses_the_typed_prompt(self):
        self.g.setInput(0, nuke.nodes.Read(name="Plate"))
        self.g["gf_prompt"].setValue("")
        self.assertEqual(self.ctl.cost_snapshot()["prompt"], "")
        self.assertEqual(self.ctl.cost_snapshot("a red crate")["prompt"], "a red crate")
        self.g["gf_prompt"].setValue("saved")
        self.assertEqual(self.ctl.cost_snapshot()["prompt"], "saved")
        self.assertEqual(self.ctl.cost_snapshot("")["prompt"], "")


if __name__ == "__main__":
    unittest.main()
