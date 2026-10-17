# Region prompts (bounding boxes), FLUX 3 Klein workflows, grouped model menu, Enter to generate

Date: 2026-10-17. Status: user decisions below, design by Claude.

## User decisions

- Prompt field: **Enter generates, Shift+Enter inserts a newline** (Ctrl/Cmd+Enter keeps generating).
- Model version menu: **group models that belong together** (all Google / Nano Banana together, all
  OpenAI / GPT Image together, ...).
- New ComfyUI workflows for **FLUX 3 Image Klein** (released 2026-10-15): one without and one with
  the prompt upsampler ("Zweiter Workflow mit Upsampler").
- **Region prompts**: the artist adds bounding boxes, each with its own prompt, sees all boxes at
  once in the Viewer in distinct colours. Each box is **an internal Crop node** (robust rectangle,
  cannot turn non-rectangular). Colour shown in the Viewer for all boxes at the same time.
- Box prompts apply to **FLUX 3 and Ideogram models, on every backend** (local ComfyUI, Magnific,
  fal, Higgsfield, Comfy API nodes).
- Live test: local ComfyUI updated to v0.41.3 (done), FLUX 3 Klein files downloaded (needs the
  user's Hugging Face token; gated repo with automatic approval).

## 1. Prompt keys

`ui/panel.py` `PromptEdit.keyPressEvent`: Return/Enter without Shift -> generate; with Shift ->
newline (`insertPlainText("\n")`). Placeholder/tooltip text updated
("Enter = Generate, Shift+Enter = new line"). Keypad Enter behaves the same.

## 2. Grouped model version menu

- New module `backends/families.py`: `family(spec_or_id, label="") -> str` from an ordered regex
  table over id + label (case-insensitive). Families and display names:
  Google (nano banana, imagen, gemini), OpenAI (gpt, dall-e, openai), Black Forest Labs (flux,
  kontext), ByteDance (seedream, seededit, seed), Ideogram, Alibaba (qwen, wan), Recraft, Bria,
  Stability (stable, sdxl, sd3, sd-), Kling, Runway, Luma, Midjourney, Magnific (retouch-auto,
  retouch-pro, retouch-classic, retouch-erase), Other (fallback). A `ModelSpec.extra["family"]`
  overrides inference. Retouch variants of a provider model ("Retouch Nano Banana 2") go to that
  provider (Google), because the user wants all Nano Bananas together.
- `NukeController.model_catalogue()` entries gain the family: `(id, label, note, family)`.
- Panel `_fill_version`: "Default (...)" first, then for each family in order of first
  appearance in the catalogue a non-selectable bold header item (`Qt.ItemIsEnabled` removed,
  `ItemIsSelectable` removed) and the family's models indented by two spaces in catalogue order
  (discovered "(new)" models land in their family). Then separator and "Refresh model list...".
  A backend whose entries all share one family (ComfyUI workflows, Stability) gets no headers.
  ComfyUI workflows group by their sidecar `"family"` or by inference from label.
- Selecting / typing ids is unchanged (`findData` still works; headers carry data `None`).

## 3. Region prompts (bounding boxes)

### 3.1 Data in the Nuke node

- Each box is an internal `Crop` node `GenFillBox<N>` inside the group (N = 1, 2, ..., never
  reused while the box exists; a new box takes the lowest free N), input = the Source path
  (so its box knob lives in plate pixel coordinates), not connected to the output.
- The box rectangle is the Crop's `box` knob (x, y, r, t, Nuke coordinates, origin bottom-left),
  animatable like any Crop; GenFill reads it at the reference frame.
- Per-box data lives on the Crop node as added knobs: `gf_box_prompt` (String, the box's text),
  `gf_box_enabled` (Boolean, default True). Colour: the Crop's `gl_color` (Viewer overlay colour)
  and `tile_color`, both set from a fixed 8-colour palette by N (red, green, blue, yellow, magenta,
  cyan, orange, violet; colour N = palette[(N-1) % 8]).
- Module `genfill/boxes.py` (Nuke-facing, tested against `tests/fake_nuke.py`):
  `list_boxes(group) -> [BoxInfo(n, node_name, rect(x,y,r,t), prompt, enabled, rgb)]`,
  `add_box(group, rect=None) -> n` (default rect = the selection bbox if a probe exists, else the
  centre 30 % of the source format), `remove_box(group, n)`, `set_prompt(group, n, text)`,
  `set_enabled(group, n, on)`, `palette(n) -> (r, g, b)` floats and `palette_hex(n)`,
  `attach_box_tools(group)` / `detach_box_tools(group)`.

### 3.2 Seeing all boxes at once

- Nuke draws a node's knob handles only while that node's control panel is open, in the node's
  `gl_color`. While the GenFill panel is open, GenFill opens every enabled box Crop's control
  panel as a floating window and hides the window (the exact technique already used for the
  selection Roto: `showControlPanel(forceFloat=True)` + hiding the top-level widget whose
  objectName is the node name). All boxes then show in the Viewer at once, each in its colour,
  draggable with Nuke's own Crop handles. Detach closes those panels (`hideControlPanel`).
- Robust fallback that works on any Viewer: a **colour box overlay in the GenFill output**:
  per box an internal `Rectangle` outline (outer Rectangle minus inner Rectangle 3 px smaller,
  colour = palette, area linked by expression to `GenFillBox<N>.box`) merged over the output by a
  `Switch` whose `which` is the expression `$gui && parent.gf_show_boxes` — so it is visible in
  the interactive Viewer only and never in renders. Group knob `gf_show_boxes` (Boolean, default
  True, label "show boxes in Viewer"). The overlay subgraph is rebuilt whenever boxes are added
  or removed (`boxes.rebuild_overlay(group)`).
- Old nodes: `ensure_current` adds `gf_show_boxes` and the overlay Switch (with no boxes it is a
  pass-through).

### 3.3 Panel

A "Region prompts" section under the main prompt:

- "+ Add box" button and, per box, a row: colour swatch (palette colour), tag label `<obj_N>`
  (click inserts the tag at the cursor in the main prompt), a one-line prompt edit (the box
  description), enable checkbox, delete (x) button.
- The section is enabled only when the current backend + model supports layouts
  (`backend.layout_style(model)` is not None); otherwise greyed out with the tooltip
  "Region prompts work with FLUX 3 and Ideogram 4.5 models." Boxes stay stored either way.
- Hint text: "Box text describes what goes in the box. Cite a box in the main prompt as <obj_N>."

### 3.4 Engine

- `GenerationRequest.boxes`: list of `layout.Box(n, x0, y0, x1, y1, prompt)` in **crop pixel
  coordinates of the image sent to the model, origin top-left**, clipped to the crop; boxes with
  an empty prompt or disabled are skipped; a box fully outside the crop is dropped with a log
  warning and a status-line note.
- Region: when boxes exist and the backend/model supports layouts, the crop region is computed
  from the union of the selection bbox and all enabled boxes (before context expansion), so boxes
  are inside the crop.
- Selection: if the selection is empty but enabled boxes exist (and the model supports layouts),
  the union of the boxes is the selection (internal `GenFillBoxMask`: filled Rectangles merged
  `max` into the mask path by a Switch whose `which` is driven by the engine before rendering).
  Otherwise the existing "Nothing is selected yet" error stays.
- Implicit box: for **FLUX 3** layout styles, when the artist has no boxes, GenFill adds the
  selection bbox as box `obj_1` with the main prompt as description and prefixes the prompt with
  "In <ref_image_0>, " + prompt + " <obj_1>" so reference-edit FLUX 3 knows where to edit. With
  explicit boxes the main prompt is sent as typed (prefixed "In <ref_image_0>, " when it does not
  mention `<ref_image_0>`).
- `gf_info`, state per generation: boxes stored in the generation record (`"boxes"`: list of
  dicts) so the history shows what was sent.

### 3.5 Formats (`genfill/backends/layout.py`, pure Python)

- `Box(n, x0, y0, x1, y1, prompt)`, `norm1000(box, w, h) -> [y0, x0, y1, x1]` ints 0-1000
  (round, clamp, y0<y1, x0<x1).
- `STYLES = ("flux3", "flux3_fields", "ideogram")`.
- `flux3_prompt(prompt, boxes, w, h)`: `f"{prompt.rstrip()} {json.dumps(rows, ensure_ascii=False)}"`
  with rows `[{"id": "obj_N", "bbox": [y0,x0,y1,x1], "desc": box.prompt}, ...]` (a space before
  the JSON, `json.dumps` default separators, non-ASCII kept, ids by box n). Byte-identical to
  `Flux3LayoutPrompt` in ComfyUI 0.41.3 (`comfy_extras/nodes_flux.py`). Source: BFL layout
  prompting guide.
- `flux3_fields(boxes, w, h)`: fal FLUX 3 Klein native `bounding_boxes` list:
  `{"label": "obj_N", "description": text, "box": {"x_min","y_min","x_max","y_max"}}` fractions
  0-1, x first. Prompt cites `<obj_N>`.
- `ideogram_prompt(prompt, boxes, w, h)`: Ideogram 4.5 JSON prompt:
  `{"high_level_description": prompt, "compositional_deconstruction": {"background":
  "Keep the existing image outside the boxes unchanged.", "elements": [{"type": "obj",
  "bbox": [y0,x0,y1,x1], "desc": text}, ...]}}` as a JSON string; `<obj_N>` tags in the main
  prompt are replaced by the box's text in parentheses (Ideogram has no citation syntax).
  Callers that have a magic-prompt switch turn it off when boxes are sent.
- `apply(style, prompt, boxes, w, h) -> (prompt_text, extra_fields)`; no boxes -> prompt
  unchanged (except the FLUX 3 implicit box, done by the engine), `{}`.

### 3.6 Backends

`Backend.layout_style(model) -> str | None` (default: `spec.extra.get("layout")`).
Set `layout` on these catalogue entries (and on discovered models whose id matches
`flux-3|flux_3|flux3` -> flux3, `ideogram.*4[-_.]5|ideogram.*v4` -> ideogram):

| Backend | Models | Style | How |
|---|---|---|---|
| ComfyUI | `flux3_klein`, `flux3_klein_upsampler`, `api_flux3_image` | flux3 | sidecar `"layout": "flux3"`; prompt text through `GenFill Prompt` |
| Magnific | `flux-3-image` | flux3 | prompt text |
| Magnific | `ideogram-4-5`, `retouch-ideogram-4-5`, `retouch-precise-ideogram-4-5` | ideogram | prompt text |
| Higgsfield | `flux_3_image` | flux3 | prompt text |
| Higgsfield | `ideogram_4_5` | ideogram | prompt text |
| fal | `blackforestlabs/flux-3/edit-image` | flux3 | prompt text |
| fal | new `blackforestlabs/flux-3/klein/edit-image` (edit, `image_urls`) | flux3_fields | `bounding_boxes` field |
| fal | `ideogram/v4.5/edit` | ideogram | prompt text |

Ideogram box handling and FLUX 3 boxes on Magnific / Higgsfield / fal are **unverified** until a
live run; README and CHANGELOG say so. Comfy Cloud has no FLUX 3 / Ideogram 4.5 entries today; not
added (out of scope).

## 4. FLUX 3 Klein ComfyUI workflows

Files in `nuke/workflows/comfyui/` (API format, node-title convention):

- `flux3_klein.json`: `UNETLoader` (`flux3-image-klein-int8-convrot.safetensors`, default dtype),
  `CLIPLoader` (`qwen3vl_4b_int8_convrot.safetensors`, type `flux3`), `VAELoader`
  (`flux2-vae.safetensors`), `LoadImage` "GenFill Image", `CLIPTextEncode` "GenFill Prompt",
  `ConditioningZeroOut` (negative), `ReferenceMultiLatent` (resolution "keep resolution", image0 =
  GenFill Image), `KSampler` "GenFill Seed" (16 steps, cfg 1, euler, simple, denoise 1),
  `VAEDecode`, `SaveImage` "GenFill Output".
- `flux3_klein_upsampler.json`: same plus `CLIPLoader` for
  `qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors` (type flux3) and `TextGenerate` (system
  prompt copied from the official template `image_flux3_klein_distilled_image_edit`, image = GenFill
  Image, prompt = GenFill Prompt node output) feeding `CLIPTextEncode`. The "GenFill Prompt" node
  is then a `PrimitiveStringMultiline` (value input) so GenFill patches it.
- `api_flux3_image.json`: Comfy API node `Flux3ImageNode` (prompt, images.image_1 = GenFill
  Image), `api_nodes: true`.
- Sidecars: `kind: "edit"`, `mask: false` (new key: no `GenFill Mask` node required), `layout:
  "flux3"`, `family: "Black Forest Labs"`, pixels 1048576, multiple 16, min_side 256, max_side
  4096 (Klein) / per API, resolutions follow the existing sidecar rules, `requires` lists the
  files, `empty_prompt`: none (the engine's empty-prompt text for edit models is used).
- `comfy_workflows.validate`: `GenFill Mask` is required unless the sidecar says `mask: false`;
  `patch` skips the mask when there is no mask node; the ComfyUI backend skips the mask upload
  when the workflow has no mask node.
- README: new section with the FLUX 3 Klein file list, HF repo (gated, accept the licence), the
  FLUX Kommunity licence note (non-commercial / < US$5M revenue), ComfyUI >= 0.41.

## 5. Testing

- Unit: families inference table; grouped combo items (offscreen Qt where available, else the
  pure helper that builds the item list); PromptEdit key handling; layout formatters (exact JSON,
  coordinate flips, clamping, ids); each backend sends the layout (fake HTTP servers / existing
  fakes); boxes module against fake_nuke (add/remove/lowest free N/colours/prompt knobs);
  engine box conversion to crop coordinates (Nuke bottom-left -> top-left) and region union;
  workflow validation with `mask: false`; patching the Klein workflows.
- Live: FLUX 3 Klein workflow on the local ComfyUI (after the model download) with two boxes;
  `POST /prompt` validation of all three new workflows; Magnific `flux-3-image` with a box and
  `ideogram-4-5` with a box (cheap paid runs), via `installers/live_check.py --boxes`.
- Not testable here (no Nuke): Crop handle colours (`gl_color`) and the floating hidden panels.
