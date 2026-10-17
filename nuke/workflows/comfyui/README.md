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
| `GenFill Mask` | `LoadImageMask` (channel red, white = fill) or `LoadImage` (mask from alpha) | the mask (required, unless the sidecar says `"mask": false`) |
| `GenFill Prompt` | node with a `text` / `prompt` / `value` / `string` input | the prompt |
| `GenFill Negative` | same | the negative prompt (optional) |
| `GenFill Seed` | node with `seed` / `noise_seed` / `value` | without one, every `seed` / `noise_seed` input gets the seed |
| `GenFill Output` | `SaveImage` / `PreviewImage` | result to read; without one, every `SaveImage` |

An erase workflow has no `GenFill Prompt` node; it carries a fixed prompt or none.

## Sidecar keys (`<id>.genfill.json`)

| Key | Meaning |
|---|---|
| `label`, `description` | shown in the model list |
| `kind` | `inpaint` (default), `erase`, or `edit` (re-renders the crop from the image; GenFill comps the result back through the selection) |
| `mask` | `false`: the graph has no `GenFill Mask` node and GenFill uploads no mask (default `true`) |
| `layout` | region prompt style: `flux3` appends the region boxes to the `GenFill Prompt` text as FLUX 3 layout rows |
| `family` | group of the workflow in the model version menu (e.g. `Black Forest Labs`) |
| `empty_prompt` | id of the erase workflow GenFill uses when the prompt is empty |
| `pixels`, `multiple` | pixel budget and size multiple of the crop sent to the model (defaults 1048576 and 64). `pixels` is the budget for *auto* and *1K*; *2K* and *4K* use 4 and 16 times it, never more than the crop's own pixel count |
| `min_side`, `max_side` | side limits of that crop (defaults 512 and 2048). `max_side` also sets the resolutions the workflow offers: 1K always, 2K when `max_side` is at least 2048, 4K when it is at least 4096; a higher setting runs at the largest offered tier ("model max 2K") |
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
| `flux3_klein`, `flux3_klein_upsampler` | local FLUX 3 Image Klein, see below |
| `api_flux3_image` | Comfy account API key (Black Forest Labs FLUX 3 Image, 1K output) |

### Model files for `flux_fill` / `flux_fill_erase`

| File | Folder |
|---|---|
| `flux1-fill-dev.safetensors` | `ComfyUI/models/diffusion_models/` |
| `clip_l.safetensors` | `ComfyUI/models/text_encoders/` |
| `t5xxl_fp8_e4m3fn.safetensors` | `ComfyUI/models/text_encoders/` |
| `ae.safetensors` | `ComfyUI/models/vae/` |

`flux1-fill-dev` is gated on Hugging Face (black-forest-labs/FLUX.1-Fill-dev); accept the license first.

### FLUX 3 Image Klein: `flux3_klein` / `flux3_klein_upsampler` / `api_flux3_image`

Reference edits (`kind: edit`, no mask node): the model re-renders the crop from the plate and
the prompt, and GenFill comps the result back through the selection. Region boxes go into the
prompt as FLUX 3 layout rows (`layout: flux3`); cite a box as `<obj_N>` in the prompt.
`flux3_klein` runs the distilled Klein model (16 steps, cfg 1); `flux3_klein_upsampler` first
expands the prompt with BFL's prompt upsampler (a Qwen 3.5 9B text model that looks at the
image), as BFL recommends for Klein. Needs ComfyUI 0.41 or newer.

| File | Folder | Workflow |
|---|---|---|
| `flux3-image-klein-int8-convrot.safetensors` | `ComfyUI/models/diffusion_models/` | both |
| `qwen3vl_4b_int8_convrot.safetensors` | `ComfyUI/models/text_encoders/` | both |
| `flux2-vae.safetensors` | `ComfyUI/models/vae/` | both |
| `qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors` | `ComfyUI/models/text_encoders/` | `flux3_klein_upsampler` |

All files are in the Hugging Face repo black-forest-labs/flux-3-image-klein-comfy. It is gated:
sign in and accept the license (approval is automatic). FLUX 3 Image Klein is under the **FLUX
Kommunity license**: non-commercial use, or commercial use below US$5M annual revenue; read the
license before production use.

`api_flux3_image` needs no model files: it calls the Comfy API node FLUX 3 Image (Black Forest
Labs) with the crop as reference image at 1K output and web grounding off, and bills your Comfy
account.

Speed: on an NVIDIA GPU a 1 MP Klein edit takes seconds. On Apple Silicon it is slow (an M4 with
24 GB needs about 4 minutes at 0.25 MP and runs out of memory at 1 MP, past the 30 minute
timeout); if a run stops with "Timed out waiting for ComfyUI", copy
the workflow and its sidecar into your studio workflow folder (`GENFILL_COMFY_WORKFLOWS`) and
raise `timeout` there (seconds; the shipped value is 1800).
