"""Colour pipeline between Nuke's working space and the model.

Image models only understand display-style values in 0..1. Nothing they make
goes above 1.0, so keeping highlights is a question of *encoding*: the plate
is converted into a space where its whole range fits in 0..1, the model works
there, and the result goes back through the exact inverse.

Transfers (stored per generation, so old variations decode correctly):

    display  The show's display transform as an OCIO colorspace, e.g. ACES
          "Output - Rec.709". Its filmic tone curve squeezes highlights into
          0..1 and the model sees a natural-looking picture; Nuke's inverse
          expands them back. Default for scene-linear plates. Transport is
          8-bit, so the steep inverse near white can band in highlights; log
          (ACEScct) is the safer encoding there.
    log   ACEScct curve (pure maths, no OCIO needed). 0..1 covers
          scene-linear -0.007 .. 222.9, so highlights survive. Used for
          scene-linear plates (EXR, ACEScg, linear).
    ocio  An OCIO colorspace, e.g. the plate's own input colorspace
          (matte_paint / sRGB texture for a PNG, LogC for an ARRI plate).
          Working -> colorspace on the way out, colorspace -> working back.
    none  Working-space values as-is (legacy / data).

Transport: every backend takes and returns 8-bit PNG, so the chosen encoding is
quantised to 8 bits on the way out and decoded from 8 bits on the way back.

Everything here except the *_node helpers is pure Python (tested without Nuke).
"""
from __future__ import annotations

import math
import os

DEFAULT = "default"

MODES = ["Auto (match input)", "Display transform (e.g. Output - Rec.709)",
         "Log (ACEScct)", "Input colorspace", "Custom colorspace"]
MODE_AUTO, MODE_DISPLAY, MODE_LOG, MODE_INPUT, MODE_CUSTOM = range(5)

# Display colorspaces to look for in the OCIO config, best first.
DISPLAY_PREFERENCE = (
    ("output", "rec.709"), ("output", "rec709"), ("output", "709"),
    ("rec.1886", "rec.709"), ("rec.709", "display"), ("bt.1886",),
    ("output", "srgb"), ("srgb", "display"),
)

SWITCH_INDEX = {"log": 0, "ocio": 1, "none": 2}

# ---------------------------------------------------------------- ACEScct
_A, _B = 10.5402377416545, 0.0729055341958355
_LIN_BREAK, _CCT_BREAK = 0.0078125, 0.155251141552511
ACESCCT_MAX_LINEAR = 2.0 ** (17.52 - 9.72)          # value encoded as 1.0 (~222.86)


def acescct_encode(x):
    if x <= _LIN_BREAK:
        return _A * x + _B
    return (math.log(x, 2) + 9.72) / 17.52


def acescct_decode(y):
    if y <= _CCT_BREAK:
        return (y - _B) / _A
    return 2.0 ** (y * 17.52 - 9.72)


def _expr_encode(c):
    return ("%s > 0.0078125 ? (log(%s)/log(2) + 9.72)/17.52 : 10.5402377416545*%s + "
            "0.0729055341958355" % (c, c, c))


def _expr_decode(c):
    return ("%s > 0.155251141552511 ? pow(2, %s*17.52 - 9.72) : (%s - 0.0729055341958355)"
            "/10.5402377416545" % (c, c, c))


ENCODE_EXPRS = [_expr_encode(c) for c in ("r", "g", "b")]
DECODE_EXPRS = [_expr_decode(c) for c in ("r", "g", "b")]


def srgb_oetf(v):
    v = min(1.0, max(0.0, v))
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


# ---------------------------------------------------------------- resolution
LINEAR_MARKERS = ("linear", "acescg", "2065", "scene_linear", "compositing_linear", "lin_",
                  "lin ", "reference", "rendering")
FLOAT_EXTS = (".exr", ".hdr", ".sxr", ".pfm")


def parse_colorspace_value(value):
    """'default (matte_paint)' -> 'matte_paint'; 'Roles/matte_paint' -> 'matte_paint'."""
    v = (value or "").strip()
    if v.lower().startswith("default") and "(" in v and v.endswith(")"):
        v = v[v.index("(") + 1:-1].strip()
    return v.split("\t")[0].split("/")[-1].strip()


def classify(colorspace, ext=""):
    """'linear' (needs log encoding to keep highlights) or 'display' (fine as-is)."""
    n = (colorspace or "").lower()
    if n in ("data", "raw"):
        return "linear" if (ext or "").lower() in FLOAT_EXTS else "display"
    if n and any(m in n for m in LINEAR_MARKERS):
        return "linear"
    if not n:
        return "linear" if (ext or "").lower() in FLOAT_EXTS else "display"
    return "display"


def pick_display_colorspace(names):
    """Choose a display colorspace (e.g. 'Output - Rec.709') from OCIO names."""
    leaves = [parse_colorspace_value(n) for n in names or []]
    for wanted in DISPLAY_PREFERENCE:
        for leaf in leaves:
            low = leaf.lower()
            if all(w in low for w in wanted):
                return leaf
    return None


def resolve(mode, read_colorspace=None, read_ext="", custom="", working="scene_linear",
            display=None):
    """Decide the transfer for a generation. Returns a dict stored with it.

    ``display`` is the display colorspace to use for the Display mode (and for
    scene-linear plates in Auto); None means the config has none (falls back
    to Log).
    """
    log = {"mode": "log", "label": "Log (ACEScct)", "working": working}

    def disp(why=""):
        if not display:
            return dict(log, label="Log (ACEScct) (no display colorspace in this config)")
        return {"mode": "ocio", "colorspace": display, "working": working,
                "label": "display: %s%s" % (display, why), "kind": "display"}
    if mode == MODE_LOG:
        return log
    if mode == MODE_DISPLAY:
        return disp()
    if mode == MODE_CUSTOM:
        cs = parse_colorspace_value(custom) if custom and custom != DEFAULT else "matte_paint"
        return {"mode": "ocio", "colorspace": cs, "working": working, "label": cs}
    cs = parse_colorspace_value(read_colorspace) if read_colorspace else ""
    if mode == MODE_INPUT:
        if cs:
            return {"mode": "ocio", "colorspace": cs, "working": working,
                    "label": "input: %s" % cs}
        return dict(log, label="Log (ACEScct) (no input colorspace found)")
    # Auto: display-referred inputs keep their own colorspace; scene-linear
    # plates go through the display transform (log if the config has none).
    if not cs and not read_ext:
        return disp(" (auto: no Read found)")
    if classify(cs, read_ext) == "linear":
        return disp(" (auto: %s is scene-linear)" % (cs or read_ext))
    return {"mode": "ocio", "colorspace": cs or "matte_paint", "working": working,
            "label": "input: %s (auto)" % (cs or "matte_paint")}


def preview_kind(transfer):
    """How the helper should make 8-bit thumbnails from model-space floats:
    'log' = decode ACEScct then sRGB; 'display' = values are already viewable."""
    return "log" if (transfer or {}).get("mode") == "log" else "display"


# ---------------------------------------------------------------- Nuke helpers
def _set_cs_knob(knob, name):
    """Set a colorspace knob by name, tolerant of family menus ('Roles/x')."""
    if not name or name == DEFAULT:
        knob.setValue(0)
        return True
    try:
        values = list(knob.values())
    except Exception:
        values = []
    for i, v in enumerate(values):
        if v == name or v.split("\t")[0].split("/")[-1] == name:
            knob.setValue(i)
            return True
    try:
        knob.setValue(name)
        return True
    except Exception:
        return False


def apply_to_write(write_node, name):
    return _set_cs_knob(write_node["colorspace"], name)


def apply_to_read(read_node, name):
    return _set_cs_knob(read_node["colorspace"], name)


def set_raw(node, raw=True):
    if "raw" in node.knobs():
        node["raw"].setValue(bool(raw))


def working_space():
    import nuke
    root = nuke.root()
    for k in ("workingSpaceLUT", "colorManagement_workingSpace"):
        if k in root.knobs():
            v = parse_colorspace_value(root[k].value())
            if v:
                return v
    return "scene_linear"


def find_upstream_read(node, limit=60):
    """First Read node up the main (input 0) chain, or None."""
    seen = 0
    while node is not None and seen < limit:
        if node.Class() in ("Read", "DeepRead"):
            return node
        node = node.input(0)
        seen += 1
    return None


def read_info(read):
    if read is None:
        return None, ""
    cs = parse_colorspace_value(read["colorspace"].value()) if "colorspace" in read.knobs() else ""
    if "raw" in read.knobs() and read["raw"].value():
        cs = "raw"
    path = read["file"].value() if "file" in read.knobs() else ""
    return cs, os.path.splitext(path.split("%")[0].split("#")[0] or path)[1].lower()


def configure(switch, log_node, ocio_node, transfer, direction):
    """Point a to-model / from-model switch at the transfer.

    direction 'to': working -> model space. 'from': model space -> working.
    """
    mode = (transfer or {}).get("mode", "none")
    switch["which"].setValue(SWITCH_INDEX.get(mode, 2))
    if mode == "ocio":
        working, cs = transfer.get("working", "scene_linear"), transfer["colorspace"]
        src, dst = (working, cs) if direction == "to" else (cs, working)
        _set_cs_knob(ocio_node["in_colorspace"], src)
        _set_cs_knob(ocio_node["out_colorspace"], dst)
