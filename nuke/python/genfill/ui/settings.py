"""API keys and cloud accounts dialog.

Keys go to ~/.nuke/genfill/config.json, never into the script. Browser sign-ins
(OAuth) are stored by the OAuth layer next to it.
"""
from __future__ import annotations

import os
import threading

from .qt import QtCore, QtWidgets, exec_
from . import style
from .. import backends, config
from ..backends import comfyui

POLL_MS = 200


class _SignIn(object):
    """One sign-in running in a worker thread.

    The thread only stores its outcome in plain attributes; the dialog's timer
    reads them on the Qt thread, so no Qt object is touched off-thread.
    """

    def __init__(self, backend):
        self.backend = backend
        self.cancel = False
        self.done = False
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="genfill-signin-" + backend.id)
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            self.backend.provider().login(cancelled=lambda: self.cancel)
        except Exception as exc:  # BackendError / Cancelled / anything unexpected
            if not self.cancel:
                self.error = str(exc) or exc.__class__.__name__
        self.done = True


class _ComfyTest(object):
    """One ComfyUI connection test running in a worker thread.

    Like :class:`_SignIn`, the thread only fills plain attributes; the dialog's
    timer reads them on the Qt thread.
    """

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.done = False
        self.error = ""
        self.text = ""
        self.thread = threading.Thread(target=self._run, name="genfill-comfy-test")
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            info = comfyui.server_info(self.url, self.token)
            devices = ", ".join(d for d in info["devices"] if d)
            self.text = "ComfyUI %s%s" % (info["version"] or "(unknown version)",
                                          " on %s" % devices if devices else "")
        except Exception as exc:  # BackendError / anything unexpected
            self.error = str(exc) or exc.__class__.__name__
        self.done = True


class KeysDialog(QtWidgets.QDialog):
    def __init__(self, focus=None, parent=None):
        super(KeysDialog, self).__init__(parent)
        self.setWindowTitle("Generative Fill - API Keys & Accounts")
        self.setMinimumWidth(520)
        self.setStyleSheet(style.STYLESHEET)
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Keys are saved on this machine only (%s) and are never written into .nk "
            "scripts. Environment variables take priority if set." % config.CONFIG_PATH)
        intro.setWordWrap(True)
        intro.setObjectName("gfMuted")
        lay.addWidget(intro)

        self._comfy_job = None
        self.fields = {}
        first = self._build_comfy(lay)
        key_list = backends.key_backends()
        if key_list:
            box = QtWidgets.QGroupBox("API keys")
            form = QtWidgets.QFormLayout(box)
            for b in key_list:
                field = QtWidgets.QLineEdit()
                field.setEchoMode(QtWidgets.QLineEdit.Password)
                field.setText(config.load().get("api_keys", {}).get(b.id, ""))
                src = config.key_source(b.id, b.env_var)
                hint = "Using %s" % src if src else "Not set"
                if b.auth == "key_or_oauth":
                    hint = ("%s. Optional - without a key GenFill signs in with your %s account"
                            % (hint, b.label.split(" (")[0].replace(" Cloud", "")))
                field.setPlaceholderText(hint if b.env_var else "")
                field.setToolTip("%s\nEnvironment variable: %s" % (hint, b.env_var))
                form.addRow(b.label, field)
                self.fields[b.id] = field
                if b.id == focus or first is None:
                    first = field
            lay.addWidget(box)
        show = QtWidgets.QCheckBox("Show keys")
        show.toggled.connect(self._toggle)
        lay.addWidget(show)

        self.accounts = {}   # backend id -> dict(status, sign_in, sign_out, job)
        account_list = backends.account_backends()
        if account_list:
            box = QtWidgets.QGroupBox("Accounts")
            grid = QtWidgets.QGridLayout(box)
            grid.setColumnStretch(1, 1)
            for row, b in enumerate(account_list):
                status = QtWidgets.QLabel("")
                status.setWordWrap(True)
                sign_in = QtWidgets.QPushButton("Sign in")
                sign_out = QtWidgets.QPushButton("Sign out")
                grid.addWidget(QtWidgets.QLabel(b.label), row, 0)
                grid.addWidget(status, row, 1)
                grid.addWidget(sign_in, row, 2)
                grid.addWidget(sign_out, row, 3)
                acc = {"status": status, "sign_in": sign_in, "sign_out": sign_out,
                       "job": None, "error": ""}
                sign_in.clicked.connect(lambda _=False, bid=b.id: self._on_sign_in(bid))
                sign_out.clicked.connect(lambda _=False, bid=b.id: self._on_sign_out(bid))
                self.accounts[b.id] = acc
                self._refresh_account(b.id)
            lay.addWidget(box)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLL_MS)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                          | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        if first is not None:
            first.setFocus()

    def _toggle(self, on):
        mode = QtWidgets.QLineEdit.Normal if on else QtWidgets.QLineEdit.Password
        for f in list(self.fields.values()) + [self.comfy_token]:
            f.setEchoMode(mode)

    # ComfyUI --------------------------------------------------------------
    def _build_comfy(self, lay):
        """Add the ComfyUI group; returns the URL field (the default focus)."""
        stored = config.load().get("comfyui") or {}
        env_url = os.environ.get("GENFILL_COMFY_URL", "").strip()
        box = QtWidgets.QGroupBox("ComfyUI")
        form = QtWidgets.QFormLayout(box)
        self.comfy_url = QtWidgets.QLineEdit()
        if env_url:
            self.comfy_url.setText(env_url)
            self.comfy_url.setReadOnly(True)
            hint = "Set by GENFILL_COMFY_URL (unset it to edit here)"
        else:
            self.comfy_url.setText(str(stored.get("url") or ""))
            hint = "Using %s" % (
                "config" if stored.get("url") else "default")
        self.comfy_url.setPlaceholderText("%s (%s)" % (config.comfyui_url(), hint))
        self.comfy_url.setToolTip("%s\nEnvironment variable: GENFILL_COMFY_URL" % hint)
        form.addRow("Server URL", self.comfy_url)
        self.comfy_token = QtWidgets.QLineEdit()
        self.comfy_token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.comfy_token.setText(config.load().get("api_keys", {}).get("comfyui", ""))
        src = config.key_source("comfyui", "GENFILL_COMFY_TOKEN")
        token_hint = "Using %s" % src if src else "Not set (only needed for a protected server)"
        self.comfy_token.setPlaceholderText(token_hint)
        self.comfy_token.setToolTip("%s\nEnvironment variable: GENFILL_COMFY_TOKEN" % token_hint)
        form.addRow("Access token", self.comfy_token)
        row = QtWidgets.QHBoxLayout()
        self.comfy_test = QtWidgets.QPushButton("Test connection")
        self.comfy_test.clicked.connect(self._on_comfy_test)
        self.comfy_status = QtWidgets.QLabel("")
        self.comfy_status.setWordWrap(True)
        self.comfy_status.setObjectName("gfMuted")
        row.addWidget(self.comfy_test)
        row.addWidget(self.comfy_status, 1)
        form.addRow(row)
        folders = QtWidgets.QLabel("\n".join(config.comfyui_workflow_dirs()))
        folders.setObjectName("gfMuted")
        folders.setWordWrap(True)
        folders.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        folders.setToolTip("Bundled folder first. More folders: comfyui.workflow_dirs in the "
                           "config file or GENFILL_COMFY_WORKFLOWS.")
        form.addRow("Workflow folders", folders)
        lay.addWidget(box)
        return self.comfy_url

    def _on_comfy_test(self):
        if self._comfy_job is not None:
            return
        url = config.normalize_comfyui_url(self.comfy_url.text()) or config.comfyui_url()
        token = self.comfy_token.text().strip() or config.comfyui_token()
        self._comfy_job = _ComfyTest(url, token)
        self.comfy_test.setEnabled(False)
        self._set_comfy_status("Connecting to %s..." % url, False)
        self._comfy_job.start()

    def _set_comfy_status(self, text, error):
        self.comfy_status.setText(text)
        self.comfy_status.setObjectName("gfError" if error else "gfMuted")
        self.comfy_status.style().unpolish(self.comfy_status)
        self.comfy_status.style().polish(self.comfy_status)

    # accounts -------------------------------------------------------------
    def _refresh_account(self, bid):
        acc = self.accounts[bid]
        backend = backends.get(bid)
        job = acc["job"]
        if job is not None:
            text, signed = "Waiting for the browser...", False
            acc["sign_in"].setText("Cancel")
        else:
            signed = backend.signed_in()
            text = acc["error"] or ("Signed in" if signed else "Not signed in")
            acc["sign_in"].setText("Sign in")
        acc["status"].setText(text)
        acc["status"].setObjectName("gfError" if acc["error"] and job is None else "gfMuted")
        acc["status"].style().unpolish(acc["status"])
        acc["status"].style().polish(acc["status"])
        acc["sign_in"].setEnabled(job is not None or not signed)
        acc["sign_out"].setEnabled(job is None and signed)

    def _on_sign_in(self, bid):
        acc = self.accounts[bid]
        if acc["job"] is not None:      # the button reads "Cancel" while waiting
            acc["job"].cancel = True
            return
        acc["error"] = ""
        acc["job"] = _SignIn(backends.get(bid))
        acc["job"].start()
        self._refresh_account(bid)

    def _on_sign_out(self, bid):
        self.accounts[bid]["error"] = ""
        try:
            backends.get(bid).provider().sign_out()
        except Exception as exc:
            self.accounts[bid]["error"] = str(exc)
        self._refresh_account(bid)

    def _poll(self):
        job = self._comfy_job
        if job is not None and job.done:
            self._comfy_job = None
            self.comfy_test.setEnabled(True)
            self._set_comfy_status(job.error or job.text, bool(job.error))
        for bid, acc in self.accounts.items():
            job = acc["job"]
            if job is not None and job.done:
                acc["job"] = None
                acc["error"] = job.error
                self._refresh_account(bid)

    def _stop_jobs(self):
        for acc in self.accounts.values():
            if acc["job"] is not None:
                acc["job"].cancel = True

    def done(self, result):
        self._stop_jobs()
        super(KeysDialog, self).done(result)

    def _save(self):
        for bid, field in self.fields.items():
            config.set_api_key(bid, field.text())
        if not self.comfy_url.isReadOnly():
            config.set_comfyui(url=self.comfy_url.text())
        config.set_api_key("comfyui", self.comfy_token.text())
        self.accept()


def show(focus=None):
    """Show the dialog modally. Returns True if saved."""
    dlg = KeysDialog(focus=focus, parent=QtWidgets.QApplication.activeWindow())
    return bool(exec_(dlg))
