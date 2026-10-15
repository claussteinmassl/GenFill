# GenFill ComfyUI workflows

Each `<id>.json` here is a ComfyUI graph in **API format** (ComfyUI: Workflow > Export (API)).
GenFill lists every workflow it finds in this folder (and in your own workflow folder) as a model.
An optional `<id>.genfill.json` next to it holds the settings.

## Title convention

GenFill finds its inputs by node title (`_meta.title`, case-insensitive). Rename nodes in ComfyUI
(right click > Title) before exporting.

| Title | Node | Purpose |
|---|---|---|
| `GenFill Image` | `LoadImage` | the plate crop (required) |
| `GenFill Mask` | `LoadImageMask` (channel red, white = fill) or `LoadImage` (mask from alpha) | the mask (required) |
| `GenFill Prompt` | node with a `text` / `prompt` / `value` / `string` input | the prompt |
| `GenFill Negative` | same | the negative prompt (optional) |
| `GenFill Seed` | node with `seed` / `noise_seed` / `value` | without one, every `seed` / `noise_seed` input gets the seed |
| `GenFill Output` | `SaveImage` / `PreviewImage` | result to read; without one, every `SaveImage` |

An erase workflow has no `GenFill Prompt` node; it carries a fixed prompt or none.

## Sidecar keys (`<id>.genfill.json`)

| Key | Meaning |
|---|---|
| `label`, `description` | shown in the model list |
| `kind` | `inpaint` (default) or `erase` |
| `empty_prompt` | id of the erase workflow GenFill uses when the prompt is empty |
| `pixels`, `multiple` | pixel budget and size multiple of the crop sent to the model |
| `min_side`, `max_side` | side limits of that crop |
| `api_nodes` | `true` if the graph uses Comfy API nodes (needs a Comfy account API key, costs credits) |
| `timeout` | seconds to wait for a result |
| `requires` | model files the workflow needs, relative to the ComfyUI folder (informational) |

## Adding a workflow

1. Build and test the graph in ComfyUI; title the nodes as above.
2. Export it with Workflow > Export (API) into this folder (or your own workflow folder).
3. Add a `<id>.genfill.json` sidecar. Check that it loads: the model list shows "Not usable: ..." otherwise.

## Shipped workflows

| Id | Needs |
|---|---|
| `flux_fill`, `flux_fill_erase` | local FLUX.1 Fill dev, see below |
| `api_flux_pro_fill`, `api_flux_erase` | Comfy account API key (Black Forest Labs) |
| `api_bria_genfill`, `api_bria_eraser` | Comfy account API key (Bria) |
| `api_recraft_inpaint` | Comfy account API key (Recraft) |

### Model files for `flux_fill` / `flux_fill_erase`

| File | Folder |
|---|---|
| `flux1-fill-dev.safetensors` | `ComfyUI/models/diffusion_models/` |
| `clip_l.safetensors` | `ComfyUI/models/text_encoders/` |
| `t5xxl_fp8_e4m3fn.safetensors` | `ComfyUI/models/text_encoders/` |
| `ae.safetensors` | `ComfyUI/models/vae/` |

`flux1-fill-dev` is gated on Hugging Face (black-forest-labs/FLUX.1-Fill-dev); accept the license first.
