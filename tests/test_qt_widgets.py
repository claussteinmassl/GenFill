"""Real-Qt widget tests (offscreen); skipped when neither PySide6 nor PySide2 is installed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)

try:
    import PySide6  # noqa: F401
    HAVE_QT = True
except ImportError:
    try:
        import PySide2  # noqa: F401
        HAVE_QT = True
    except ImportError:
        HAVE_QT = False


def _real_panel():
    """The panel module bound to real Qt (test_panel_controller caches a stub-Qt import)."""
    import genfill.ui
    import genfill.ui.qt  # noqa: F401
    stale = sys.modules.get("genfill.ui.panel")
    if stale is not None and type(stale.QtGui).__name__ == "_Namespace":
        del sys.modules["genfill.ui.panel"]
        if hasattr(genfill.ui, "panel"):
            delattr(genfill.ui, "panel")
    from genfill.ui import panel
    return panel


@unittest.skipUnless(HAVE_QT, "PySide6/PySide2 not installed")
class PromptEditKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import fake_nuke
        fake_nuke.install()
        from genfill.ui.qt import QtCore, QtGui, QtWidgets
        cls.QtCore, cls.QtGui = QtCore, QtGui
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.PromptEdit = _real_panel().PromptEdit

    def _press(self, edit, key, mod=None):
        Qt = self.QtCore.Qt
        event = self.QtGui.QKeyEvent(self.QtCore.QEvent.KeyPress, key,
                                     mod if mod is not None else Qt.NoModifier)
        edit.keyPressEvent(event)

    def test_enter_generates_and_shift_enter_adds_a_line(self):
        Qt = self.QtCore.Qt
        edit = self.PromptEdit()
        hits = []
        edit.submitted.connect(lambda: hits.append(1))
        edit.setText("a")
        edit.moveCursor(self.QtGui.QTextCursor.End)
        self._press(edit, Qt.Key_Return, Qt.ShiftModifier)
        self.assertEqual(edit.text(), "a\n")
        self.assertEqual(hits, [])
        self._press(edit, Qt.Key_Return)
        self._press(edit, Qt.Key_Enter)
        self._press(edit, Qt.Key_Return, Qt.ControlModifier)
        self.assertEqual(hits, [1, 1, 1])
        self.assertEqual(edit.text(), "a\n")


@unittest.skipUnless(HAVE_QT, "PySide6/PySide2 not installed")
class VersionMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import fake_nuke
        fake_nuke.install()
        from genfill.ui.qt import QtCore, QtWidgets
        cls.Qt = QtCore.Qt
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _panel(self, backend):
        from genfill import node as gfnode
        panel = _real_panel()
        g = gfnode.create()
        g["gf_backend"].setValue([l for l in gfnode.backend_labels() if backend in l][0])
        return panel.GenFillPanel(panel.NukeController(g))

    def test_family_headers_are_not_selectable_and_models_are_indented(self):
        Qt = self.Qt
        combo = self._panel("Magnific").version
        model = combo.model()
        headers = [i for i in range(combo.count()) if combo.itemData(i) is None
                   and combo.itemText(i)]
        self.assertTrue(headers)
        names = [combo.itemText(i) for i in headers]
        self.assertEqual(names[0], "Magnific")
        self.assertIn("Google", names)
        for i in headers:
            flags = model.item(i).flags()
            self.assertFalse(flags & Qt.ItemIsSelectable)
            self.assertFalse(flags & Qt.ItemIsEnabled)
            self.assertTrue(model.item(i).font().bold())
        idx = combo.findData("imagen-nano-banana-2")
        self.assertTrue(combo.itemText(idx).startswith("  "))
        self.assertTrue(model.item(idx).flags() & Qt.ItemIsSelectable)

    def test_single_family_backend_has_no_headers(self):
        combo = self._panel("Offline").version
        self.assertEqual([combo.itemText(i) for i in range(combo.count())
                          if combo.itemData(i) is None and combo.itemText(i)], [])


if __name__ == "__main__":
    unittest.main()
