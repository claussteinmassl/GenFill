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
        from genfill.ui.panel import PromptEdit
        cls.PromptEdit = PromptEdit

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


if __name__ == "__main__":
    unittest.main()
