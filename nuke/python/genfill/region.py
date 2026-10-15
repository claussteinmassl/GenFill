"""Selection bounds and generation-region math (pure Python, no Nuke).

Coordinate conventions
----------------------
* ``Box`` values passed around here are in **Nuke pixel space**: origin at the
  bottom-left of the format, ``x`` right, ``y`` up, ``r``/``t`` exclusive.
* PNG data is top-down, so :func:`probe_bbox_to_format` flips Y.

Like Photoshop, GenFill does not send the whole frame to the model. It sends
the selection plus a band of surrounding context, then resizes that crop to a
size the model likes. Matching the crop's aspect ratio to the model size
avoids stretching the plate on the way in and out.
"""
from __future__ import annotations

import math
from collections import namedtuple

Box = namedtuple("Box", "x y r t")


class Region(namedtuple("Region", "x y w h")):
    """Integer crop region in Nuke pixel space."""

    @property
    def r(self):
        return self.x + self.w

    @property
    def t(self):
        return self.y + self.h

    @property
    def aspect(self):
        return float(self.w) / float(self.h)


def mask_bbox(image, threshold=8):
    """Bounds of pixels whose first channel exceeds ``threshold``.

    Returns ``(x0, y0, x1, y1)`` in PNG (top-down) pixels, exclusive end,
    or ``None`` when the mask is empty.
    """
    ch = image.channels
    x0, y0, x1, y1 = image.width, image.height, -1, -1
    for y, row in enumerate(image.rows):
        lum = row[0::ch]
        hits = [i for i, v in enumerate(lum) if v > threshold]
        if not hits:
            continue
        if y < y0:
            y0 = y
        y1 = y
        if hits[0] < x0:
            x0 = hits[0]
        if hits[-1] > x1:
            x1 = hits[-1]
    if x1 < 0:
        return None
    return (x0, y0, x1 + 1, y1 + 1)


def probe_bbox_to_format(bbox, probe_w, probe_h, fmt_w, fmt_h):
    """Scale a top-down probe bbox to a bottom-up full-resolution :class:`Box`.

    The probe is low resolution, so the result is grown by one probe pixel on
    every side to make sure nothing in the selection is clipped.
    """
    px0, py0, px1, py1 = bbox
    sx = float(fmt_w) / probe_w
    sy = float(fmt_h) / probe_h
    x = max(0.0, (px0 - 1) * sx)
    r = min(float(fmt_w), (px1 + 1) * sx)
    t = min(float(fmt_h), (probe_h - py0 + 1) * sy)
    y = max(0.0, (probe_h - py1 - 1) * sy)
    return Box(x, y, r, t)


def _fit_span(center, length, limit):
    length = min(length, limit)
    start = center - length / 2.0
    start = max(0.0, min(start, limit - length))
    return start, length


def expand_region(box, fmt_w, fmt_h, context=0.35, min_pad=32, min_size=512, aspect=None):
    """Grow a selection :class:`Box` into the region sent to the model.

    ``context``  fraction of the selection's larger side added on every side.
    ``min_pad``  minimum context band in pixels.
    ``min_size`` minimum region side (models do badly on tiny crops).
    ``aspect``   if given, the region is widened/heightened to this w/h ratio.
    The region is kept inside the format; if it can't be (selection larger
    than the frame in that axis) it is clamped and the aspect will drift.
    """
    bw = max(1.0, box.r - box.x)
    bh = max(1.0, box.t - box.y)
    pad = max(float(min_pad), context * max(bw, bh))
    w = max(bw + 2 * pad, float(min_size))
    h = max(bh + 2 * pad, float(min_size))
    if aspect:
        if w / h < aspect:
            w = h * aspect
        else:
            h = w / aspect
        # If one side has to be clamped to the format, shrink the other side
        # too (but never below the selection) to stay close to the aspect.
        if w > fmt_w:
            w = float(fmt_w)
            h = max(min(h, w / aspect), bh)
        if h > fmt_h:
            h = float(fmt_h)
            w = max(min(w, h * aspect), bw)
    cx = (box.x + box.r) / 2.0
    cy = (box.y + box.t) / 2.0
    x, w = _fit_span(cx, w, float(fmt_w))
    y, h = _fit_span(cy, h, float(fmt_h))
    xi, yi = int(math.floor(x)), int(math.floor(y))
    wi = min(int(math.ceil(x + w)) - xi, fmt_w - xi)
    hi = min(int(math.ceil(y + h)) - yi, fmt_h - yi)
    return Region(xi, yi, max(1, wi), max(1, hi))


def round_to(value, multiple):
    return max(multiple, int(round(value / float(multiple))) * multiple)


def scale_to_pixel_budget(w, h, budget, multiple, min_side=64, max_side=None, max_aspect=None):
    """Pick a model resolution near ``budget`` pixels with ``w/h`` aspect.

    Both sides are rounded to ``multiple``. ``max_aspect`` clamps extreme
    ratios (e.g. 3.0 for 3:1).
    """
    aspect = float(w) / float(h)
    if max_aspect:
        aspect = max(1.0 / max_aspect, min(max_aspect, aspect))
    mh = math.sqrt(budget / aspect)
    mw = mh * aspect
    if max_side:
        s = min(1.0, float(max_side) / max(mw, mh))
        mw, mh = mw * s, mh * s
    return max(min_side, round_to(mw, multiple)), max(min_side, round_to(mh, multiple))


def closest_size(w, h, sizes):
    """Return the entry of ``sizes`` [(w, h), ...] whose aspect is closest (log scale)."""
    a = math.log(float(w) / float(h))
    return min(sizes, key=lambda s: abs(math.log(float(s[0]) / s[1]) - a))


def aspect_sizes(ratios, budget, multiple=16):
    """``["16:9", "1:1", ...]`` -> sizes near ``budget`` pixels, as ``[(w, h, "16:9"), ...]``."""
    out = []
    for r in ratios:
        a, b = (float(x) for x in r.split(":"))
        w, h = scale_to_pixel_budget(a, b, budget, multiple, min_side=multiple)
        out.append((w, h, r))
    return out


def clamp_pixels(w, h, multiple, min_pixels=None, max_pixels=None):
    """Nudge a rounded size back inside a model's pixel limits (rounding can overshoot).

    Sides stay on ``multiple``; the aspect barely changes.
    """
    if max_pixels and w * h > max_pixels:
        s = math.sqrt(float(max_pixels) / (w * h))
        w = max(multiple, int(w * s / multiple) * multiple)
        h = max(multiple, int(h * s / multiple) * multiple)
    if min_pixels and w * h < min_pixels:
        s = math.sqrt(float(min_pixels) / (w * h))
        w = int(math.ceil(w * s / multiple)) * multiple
        h = int(math.ceil(h * s / multiple)) * multiple
    return w, h
