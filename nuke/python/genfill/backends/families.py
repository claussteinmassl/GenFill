"""Model families for the grouped version menu (Google, OpenAI, Black Forest Labs, ...).

The family is inferred from a model's id and label with an ordered regex table, so a Magnific
"Retouch Nano Banana 2" lands under Google next to the other Nano Bananas. A catalogue entry can
set ``ModelSpec.extra["family"]`` to override the inference.
"""
from __future__ import annotations

import re

OTHER = "Other"

# Ordered: the first match wins. Provider patterns come before the Magnific retouch patterns so
# that "retouch-inpaint-lab-nano-banana-pro" is a Google model, not a Magnific one.
_TABLE = [
    ("Google", r"nano[-_ ]?banana|imagen|gemini"),
    ("OpenAI", r"gpt|dall[-_ ]?e|openai"),
    ("Black Forest Labs", r"flux|kontext"),
    ("ByteDance", r"seedream|seededit|seedance|(?<![a-z])seed(?![a-z])"),
    ("Ideogram", r"ideogram"),
    ("Alibaba", r"qwen|(?<![a-z])wan(?![a-z])"),
    ("Recraft", r"recraft"),
    ("Bria", r"bria"),
    ("Stability", r"stable|sdxl|sd3|sd-"),
    ("xAI", r"grok"),
    ("Kling", r"kling"),
    ("Runway", r"runway"),
    ("Luma", r"luma"),
    ("Midjourney", r"midjourney"),
    ("Magnific", r"retouch-(?:auto|pro|classic|erase)"),
]
_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _TABLE]


def family(model_id, label="", explicit=""):
    """Display name of the family a model belongs to.

    Args:
        model_id: The provider's model id.
        label: The catalogue label (searched together with the id).
        explicit: A family set on the catalogue entry itself; it wins over the inference.

    Returns:
        The family name, "Other" if nothing matches.
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    text = "%s %s" % (model_id or "", label or "")
    for name, pattern in _COMPILED:
        if pattern.search(text):
            return name
    return OTHER


def grouped(entries):
    """Menu rows for catalogue entries, grouped by family.

    Args:
        entries: ``[(id, label, note, family), ...]`` in catalogue order.

    Returns:
        ``[("header", family) | ("model", id, label, note), ...]``. Families appear in order of
        first appearance, models keep the catalogue order inside a family. With only one family
        there are no headers.
    """
    order = []
    members = {}
    for mid, label, note, fam in entries:
        if fam not in members:
            order.append(fam)
            members[fam] = []
        members[fam].append(("model", mid, label, note))
    rows = []
    for fam in order:
        if len(order) > 1:
            rows.append(("header", fam))
        rows.extend(members[fam])
    return rows
