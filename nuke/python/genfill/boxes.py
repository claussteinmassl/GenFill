"""Region prompt boxes stored inside a GenerativeFill node.

Each box is an internal ``Crop`` node ``GenFillBox<N>`` fed by the Source input, so its
``box`` knob (x, y, r, t; Nuke pixels, origin bottom-left) lives in plate coordinates and can
be dragged and animated with Nuke's own Crop handles. It is not connected to the output. Per
box data are knobs added to the Crop: ``gf_box_prompt`` (the box's text) and
``gf_box_enabled``. Its ``gl_color`` (Viewer handle colour) and ``tile_color`` come from a
fixed 8-colour palette by N.

Two subgraphs follow the boxes and are rebuilt whenever one is added or removed
(:func:`rebuild_overlay`):

* the overlay: per box a coloured outline (outer Rectangle stencilled by an inner Rectangle
  3 px smaller, areas linked to the Crop by expression) merged over the output, shown by the
  ``gf_box_overlay`` Switch in the interactive Viewer only;
* the anchor: the ``gf_box_anchor`` Switch before the Output (fixed on input 0) takes every
  box Crop as an extra input, so the Crops are upstream of the Output and Nuke draws their
  handles in the Viewer;
* the box mask: per box a filled Rectangle merged ``max`` into the selection matte, used by
  the ``gf_box_mask`` Switch when the boxes are the selection (set by the engine).

A disabled box's merges are disabled by expression, so toggling needs no rebuild.
"""
from __future__ import annotations

import collections
import re

import nuke

from . import node as gfnode

PREFIX = "GenFillBox"
OVERLAY_PREFIX = "gf_boxov_"
MASK_PREFIX = "gf_boxmask_"
PROMPT_KNOB = "gf_box_prompt"
ENABLED_KNOB = "gf_box_enabled"
OUTLINE = 3                   # outline width in plate pixels

# red, green, blue, yellow, magenta, cyan, orange, violet
PALETTE = ((1.0, 0.25, 0.25), (0.3, 0.9, 0.3), (0.3, 0.5, 1.0), (1.0, 0.9, 0.2),
           (1.0, 0.3, 0.9), (0.2, 0.9, 1.0), (1.0, 0.6, 0.15), (0.65, 0.4, 1.0))

BoxInfo = collections.namedtuple("BoxInfo", "n node_name rect prompt enabled rgb")

_NAME = re.compile(r"^%s(\d+)$" % PREFIX)


def palette(n):
    """``(r, g, b)`` floats 0-1 of box ``n`` (1-based; wraps after 8)."""
    return PALETTE[(int(n) - 1) % len(PALETTE)]


def _bytes(n):
    return [int(round(c * 255)) for c in palette(n)]


def palette_hex(n):
    """``"#rrggbb"`` of box ``n``, for Qt swatches."""
    return "#%02x%02x%02x" % tuple(_bytes(n))


def palette_uint(n):
    """Box ``n``'s colour packed as Nuke's ``0xRRGGBBAA`` (``gl_color`` / ``tile_color``)."""
    r, g, b = _bytes(n)
    return (r << 24) | (g << 16) | (b << 8) | 0xFF


def node_name(n):
    return "%s%d" % (PREFIX, int(n))


def _box_nodes(group):
    """``[(n, crop)]`` sorted by n."""
    out = []
    for child in group.nodes():
        match = _NAME.match(child.name())
        if match and child.Class() == "Crop":
            out.append((int(match.group(1)), child))
    return sorted(out, key=lambda item: item[0])


def _find(group, n):
    crop = group.node(node_name(n))
    return crop if crop is not None and crop.Class() == "Crop" else None


def _rect(crop, frame=None):
    knob = crop["box"]
    if frame is not None:
        try:
            return tuple(float(knob.valueAt(frame, i)) for i in range(4))
        except Exception:
            pass
    return tuple(float(v) for v in knob.value())


def list_boxes(group, frame=None):
    """Every box of ``group``, sorted by number.

    Args:
        group: The GenerativeFill node.
        frame: Read animated rectangles at this frame (``None`` = the current frame).

    Returns:
        ``[BoxInfo(n, node_name, rect, prompt, enabled, rgb)]``; ``rect`` is ``(x, y, r, t)``
        in plate pixels, origin bottom-left.
    """
    out = []
    for n, crop in _box_nodes(group):
        knobs = crop.knobs()
        prompt = crop[PROMPT_KNOB].value() if PROMPT_KNOB in knobs else ""
        enabled = bool(crop[ENABLED_KNOB].value()) if ENABLED_KNOB in knobs else True
        out.append(BoxInfo(n, crop.name(), _rect(crop, frame), prompt or "", enabled,
                           palette(n)))
    return out


def _source_format(group):
    for src in (group.input(0), group.node(gfnode.SRC_IN)):
        if src is not None:
            try:
                fmt = src.format()
                return fmt.width(), fmt.height()
            except Exception:
                pass
    fmt = nuke.root().format()
    return fmt.width(), fmt.height()


def default_rect(group):
    """The last selection GenFill saw (newest generation), else the centre 30 % of the
    source format; ``(x, y, r, t)`` plate pixels."""
    from . import engine
    try:
        gens = engine.get_state(group)["generations"]
    except Exception:
        gens = []
    for gen in reversed(gens):
        sel = gen.get("selection")
        if sel and len(sel) == 4:
            return tuple(float(v) for v in sel)
    w, h = _source_format(group)
    return (w * 0.35, h * 0.35, w * 0.65, h * 0.65)


def add_box(group, rect=None):
    """Add a box (lowest free number) and return its number.

    Args:
        group: The GenerativeFill node.
        rect: ``(x, y, r, t)`` plate pixels; ``None`` = :func:`default_rect`.
    """
    used = set(n for n, _crop in _box_nodes(group))
    n = 1
    while n in used:
        n += 1
    rect = default_rect(group) if rect is None else rect
    with group:
        crop = nuke.nodes.Crop(name=node_name(n))
        crop.setInput(0, group.node(gfnode.SRC_IN))
        crop["box"].setValue([float(v) for v in rect])
        prompt = nuke.String_Knob(PROMPT_KNOB, "box prompt", "")
        prompt.setTooltip("What goes in this box. Cite it in the main prompt as <obj_%d>." % n)
        crop.addKnob(prompt)
        enabled = nuke.Boolean_Knob(ENABLED_KNOB, "enabled")
        enabled.setValue(True)
        crop.addKnob(enabled)
        colour = palette_uint(n)
        crop["gl_color"].setValue(colour)
        crop["tile_color"].setValue(colour)
        src = group.node(gfnode.SRC_IN)
        if src is not None:
            crop.setXYpos(src.xpos() - 140, src.ypos() + 35 * n)
    rebuild_overlay(group)
    if nuke.GUI:
        gfnode.open_hidden_panels([crop])     # the GenFill panel is open: show its handles
    return n


def remove_box(group, n):
    """Delete box ``n``; False if there is none."""
    crop = _find(group, n)
    if crop is None:
        return False
    if nuke.GUI:
        try:
            crop.hideControlPanel()
        except Exception:
            pass
    nuke.delete(crop)
    rebuild_overlay(group)
    return True


def set_prompt(group, n, text):
    """Set box ``n``'s text; False if there is no such box."""
    crop = _find(group, n)
    if crop is None:
        return False
    crop[PROMPT_KNOB].setValue(text or "")
    return True


def set_enabled(group, n, on):
    """Enable / disable box ``n`` (a disabled box is neither drawn nor sent)."""
    crop = _find(group, n)
    if crop is None:
        return False
    crop[ENABLED_KNOB].setValue(bool(on))
    if nuke.GUI:
        if on:
            gfnode.open_hidden_panels([crop])
        else:
            try:
                crop.hideControlPanel()
            except Exception:
                pass
    return True


def attach_box_tools(group):
    """Open every enabled box's control panel floating and hidden, so all boxes show their
    Crop handles in the Viewer at once (the selection Roto technique, see node.py).

    Returns:
        The number of boxes attached (0 without a GUI).
    """
    if not nuke.GUI:
        return 0
    on = []
    for _n, crop in _box_nodes(group):
        if ENABLED_KNOB not in crop.knobs() or crop[ENABLED_KNOB].value():
            on.append(crop)
        else:
            try:
                crop.hideControlPanel()
            except Exception:
                pass
    return len(on) if gfnode.open_hidden_panels(on) else 0


def detach_box_tools(group):
    """Close every box's control panel (their handles leave the Viewer)."""
    if not nuke.GUI:
        return
    for _n, crop in _box_nodes(group):
        try:
            crop.hideControlPanel()
        except Exception:
            pass


def _link_area(rect, n, inset=0):
    knob = rect["area"]
    for i, comp in enumerate("xyrt"):
        expr = "%s.box.%s" % (node_name(n), comp)
        if inset:
            expr += (" + %d" if i < 2 else " - %d") % inset
        knob.setExpression(expr, i)


def _rectangle(name, n, rgba, inset, col, row):
    rect = nuke.nodes.Rectangle(name=name)
    rect["color"].setValue(list(rgba))
    _link_area(rect, n, inset)
    rect.setXYpos(int(col * 140), int(row * 70))
    return rect


def _merge(name, op, b, a, n, col, row):
    m = nuke.nodes.Merge2(name=name)
    m.setInput(0, b)
    m.setInput(1, a)
    m["operation"].setValue(op)
    m["disable"].setExpression("!%s.%s" % (node_name(n), ENABLED_KNOB))
    m.setXYpos(int(col * 140), int(row * 70))
    return m


def rebuild_overlay(group):
    """Rebuild the box outline overlay and the box mask from the current boxes."""
    gfnode._build_box_switches(group)
    with group:
        for child in list(group.nodes()):
            if child.name().startswith((OVERLAY_PREFIX, MASK_PREFIX)):
                nuke.delete(child)
        numbers = [n for n, _crop in _box_nodes(group)]

        ov = group.node(gfnode.BOX_OVERLAY)
        if ov is not None:
            chain = ov.input(0)
            for i, n in enumerate(numbers):
                col = 8 + 2 * i
                outer = _rectangle("%souter%d" % (OVERLAY_PREFIX, n), n,
                                   tuple(palette(n)) + (1.0,), 0, col, 11)
                inner = _rectangle("%sinner%d" % (OVERLAY_PREFIX, n), n,
                                   (1.0, 1.0, 1.0, 1.0), OUTLINE, col + 1, 11)
                ring = _merge("%sring%d" % (OVERLAY_PREFIX, n), "stencil", outer, inner, n,
                              col, 12)
                chain = _merge("%sover%d" % (OVERLAY_PREFIX, n), "over", chain, ring, n,
                               col, 13)
            ov.setInput(1, chain)

        anchor = group.node(gfnode.BOX_ANCHOR)
        if anchor is not None:
            for i in range(anchor.inputs() - 1, 0, -1):
                anchor.setInput(i, None)
            for i, (_n, crop) in enumerate(_box_nodes(group)):
                anchor.setInput(i + 1, crop)

        bm = group.node(gfnode.BOX_MASK_SWITCH)
        if bm is not None:
            chain = bm.input(0)
            for i, n in enumerate(numbers):
                col = 8 + 2 * i
                rect = _rectangle("%srect%d" % (MASK_PREFIX, n), n, (1.0, 1.0, 1.0, 1.0), 0,
                                  col + 1, 0)
                chain = _merge("%smax%d" % (MASK_PREFIX, n), "max", chain, rect, n, col, 1)
            bm.setInput(1, chain)
