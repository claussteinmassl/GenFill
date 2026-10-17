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
            flags = model.flags(model.index(i, 0))
            self.assertFalse(flags & Qt.ItemIsSelectable)
            self.assertFalse(flags & Qt.ItemIsEnabled)
            self.assertTrue(combo.itemData(i, Qt.FontRole).bold())
        idx = combo.findData("imagen-nano-banana-2")
        self.assertTrue(combo.itemText(idx).startswith("  "))
        self.assertTrue(model.flags(model.index(idx, 0)) & Qt.ItemIsSelectable)

    def test_typed_family_name_does_not_reset_the_model(self):
        w = self._panel("Magnific")
        combo = w.version
        combo.setEditText("Google")                 # a family header's text
        w._on_version_typed()
        self.assertEqual(w.ctl.node["gf_model"].value(), "Google")
        idx = combo.findData("imagen-nano-banana-2")
        combo.setEditText(combo.itemText(idx).strip())   # a model label maps to its id
        w._on_version_typed()
        self.assertEqual(w.ctl.node["gf_model"].value(), "imagen-nano-banana-2")

    def test_single_family_backend_has_no_headers(self):
        combo = self._panel("Offline").version
        self.assertEqual([combo.itemText(i) for i in range(combo.count())
                          if combo.itemData(i) is None and combo.itemText(i)], [])


@unittest.skipUnless(HAVE_QT, "PySide6/PySide2 not installed")
class RegionPrompts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import fake_nuke
        cls.nuke = fake_nuke.install()
        from genfill.ui.qt import QtCore, QtGui, QtWidgets
        cls.QtGui, cls.QtWidgets = QtGui, QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        from genfill import boxes, node as gfnode
        self.boxes = boxes
        self.mod = _real_panel()
        self.g = gfnode.create()
        self.g.setInput(0, self.nuke.nodes.Read(name="Plate"))
        self.g["gf_backend"].setValue([l for l in gfnode.backend_labels() if "fal" in l][0])
        self.g["gf_model"].setValue("blackforestlabs/flux-3/edit-image")
        boxes.add_box(self.g, (100, 100, 400, 400))
        boxes.add_box(self.g, (600, 300, 900, 700))
        boxes.set_prompt(self.g, 1, "a red balloon")
        boxes.set_enabled(self.g, 2, False)
        self.w = self.mod.GenFillPanel(self.mod.NukeController(self.g))

    def tearDown(self):
        self.w._timer.stop()
        self.w.close()
        self.w.deleteLater()

    def _rows(self):
        layout = self.w.box_list
        return [layout.itemAt(i).widget() for i in range(layout.count())]

    def test_section_shows_one_row_per_box(self):
        rows = self._rows()
        self.assertEqual([r.n for r in rows], [1, 2])
        self.assertEqual([r.tag.text() for r in rows], ["<obj_1>", "<obj_2>"])
        self.assertEqual(rows[0].edit.text(), "a red balloon")
        self.assertTrue(rows[0].enabled.isChecked())
        self.assertFalse(rows[1].enabled.isChecked())
        self.assertIn(self.boxes.palette_hex(2), rows[1].swatch.styleSheet())
        self.assertTrue(self.w.regions_body.isEnabled())
        self.assertTrue(self.w.show_boxes.isChecked())
        self.assertIn("<obj_N>", self.w.regions_hint.text())

    def test_add_and_remove_rows(self):
        self.w.add_box_btn.click()
        self.assertEqual([r.n for r in self._rows()], [1, 2, 3])
        self.assertEqual([b.n for b in self.boxes.list_boxes(self.g)], [1, 2, 3])
        first = self._rows()[0]
        self._rows()[1].delete.click()
        self.assertEqual([r.n for r in self._rows()], [1, 3])
        self.assertEqual([b.n for b in self.boxes.list_boxes(self.g)], [1, 3])
        self.assertIs(self._rows()[0], first)       # surviving rows are kept, not rebuilt
        self.w.add_box_btn.click()                  # the lowest free number comes back
        self.assertEqual([r.n for r in self._rows()], [1, 2, 3])

    def test_edits_commit_to_the_box(self):
        row = self._rows()[1]
        row.edit.setText("a kite")
        row.edit.editingFinished.emit()
        row.enabled.setChecked(True)
        info = self.boxes.list_boxes(self.g)[1]
        self.assertEqual((info.prompt, info.enabled), ("a kite", True))

    def test_generate_commits_text_that_editing_finished_never_saved(self):
        # macOS: clicking Generate does not take the focus, so no editingFinished fires.
        rows = self._rows()
        rows[0].edit.setText("a red umbrella")
        rows[1].edit.setText("a black dog")
        self.w.prompt.setText("rainy street")
        seen = []
        self.w.ctl.generate = lambda prompt=None: seen.append(
            (self.g["gf_prompt"].value(), [b.prompt for b in self.boxes.list_boxes(self.g)]))
        self.w._on_generate()
        self.assertEqual(seen, [("rainy street", ["a red umbrella", "a black dog"])])

    def test_node_changes_show_up_but_not_over_a_focused_edit(self):
        self.w.show()
        self.w.activateWindow()
        row = self._rows()[0]
        row.edit.setFocus()
        self.app.processEvents()
        if not row.edit.hasFocus():
            self.skipTest("no keyboard focus on this platform plugin")
        row.edit.setText("typing...")
        self.boxes.set_prompt(self.g, 1, "changed on the node")
        self.boxes.set_prompt(self.g, 2, "other box")
        self.w._sync()
        self.assertEqual(row.edit.text(), "typing...")
        self.assertEqual(self._rows()[1].edit.text(), "other box")
        self.w.prompt.setFocus()
        self.app.processEvents()
        self.assertEqual(self.boxes.list_boxes(self.g)[0].prompt, "typing...")  # focus-out commit

    def test_tag_inserts_at_the_prompt_cursor(self):
        self.w.prompt.setText("put the balloonhere")
        cursor = self.w.prompt.textCursor()
        cursor.setPosition(len("put the balloon"))
        self.w.prompt.setTextCursor(cursor)
        self._rows()[0].tag.click()
        self.assertEqual(self.w.prompt.text(), "put the balloon <obj_1> here")
        self.assertEqual(self.g["gf_prompt"].value(), "put the balloon <obj_1> here")

    def test_show_boxes_toggle(self):
        self.w.show_boxes.setChecked(False)
        self.assertFalse(self.g["gf_show_boxes"].value())
        self.g["gf_show_boxes"].setValue(True)
        self.w._sync()
        self.assertTrue(self.w.show_boxes.isChecked())

    def test_disabled_for_models_without_layouts_and_boxes_are_kept(self):
        self.g["gf_model"].setValue("fal-ai/nano-banana-pro/edit")
        self.w._sync()
        self.assertFalse(self.w.regions_body.isEnabled())
        self.assertEqual(self.w.regions_body.toolTip(), self.mod.REGION_UNSUPPORTED)
        self.assertEqual([r.n for r in self._rows()], [1, 2])
        self.assertEqual(len(self.boxes.list_boxes(self.g)), 2)
        self.assertTrue(self.w.show_boxes.isEnabled())
        self.g["gf_model"].setValue("ideogram/v4.5/edit")
        self.w._sync()
        self.assertTrue(self.w.regions_body.isEnabled())
        self.assertEqual(self.w.regions_body.toolTip(), "")


if __name__ == "__main__":
    unittest.main()
