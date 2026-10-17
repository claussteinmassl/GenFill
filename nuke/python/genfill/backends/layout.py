"""Region prompts (bounding boxes) in the formats the layout-aware models read.

A :class:`Box` is one region of the image sent to the model, in its pixel coordinates (origin
top-left), with its own text. Three styles:

* ``flux3``: FLUX 3 layout prompt. The prompt (trailing whitespace stripped), a space, then a
  JSON list of rows ``{"id": "obj_N", "bbox": [y0, x0, y1, x1], "desc": text}`` with 0-1000
  coordinates, y first, written like ComfyUI's ``Flux3LayoutPrompt`` (``json.dumps`` default
  separators, non-ASCII kept; BFL layout prompting guide). The prompt cites a box as
  ``<obj_N>``.
* ``flux3_fields``: fal's FLUX 3 Klein native ``bounding_boxes`` field: ``{"label": "obj_N",
  "description": text, "box": {"x_min", "y_min", "x_max", "y_max"}}`` as fractions 0-1, x first.
* ``ideogram``: Ideogram 4.5 JSON prompt (``high_level_description`` plus
  ``compositional_deconstruction`` elements with 0-1000 ``bbox`` [y0, x0, y1, x1]). Ideogram has
  no citation syntax, so ``<obj_N>`` in the prompt becomes the box text in parentheses. Callers
  with a magic-prompt / prompt-expansion switch turn it off when boxes are sent.
"""
from __future__ import annotations

import collections
import json
import re

STYLES = ("flux3", "flux3_fields", "ideogram")

Box = collections.namedtuple("Box", "n x0 y0 x1 y1 prompt")

IDEOGRAM_BACKGROUND = "Keep the existing image outside the boxes unchanged."

_TAG = re.compile(r"[ \t]*<obj_(\d+)>")
_INFER = (("flux3", re.compile(r"flux-3|flux_3|flux3", re.I)),
          ("ideogram", re.compile(r"ideogram.*4[-_.]5|ideogram.*v4", re.I)))


def infer_style(model_id):
    """The layout style for a discovered model id, or ``None`` (``flux-3`` -> ``flux3``)."""
    for style, pattern in _INFER:
        if pattern.search(str(model_id or "")):
            return style
    return None


def _label(box):
    return "obj_%d" % int(box.n)


def _used(boxes):
    """The boxes that carry text, in their given order."""
    return [b for b in boxes or [] if str(b.prompt or "").strip()]


def _span(lo, hi, size, scale):
    """``(lo, hi)`` of one axis scaled from ``0..size`` to ``0..scale``, ordered and clamped."""
    size = float(size) or 1.0
    a, b = sorted((float(lo), float(hi)))
    return (min(max(a / size * scale, 0.0), float(scale)),
            min(max(b / size * scale, 0.0), float(scale)))


def norm1000(box, width, height):
    """``[y0, x0, y1, x1]`` ints 0-1000 of ``box`` in a ``width`` x ``height`` image.

    Rounded and clamped; a box that rounds to nothing still spans one unit (``y0 < y1``,
    ``x0 < x1``).
    """
    out = []
    for lo, hi, size in ((box.y0, box.y1, height), (box.x0, box.x1, width)):
        a, b = (int(round(v)) for v in _span(lo, hi, size, 1000))
        if b <= a:
            a, b = (a, a + 1) if a < 1000 else (999, 1000)
        out.append((a, b))
    (y0, y1), (x0, x1) = out
    return [y0, x0, y1, x1]


def flux3_prompt(prompt, boxes, width, height):
    """``prompt`` + space + JSON rows ``{"id", "bbox", "desc"}``, as ComfyUI's
    ``Flux3LayoutPrompt`` writes them (see module doc)."""
    rows = [{"id": _label(b), "bbox": norm1000(b, width, height), "desc": b.prompt.strip()}
            for b in _used(boxes)]
    if not rows:
        return prompt
    return "%s %s" % (prompt.rstrip(), json.dumps(rows, ensure_ascii=False))


def flux3_fields(boxes, width, height):
    """fal FLUX 3 Klein ``bounding_boxes``: label, description and an x-first fraction box."""
    out = []
    for b in _used(boxes):
        x0, x1 = (round(v, 4) for v in _span(b.x0, b.x1, width, 1.0))
        y0, y1 = (round(v, 4) for v in _span(b.y0, b.y1, height, 1.0))
        out.append({"label": _label(b), "description": b.prompt.strip(),
                    "box": {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1}})
    return out


def ideogram_prompt(prompt, boxes, width, height):
    """Ideogram 4.5 JSON prompt string (``ensure_ascii=False``, compact; see module doc).

    ``<obj_N>`` tags become `` (box text)``; a tag without a box is dropped. An empty prompt
    is described by the box texts (Ideogram requires ``high_level_description``).
    """
    used = _used(boxes)
    texts = dict((int(b.n), b.prompt.strip()) for b in used)

    def tag(match):
        text = texts.get(int(match.group(1)))
        return " (%s)" % text if text else ""

    described = _TAG.sub(tag, prompt or "").strip() or "; ".join(texts[int(b.n)] for b in used)
    elements = [{"type": "obj", "bbox": norm1000(b, width, height), "desc": b.prompt.strip()}
                for b in used]
    data = {"high_level_description": described,
            "compositional_deconstruction": {"background": IDEOGRAM_BACKGROUND,
                                             "elements": elements}}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def sends(style, boxes):
    """True if :func:`apply` changes anything: a known style and at least one box with text."""
    return style in STYLES and bool(_used(boxes))


def apply(style, prompt, boxes, width, height):
    """``(prompt_text, extra_fields)`` for ``style``; no boxes (or no style) -> ``(prompt, {})``.

    Args:
        style: One of :data:`STYLES` or ``None``.
        prompt: The prompt as the backend would send it without boxes.
        boxes: :class:`Box` list in pixels of the ``width`` x ``height`` image sent.

    Returns:
        The prompt to send and the fields to merge into the request body (``flux3_fields``:
        ``{"bounding_boxes": [...]}``).
    """
    if not sends(style, boxes):
        return prompt, {}
    if style == "flux3":
        return flux3_prompt(prompt, boxes, width, height), {}
    if style == "flux3_fields":
        return prompt, {"bounding_boxes": flux3_fields(boxes, width, height)}
    return ideogram_prompt(prompt, boxes, width, height), {}
