# Region prompts, FLUX 3 Klein, grouped model menu — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development.

**Goal:** Grouped model version menu, bounding-box region prompts for FLUX 3 / Ideogram models on
every backend, FLUX 3 Klein ComfyUI workflows.

**Architecture:** Pure-Python formatters (`backends/layout.py`) turn boxes into each model
family's prompt/field format; backends declare a layout style per model; the Nuke side stores
each box as an internal Crop node (`genfill/boxes.py`), the engine converts box rectangles into
crop coordinates; the panel gets a Region prompts section.

**Tech Stack:** Python 3.7+ stdlib, Nuke Python API (tested via `tests/fake_nuke.py`),
PySide2/6 via `genfill/ui/qt.py`, ComfyUI API-format workflows.

**Spec:** `docs/superpowers/specs/2026-10-17-region-prompts-design.md` (binding; read the
section named in each task).

## Global Constraints

- English code/comments, Google-style docstrings, match surrounding style (comment density,
  naming). Python 3.7 compatible (no walrus, no `match`, `from __future__ import annotations`
  where the file already uses it).
- Tests: stdlib unittest, `python3 -m unittest discover -s tests` must stay green (326 tests now,
  1 skipped without Qt). Every test module imports `isolated_env` first (never touch
  `~/.nuke/genfill`). Qt widget tests go into `tests/test_qt_widgets.py` (skipped without
  PySide); a PySide6 env for running them: `$SP/qtenv/bin/python` with
  `SP=/private/tmp/claude-501/-Users-claussteinmassl-Library-CloudStorage-Dropbox-dev-nuke-GenFill/fca11512-9ac1-48f8-8e24-2ef21f672976/scratchpad`.
- No Qt calls off the main thread. No network in unit tests.
- Never print API keys/tokens. Commit only your own files by explicit path (`git add <paths>`,
  never `git add -A`); other agents may work in the same tree. Commit trailer:
  `Claude-Session: https://claude.ai/code/session_0145ayo4GhtZYb892kjAnJFw`.
- Box coordinate formats exactly as spec 3.5 (0-1000, `[y0, x0, y1, x1]`, ids `obj_N`; fal Klein
  fractions x-first).

**User decisions (already made):** see spec "User decisions".

---

### Task 2: Model families + grouped version menu

Spec section 2. Files: create `nuke/python/genfill/backends/families.py`; modify
`nuke/python/genfill/ui/panel.py` (`NukeController.model_catalogue`, `GenFillPanel._fill_version`
and anything reading catalogue tuples); tests `tests/test_families.py`, extend
`tests/test_panel_controller.py`, add a combo test to `tests/test_qt_widgets.py`.

- `families.family(model_id, label="", explicit="")` returns a display name; ordered regex table
  as in the spec; explicit (from `spec.extra.get("family")`) wins.
- Put the item-list construction in a pure helper (e.g. `families.grouped(entries)` returning
  `[("header", name) | ("model", id, label, note)]`, no headers when only one family) so it is
  testable without Qt; `_fill_version` renders it (headers: disabled, non-selectable, bold font,
  data None; model labels indented two spaces).
- Tests: every curated catalogue entry of every backend maps to a non-"Other" family except
  ComfyUI workflows/mock where Other is fine — assert specific ones: all Magnific ids containing
  `nano-banana` -> Google, `gpt` -> OpenAI, `flux` -> Black Forest Labs, `seedream` -> ByteDance,
  `ideogram` -> Ideogram, `retouch-auto` -> Magnific; fal `fal-ai/flux-pro/v1/fill` -> Black
  Forest Labs. Header ordering = first appearance. Real-Qt test: header item not selectable.

### Task 3: Layout formatters + backends send boxes

Spec 3.5 and 3.6. Files: create `nuke/python/genfill/backends/layout.py`; modify
`backends/base.py` (`GenerationRequest(..., boxes=None)` -> `self.boxes = list(boxes or [])`,
`Backend.layout_style(model)`), `backends/magnific.py`, `backends/higgsfield.py`,
`backends/fal.py` (also the new Klein edit entry with `image_urls` and the native
`bounding_boxes` field), `backends/comfyui.py` (style from the workflow sidecar `layout`; just
`Workflow.meta.get("layout")` — do NOT change workflow validation, Task 4 owns that),
`backends/discovery.py` only if needed for the id inference. Tests: `tests/test_layout.py` plus
additions to `test_magnific.py`, `test_higgsfield.py`, `test_fal.py`, `test_comfyui.py`.

- Exact JSON per spec, `json.dumps(..., separators=(",", ":"))` for flux3 rows; ideogram JSON
  prompt with `ensure_ascii=False`.
- Each backend: `prompt_text, fields = layout.apply(self.layout_style(model), prompt,
  request.boxes, request.width, request.height)`; use `prompt_text` where the prompt is sent;
  merge `fields` into the payload (fal Klein only). When boxes are sent to Ideogram on fal,
  any magic-prompt / prompt-expansion field present in the schema is set off.
- No boxes -> payloads byte-identical to today (assert in tests).
- Don't touch engine/node/panel.

### Task 4: FLUX 3 Klein ComfyUI workflows

Spec section 4. Files: create `nuke/workflows/comfyui/flux3_klein.json` (+ `.genfill.json`),
`flux3_klein_upsampler.json` (+ sidecar), `api_flux3_image.json` (+ sidecar); modify
`backends/comfy_workflows.py` (`mask: false`), `backends/comfyui.py` (skip mask upload when no
mask node), `nuke/workflows/comfyui/README.md`, `README.md` (ComfyUI section: FLUX 3 Klein files);
tests in `tests/test_comfy_workflows.py`, `tests/test_comfyui.py`.

- Node inputs must match the live ComfyUI 0.41.3 object_info (server at http://127.0.0.1:8188 is
  running; `curl -s http://127.0.0.1:8188/object_info/<Class>`). `ReferenceMultiLatent` uses a
  dynamic combo: find the correct API-format encoding for `resolution` = "keep resolution" and the
  autogrow `images.image0` input by exporting/reasoning from the node definition and verify it with
  `POST /prompt` (expected result: only missing-model errors, no graph errors). Same for
  `TextGenerate` (`sampling_mode` dynamic combo) and `Flux3ImageNode`.
- Upsampler system prompt: copy verbatim from
  `$SP/flux3/image_flux3_klein_distilled_image_edit.json` node 48 (PrimitiveStringMultiline).
- The template templates are UI format in `$SP/flux3/`; source facts in `$SP/flux3/` and
  `$SP/bbox_apis.md`.

### Task 5: Boxes in the Nuke node + engine

Spec 3.1, 3.2, 3.4. Files: create `nuke/python/genfill/boxes.py`; modify `node.py`
(`gf_show_boxes` knob, overlay Switch + box mask Switch in `_build_internals` and
`ensure_current`, attach/detach box tools next to the roto tools), `engine.py` (collect boxes,
region union, boxes-only selection, implicit FLUX 3 box, `request.boxes`, boxes in the generation
record), `tests/fake_nuke.py` (only what is missing: Crop/Rectangle/Switch knobs, `gl_color`,
`tile_color`, `showControlPanel`, `hideControlPanel`, knob expressions); tests
`tests/test_boxes.py`, additions to `tests/test_pipeline.py`.

- Coordinate conversion: Nuke box (x, y, r, t), origin bottom-left, region `reg` (x, y, w, h,
  r, t) -> model pixels origin top-left: `x0 = (bx - reg.x) * mw / reg.w`,
  `y0 = (reg.t - bt) * mh / reg.h` etc., clipped to [0, mw] x [0, mh].
- Implicit FLUX 3 box and prompt prefix per spec 3.4 happen in the engine only when
  `backend.layout_style(model)` starts with "flux3".

### Task 6: Panel Region prompts section

Spec 3.3. Files: `nuke/python/genfill/ui/panel.py` (controller methods `boxes()`,
`add_box()`, `remove_box(n)`, `set_box_prompt(n, text)`, `set_box_enabled(n, on)`,
`layout_supported()`; the section widgets; sync in `_sync_impl`), `tests/test_panel_controller.py`,
`tests/test_qt_widgets.py` (section builds, add/remove rows, disabled when unsupported).

### Task 7: Live checks + docs

`installers/live_check.py --boxes "x0,y0,x1,y1:text;..."` (fractions of the crop) ; live runs per
spec 5; README (Region prompts section, model menu grouping), `docs/ARCHITECTURE.md`,
`CHANGELOG.md` 0.10.0, `__version__` 0.10.0, `USER_AGENT` `GenFill-Nuke/0.10`.
