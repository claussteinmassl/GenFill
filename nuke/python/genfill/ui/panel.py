"""Photoshop-style Generative Fill panel embedded in the node's properties.

Layout follows Photoshop's Generative Fill: a contextual task bar (prompt,
variation arrows, Generate), the model picker, and the Variations grid of
thumbnails from every generation, newest first.

The widget talks to a *controller* rather than to Nuke directly, so the UI can
be previewed and tested outside Nuke (see tests/render_panel_preview.py).
"""
from __future__ import annotations

import math
import os

from .qt import QtCore, QtGui, QtWidgets, Qt
from . import style

def _safe(fn):
    """Qt swallows exceptions raised in slots; log them and show them instead."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            try:
                from .. import log as gflog
                gflog.exception("Panel action %s failed", fn.__name__)
                where = gflog.log_path()
            except Exception:
                where = "the Script Editor"
            self._show_local_error("%s failed: %s (details in %s)" % (fn.__name__.strip("_"),
                                                                    exc, where))
    return wrapper


THUMB_W = 132
THUMB_H = 92
COLUMNS = 3
POLL_MS = 300


# --------------------------------------------------------------------------
# controller
# --------------------------------------------------------------------------

class NukeController(object):
    """Adapter between the panel and a GenerativeFill node."""

    def __init__(self, node):
        import nuke  # noqa: F401  (only inside Nuke)
        from .. import engine, node as gfnode
        self.node = node
        self._engine = engine
        self._gfnode = gfnode

    def alive(self):
        try:
            self.node.name()
            return True
        except ValueError:
            return False

    def signature(self):
        n = self.node
        return (n["gf_state"].value(), n["gf_backend"].value(), n["gf_prompt"].value(),
                n["gf_model"].value(),
                tuple(sorted(self._engine.status(n).items())))

    def prompt(self):
        return self.node["gf_prompt"].value()

    def set_prompt(self, text):
        if text != self.node["gf_prompt"].value():
            self.node["gf_prompt"].setValue(text)

    def backend_labels(self):
        return self._gfnode.backend_labels()

    def backend_label(self):
        return self.node["gf_backend"].value()

    def set_backend_label(self, label):
        self.node["gf_backend"].setValue(label)
        self.set_model_version(self._gfnode.model_for_backend(
            self.node["gf_model"].value(), self._gfnode.backend_for(self.node)))

    def backend_description(self):
        return self._gfnode.backend_for(self.node).description

    def model_version(self):
        return self.node["gf_model"].value().strip()

    def set_model_version(self, text):
        text = (text or "").strip()
        if text != self.node["gf_model"].value().strip():
            self.node["gf_model"].setValue(text)

    def model_catalogue(self):
        """(default_model, [(id, label, note), ...]) of the current backend."""
        b = self._gfnode.backend_for(self.node)
        return b.default_model, [(m.id, m.label, m.note) for m in b.catalogue]

    def status(self):
        return self._engine.status(self.node)

    def thumbnails(self):
        return self._engine.thumbnails(self.node)

    def generate(self, prompt):
        self._engine.generate(self.node, prompt)

    def cancel(self):
        self._engine.cancel(self.node)

    def select(self, gi, vi):
        self._engine.select(self.node, gi, vi)

    def step(self, delta):
        self._engine.step(self.node, delta)

    def delete(self, gi, vi):
        self._engine.delete_variation(self.node, gi, vi)

    def reveal(self, path):
        folder = os.path.dirname(path)
        url = QtCore.QUrl.fromLocalFile(folder)
        QtGui.QDesktopServices.openUrl(url)

    def refresh(self):
        try:
            self._engine.refresh_result(self.node)
        except Exception as exc:
            print("GenFill: %s" % exc)


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------

def _star(painter, cx, cy, r, inner=0.28):
    path = QtGui.QPainterPath()
    pts = []
    for i in range(8):
        ang = i * 3.14159265 / 4.0
        rad = r if i % 2 == 0 else r * inner
        pts.append(QtCore.QPointF(cx + rad * math.sin(ang), cy - rad * math.cos(ang)))
    path.moveTo(pts[0])
    for p in pts[1:]:
        path.lineTo(p)
    path.closeSubpath()
    painter.drawPath(path)


def sparkle_pixmap(size=16, color="#ffffff"):
    """Generative 'sparkle' glyph (one large + two small four-point stars)."""
    dpr = 2
    pm = QtGui.QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QtGui.QColor(color))
    s = size * dpr
    _star(p, s * 0.42, s * 0.58, s * 0.40)
    _star(p, s * 0.80, s * 0.22, s * 0.18)
    _star(p, s * 0.82, s * 0.80, s * 0.11)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


# --------------------------------------------------------------------------
# thumbnail
# --------------------------------------------------------------------------

class Thumbnail(QtWidgets.QLabel):
    clicked = QtCore.Signal(int, int)
    menuRequested = QtCore.Signal(int, int, QtCore.QPoint)

    _cache = {}

    def __init__(self, info, parent=None):
        super(Thumbnail, self).__init__(parent)
        self.info = info
        self.gi, self.vi = info["gi"], info["vi"]
        self._hover = False
        self.setFixedSize(THUMB_W + 6, THUMB_H + 6)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        prompt = info.get("prompt") or "(empty prompt: remove / fill from surroundings)"
        self.setToolTip("%s\n%s  seed %s  frame %s" % (prompt, info.get("backend", ""),
                                                       info.get("seed", ""), info.get("frame", "")))
        pm = self._load(info["path"])
        if pm is not None:
            self.setPixmap(pm)
        else:
            self.setText("missing")
        self._apply()

    @classmethod
    def _load(cls, path):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        key = (path, mtime)
        if key not in cls._cache:
            img = QtGui.QPixmap(path)
            if img.isNull():
                return None
            cls._cache[key] = img.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
        return cls._cache[key]

    def _apply(self):
        if self.info.get("selected"):
            border = style.SELECT
        elif self._hover:
            border = "#7a7a7a"
        else:
            border = "transparent"
        self.setStyleSheet("QLabel { background: #1a1a1a; border: 2px solid %s; "
                           "border-radius: 6px; color: #888; font-size: 11px; }" % border)

    def enterEvent(self, event):
        self._hover = True
        self._apply()
        super(Thumbnail, self).enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._apply()
        super(Thumbnail, self).leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.gi, self.vi)
        elif event.button() == Qt.RightButton:
            pos = event.globalPosition().toPoint() if hasattr(event, "globalPosition") \
                else event.globalPos()
            self.menuRequested.emit(self.gi, self.vi, pos)


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------

class PromptEdit(QtWidgets.QPlainTextEdit):
    """Multi-line prompt field: 3 lines tall, grows to 8 with its content.

    Enter inserts a newline; Ctrl+Enter (Cmd+Enter on macOS, which Qt maps to
    the control modifier) emits ``submitted``. ``committed`` is emitted when
    the field loses focus so the text can be written to the node.
    """

    MIN_LINES = 3
    MAX_LINES = 8
    submitted = QtCore.Signal()
    committed = QtCore.Signal()

    def __init__(self, parent=None):
        super(PromptEdit, self).__init__(parent)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.textChanged.connect(self._fit_height)
        self._fit_height()

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText(text)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and event.modifiers() & Qt.ControlModifier):
            self.submitted.emit()
            return
        super(PromptEdit, self).keyPressEvent(event)

    def focusOutEvent(self, event):
        super(PromptEdit, self).focusOutEvent(event)
        self.committed.emit()

    def resizeEvent(self, event):
        super(PromptEdit, self).resizeEvent(event)
        self._fit_height()

    def _fit_height(self):
        """Size to the content (wrapped lines included), within the line limits."""
        lines = int(self.document().documentLayout().documentSize().height())
        lines = max(self.MIN_LINES, min(self.MAX_LINES, lines))
        margin = int(self.document().documentMargin()) * 2 + 2 * self.frameWidth()
        height = lines * self.fontMetrics().lineSpacing() + margin
        if self.height() != height:
            self.setFixedHeight(height)


class GenFillPanel(QtWidgets.QWidget):
    """The widget returned to Nuke's PyCustom_Knob."""

    def __init__(self, controller=None, parent=None):
        super(GenFillPanel, self).__init__(parent)
        if controller is None:
            import nuke
            controller = NukeController(nuke.thisNode())
        self.ctl = controller
        self._sig = None
        self._thumb_sig = None
        self.setObjectName("GenFillPanel")
        self.setStyleSheet(style.STYLESHEET)
        self._build()
        self.ctl.refresh()
        self._sync(force=True)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._sync)
        self._timer.start(POLL_MS)

    # Nuke PyCustom_Knob protocol ---------------------------------------
    def makeUI(self):
        return self

    def updateValue(self):
        self._sync(force=True)

    # build ----------------------------------------------------------------
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(4, 6, 4, 6)
        root.setSpacing(8)

        # Header: sparkle + title + model picker
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        icon = QtWidgets.QLabel()
        icon.setPixmap(sparkle_pixmap(16, "#e6e6e6"))
        head.addWidget(icon)
        title = QtWidgets.QLabel("Generative Fill")
        title.setObjectName("gfTitle")
        head.addWidget(title)
        head.addStretch(1)
        model_lbl = QtWidgets.QLabel("Model")
        model_lbl.setObjectName("gfMuted")
        head.addWidget(model_lbl)
        self.model = QtWidgets.QComboBox()
        self.model.setObjectName("gfModel")
        self.model.addItems(self.ctl.backend_labels())
        self.model.currentTextChanged.connect(self._on_model)
        head.addWidget(self.model)
        self.version = QtWidgets.QComboBox()
        self.version.setObjectName("gfVersion")
        self.version.setEditable(True)
        self.version.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.version.setMinimumContentsLength(14)
        self.version.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.version.activated.connect(self._on_version)
        self.version.lineEdit().editingFinished.connect(self._on_version_typed)
        self._version_backend = None
        head.addWidget(self.version)
        root.addLayout(head)

        # Contextual task bar
        bar = QtWidgets.QFrame()
        bar.setObjectName("gfBar")
        shadow = QtWidgets.QGraphicsDropShadowEffect(bar)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 2)
        shadow.setColor(QtGui.QColor(0, 0, 0, 140))
        bar.setGraphicsEffect(shadow)
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(10, 5, 5, 5)
        bl.setSpacing(4)
        self.prev_btn = QtWidgets.QToolButton()
        self.prev_btn.setObjectName("gfNav")
        self.prev_btn.setText(u"‹")
        self.prev_btn.setToolTip("Previous variation")
        self.prev_btn.clicked.connect(lambda: self.ctl.step(-1))
        self.counter = QtWidgets.QLabel("")
        self.counter.setObjectName("gfCounter")
        self.counter.setAlignment(Qt.AlignCenter)
        self.next_btn = QtWidgets.QToolButton()
        self.next_btn.setObjectName("gfNav")
        self.next_btn.setText(u"›")
        self.next_btn.setToolTip("Next variation")
        self.next_btn.clicked.connect(lambda: self.ctl.step(1))
        self.prompt = PromptEdit()
        self.prompt.setObjectName("gfPrompt")
        self.prompt.setPlaceholderText("Describe what you want to generate (optional)")
        self.prompt.setToolTip("Leave empty to remove the selected content and fill from "
                               "the surroundings.\nCtrl+Enter = Generate, Enter = new line")
        self.prompt.submitted.connect(self._on_generate)
        self.prompt.committed.connect(lambda: self.ctl.set_prompt(self.prompt.text()))
        self.generate_btn = QtWidgets.QPushButton("Generate")
        self.generate_btn.setObjectName("gfGenerate")
        self.generate_btn.setIcon(QtGui.QIcon(sparkle_pixmap(14, "#ffffff")))
        self.generate_btn.setIconSize(QtCore.QSize(14, 14))
        self.generate_btn.setCursor(Qt.PointingHandCursor)
        self.generate_btn.clicked.connect(self._on_generate)
        bl.addWidget(self.prev_btn)
        bl.addWidget(self.counter)
        bl.addWidget(self.next_btn)
        self.nav_sep = QtWidgets.QFrame()
        self.nav_sep.setFrameShape(QtWidgets.QFrame.VLine)
        self.nav_sep.setStyleSheet("color: #444;")
        bl.addWidget(self.nav_sep)
        bl.addWidget(self.prompt, 1)
        bl.setAlignment(self.prompt, Qt.AlignVCenter)
        bl.addWidget(self.generate_btn)
        root.addWidget(bar)

        # Progress / status
        self.progress = QtWidgets.QProgressBar()
        self.progress.setObjectName("gfProgress")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        root.addWidget(self.progress)
        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("gfMuted")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # Variations
        vh = QtWidgets.QHBoxLayout()
        sec = QtWidgets.QLabel("VARIATIONS")
        sec.setObjectName("gfSection")
        vh.addWidget(sec)
        vh.addStretch(1)
        self.delete_btn = QtWidgets.QToolButton()
        self.delete_btn.setObjectName("gfIcon")
        self.delete_btn.setText(u"\U0001F5D1  Delete")
        self.delete_btn.setToolTip("Delete the selected variation")
        self.delete_btn.clicked.connect(self._on_delete_selected)
        vh.addWidget(self.delete_btn)
        root.addLayout(vh)

        self.grid_host = QtWidgets.QWidget()
        self.grid_host.setObjectName("gfGrid")
        self.grid = QtWidgets.QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(6)
        self.grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setObjectName("gfScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.grid_host)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(THUMB_H + 12)
        self.scroll.setMaximumHeight(3 * (THUMB_H + 12))
        root.addWidget(self.scroll)

        self.empty = QtWidgets.QFrame()
        self.empty.setObjectName("gfEmpty")
        el = QtWidgets.QVBoxLayout(self.empty)
        el.setContentsMargins(12, 14, 12, 14)
        msg = QtWidgets.QLabel("Click \u270f Draw Selection below and draw around an area in "
                               "the Viewer, describe what you want, and click Generate.\n"
                               "Leave the prompt empty to remove the selected content.")
        msg.setObjectName("gfMuted")
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignCenter)
        el.addWidget(msg)
        root.addWidget(self.empty)

        self.footer = QtWidgets.QLabel("")
        self.footer.setObjectName("gfMuted")
        root.addWidget(self.footer)

        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        self.setMinimumWidth(3 * (THUMB_W + 12))

    # events -----------------------------------------------------------------
    @_safe
    def _on_model(self, label):
        if label and label != self.ctl.backend_label():
            self.ctl.set_backend_label(label)

    @_safe
    def _on_version(self, index):
        """A catalogue entry was picked: store its id ("" = backend default)."""
        data = self.version.itemData(index)
        self.ctl.set_model_version(data if data is not None else self.version.itemText(index))

    @_safe
    def _on_version_typed(self, *args):
        """Free text: a displayed catalogue label maps back to its id."""
        text = self.version.currentText().strip()
        idx = self.version.findText(text)
        if idx >= 0:
            text = self.version.itemData(idx) or ""
        self.ctl.set_model_version(text)

    def _fill_version(self):
        """Rebuild the version items for the current backend and show gf_model."""
        default, entries = self.ctl.model_catalogue()
        key = (self.ctl.backend_label(), default, tuple(e[0] for e in entries))
        if key != self._version_backend:
            self._version_backend = key
            self.version.blockSignals(True)
            self.version.clear()
            self.version.addItem("Default (%s)" % default if default else "Default", "")
            for mid, label, note in entries:
                self.version.addItem(label, mid)
                if note:
                    self.version.setItemData(self.version.count() - 1, note, Qt.ToolTipRole)
            self.version.blockSignals(False)
        if self.version.lineEdit().hasFocus():
            return
        current = self.ctl.model_version()
        idx = self.version.findData(current)
        self.version.blockSignals(True)
        if idx >= 0:
            self.version.setCurrentIndex(idx)
        else:
            self.version.setEditText(current)
        self.version.blockSignals(False)
        self.version.setToolTip("Model version (id); pick one or type any id the provider "
                                "offers. Default = backend default.")

    @_safe
    def _on_generate(self, *args):
        if self.ctl.status().get("running"):
            self.ctl.cancel()
            return
        self.ctl.set_prompt(self.prompt.text())
        # Immediate feedback: preparing the crop renders on the main thread.
        self._local_error = ""
        self.status.setObjectName("gfMuted")
        self.status.setText("Preparing selection...")
        self.status.setVisible(True)
        self.generate_btn.setEnabled(False)
        QtWidgets.QApplication.processEvents()
        try:
            self.ctl.generate(self.prompt.text())
        finally:
            self.generate_btn.setEnabled(True)
        self._sync(force=True)

    @_safe
    def _on_delete_selected(self, *args):
        for t in self.ctl.thumbnails():
            if t["selected"]:
                self.ctl.delete(t["gi"], t["vi"])
                break
        self._sync(force=True)

    @_safe
    def _on_thumb_menu(self, gi, vi, pos):
        info = next((t for t in self.ctl.thumbnails() if t["gi"] == gi and t["vi"] == vi), None)
        if info is None:
            return
        menu = QtWidgets.QMenu(self)
        use = menu.addAction("Use this variation")
        reuse = menu.addAction("Use this prompt")
        reveal = menu.addAction("Reveal file")
        menu.addSeparator()
        delete = menu.addAction("Delete variation")
        act = menu.exec(pos) if hasattr(menu, "exec") else menu.exec_(pos)
        if act == use:
            self.ctl.select(gi, vi)
        elif act == reuse:
            self.prompt.setText(info.get("prompt", ""))
            self.ctl.set_prompt(info.get("prompt", ""))
        elif act == reveal:
            self.ctl.reveal(info["path"])
        elif act == delete:
            self.ctl.delete(gi, vi)
        self._sync(force=True)

    # sync -------------------------------------------------------------------
    def _show_local_error(self, text):
        self._local_error = text
        self.status.setObjectName("gfError")
        self.status.setText(text)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setVisible(True)

    def _sync(self, force=False):
        try:
            self._sync_impl(force)
        except Exception as exc:
            key = str(exc)
            if key != getattr(self, "_last_sync_error", None):
                self._last_sync_error = key
                try:
                    from .. import log as gflog
                    gflog.exception("Panel refresh failed")
                except Exception:
                    pass
                self._show_local_error("Panel refresh failed: %s" % exc)

    def _sync_impl(self, force=False):
        if not self.ctl.alive():
            self._timer.stop() if hasattr(self, "_timer") else None
            return
        sig = self.ctl.signature()
        if sig == self._sig and not force:
            return
        self._sig = sig

        # model
        label = self.ctl.backend_label()
        if self.model.currentText() != label:
            self.model.blockSignals(True)
            self.model.setCurrentText(label)
            self.model.blockSignals(False)
        self.model.setToolTip(self.ctl.backend_description())
        self._fill_version()

        # prompt (don't fight the user while typing)
        if not self.prompt.hasFocus() and self.prompt.text() != self.ctl.prompt():
            self.prompt.setText(self.ctl.prompt())

        # status
        st = self.ctl.status()
        running = bool(st.get("running"))
        self.generate_btn.setText("Cancel" if running else "Generate")
        self.generate_btn.setProperty("busy", "true" if running else "false")
        self.generate_btn.style().unpolish(self.generate_btn)
        self.generate_btn.style().polish(self.generate_btn)
        self.progress.setVisible(running)
        self.progress.setValue(int(100 * max(0.03, float(st.get("progress") or 0))))
        if st.get("error") or getattr(self, "_local_error", ""):
            self.status.setObjectName("gfError")
            self.status.setText(st.get("error") or self._local_error)
        else:
            self.status.setObjectName("gfMuted")
            self.status.setText(st.get("message", "") if (running or st.get("message")) else "")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setVisible(bool(self.status.text()))

        # variations
        thumbs = self.ctl.thumbnails()
        tsig = tuple((t["path"], t["selected"]) for t in thumbs)
        if tsig != self._thumb_sig or force:
            self._thumb_sig = tsig
            self._rebuild_grid(thumbs)
        total = len(thumbs)
        idx = next((i for i, t in enumerate(thumbs) if t["selected"]), -1)
        has = total > 0
        for w in (self.prev_btn, self.next_btn, self.counter, self.nav_sep):
            w.setVisible(has)
        self.prev_btn.setEnabled(total > 1)
        self.next_btn.setEnabled(total > 1)
        self.counter.setText("%d/%d" % (idx + 1, total) if idx >= 0 else "-/%d" % total)
        self.scroll.setVisible(has)
        self.delete_btn.setVisible(has and idx >= 0)
        self.empty.setVisible(not has and not running)
        if idx >= 0:
            t = thumbs[idx]
            self.footer.setText("%s  ·  seed %s  ·  frame %s"
                                % (t.get("backend", ""), t.get("seed", ""), t.get("frame", "")))
        else:
            self.footer.setText("")
        self.footer.setVisible(idx >= 0)

    def _rebuild_grid(self, thumbs):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for i, info in enumerate(thumbs):
            t = Thumbnail(info)
            t.clicked.connect(self._on_thumb_clicked)
            t.menuRequested.connect(self._on_thumb_menu)
            self.grid.addWidget(t, i // COLUMNS, i % COLUMNS)
        rows = max(1, (len(thumbs) + COLUMNS - 1) // COLUMNS)
        self.scroll.setFixedHeight(min(3, rows) * (THUMB_H + 12) + 2)

    @_safe
    def _on_thumb_clicked(self, gi, vi):
        self.ctl.select(gi, vi)
        self._sync(force=True)
