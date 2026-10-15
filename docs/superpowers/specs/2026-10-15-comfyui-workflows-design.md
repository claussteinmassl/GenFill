# ComfyUI backend with repo workflows (replaces the built-in Local AI)

Date: 2026-10-15. Status: approved decisions below, design by Claude.

## Goal

GenFill runs generative fill on **any ComfyUI instance** (same machine, a machine on the
network, RunPod or another cloud box) using **predefined workflows stored in the repo** (and
optionally in a studio folder). The artist picks a workflow from the model version dropdown.
The built-in Local AI helper (own Python env, FLUX/LaMa, ~25 GB installer) is removed.

## User decisions

- Workflow binding: **node title convention** in Comfy API-format JSON, optional sidecar for
  label/description/size.
- Old Local AI: **remove**; ComfyUI replaces it. Old nodes on "Local" move to ComfyUI.
- Shipped workflows: **FLUX.1 Fill [dev]**, **Erase/Remove**, **Comfy API nodes** (Flux Pro
  Fill, Flux Erase, Bria GenFill, Bria Eraser, Recraft inpaint).
- Live test: download **one small model** (SDXL inpainting, ~7 GB) into the local ComfyUI
  (`~/ComfyUI-Installs/ComfyUI/ComfyUI`) for an end-to-end run; FLUX only validated (dry).

## Server connection

| Setting | Env var (wins) | config.json | Default |
|---|---|---|---|
| Server URL (scheme, host, port, optional path prefix) | `GENFILL_COMFY_URL` | `comfyui.url` | `http://127.0.0.1:8188` |
| Optional auth token (reverse proxy, RunPod with auth) | `GENFILL_COMFY_TOKEN` | API key slot `comfyui` | none |
| Comfy.org account key for API nodes | `COMFY_API_KEY` (shared with Comfy Cloud) | key slot `comfy` | none |
| Extra workflow folders (`os.pathsep` list) | `GENFILL_COMFY_WORKFLOWS` | `comfyui.workflow_dirs` | none |

- The token, if set, is sent as `Authorization: Bearer <token>`. The Comfy.org key goes into
  `extra_data.api_key_comfy_org` of `/prompt` (that is how API nodes on a self-hosted ComfyUI
  are billed), only when the workflow uses API nodes.
- The *API Keys & Accounts* dialog gets a ComfyUI section: URL field, token field and a
  **Test connection** button (`GET /system_stats`, shows version and device). Env values show
  as read-only with their source, like keys today.

## Workflows

Location: `nuke/workflows/comfyui/` (inside the `nuke` folder, so copying that folder into a
pipeline brings them along), plus every folder in `GENFILL_COMFY_WORKFLOWS`. A studio folder
overrides a repo workflow with the same id.

One workflow = `<id>.json` (Comfy **API format**, "Export (API)") + optional
`<id>.genfill.json`:

```json
{"label": "FLUX.1 Fill [dev]", "description": "...", "kind": "inpaint",
 "pixels": 1048576, "multiple": 16, "min_side": 512, "max_side": 2048,
 "empty_prompt": "flux_fill_erase", "requires": ["flux1-fill-dev.safetensors", "..."],
 "api_nodes": false}
```

Missing sidecar: label = id, kind inpaint, 1 MP on a 64 grid.

### Node title convention (`_meta.title`, case-insensitive)

| Title | Node | GenFill sets |
|---|---|---|
| `GenFill Image` | `LoadImage` | `image` = uploaded plate crop |
| `GenFill Mask` | `LoadImageMask` (channel red) or `LoadImage` | `image` = uploaded mask (white = fill) |
| `GenFill Prompt` | any node with a string input `text` / `prompt` / `value` / `string` | the prompt |
| `GenFill Negative` | same | negative prompt (optional) |
| `GenFill Seed` | any node with `seed` / `noise_seed` / `value` | the seed. Without such a node, every `seed` / `noise_seed` input in the graph gets it |
| `GenFill Output` | `SaveImage` / `PreviewImage` | results are read from it; without it, from every `SaveImage` |

`GenFill Image` and `GenFill Mask` are required (load-time validation with a clear message);
the rest is optional. Variations: the graph is queued N times with seeds `seed + i` (random
base seed when 0), so any workflow works without batch logic.

## Backend `comfyui`

Label "ComfyUI (local, network or cloud)". Stdlib HTTP only.

1. `POST /upload/image` (multipart; plate and mask, unique names under subfolder `genfill`).
2. For each variation: patch a copy of the graph, `POST /prompt` with a fixed `client_id`;
   keep the `prompt_id`s. A 400 with `node_errors` becomes a readable error, and
   "value not in list" on a loader names the missing model file and its folder.
3. Poll `GET /history/<id>` (cancellable, 1 s); progress from finished count plus queue position
   (`GET /queue`). On cancel: `POST /queue {"delete": [...]}` for pending ids and
   `POST /interrupt` if one of ours is running.
4. Download each output image with `GET /view?filename=&subfolder=&type=`.
5. Paid-results policy as for the cloud backends (`collect` / `finish`).

`model_size` follows the selected workflow's sidecar. The catalogue (version dropdown) is
built from the workflow folders at call time (cheap: directory listing, parsed once per mtime).
The empty-prompt rule: if the prompt is empty and the workflow names `empty_prompt`, that
workflow runs instead.

## Removing Local AI

Delete `backends/local.py`, `localai/`, `ui/localai_dialog.py`, Local AI menus and panel
button, `tests/test_localai.py`, README sections. `node.ensure_current` maps an old "Local"
backend menu value to ComfyUI with the FLUX Fill workflow. Float TIFF transport goes away with
it (ComfyUI loads 8-bit PNG); the colour pipeline is unchanged.

## Shipped workflows

| id | Nodes | Needs |
|---|---|---|
| `flux_fill` | FLUX.1 Fill [dev]: UNETLoader, DualCLIPLoader, VAELoader, InpaintModelConditioning, KSampler | `flux1-fill-dev.safetensors`, `clip_l`, `t5xxl_fp8_e4m3fn`, `ae.safetensors` |
| `flux_fill_erase` | same graph, fixed "clean background" prompt, prompt ignored | same |
| `api_flux_pro_fill` | FluxProFillNode | Comfy.org key |
| `api_flux_erase` | FluxEraseNode | Comfy.org key |
| `api_bria_genfill` / `api_bria_eraser` | BriaGenFill / BriaEraser | Comfy.org key |
| `api_recraft_inpaint` | RecraftImageInpaintingNode | Comfy.org key |

Default workflow: `flux_fill`. A test-only workflow for SDXL inpainting lives in
`tests/fixtures/comfyui/` and is used by the live check.

## Testing

- Unit: workflow loading/validation, graph patching, error formatting, a fake ComfyUI HTTP
  server (upload, prompt, history, view, queue, interrupt) for the full backend flow, cancel.
- Live: `installers/live_check.py comfyui <workflow>` against the local ComfyUI with the SDXL
  test workflow (real images), plus `POST /prompt` validation of the shipped FLUX workflows
  (expected: only "model missing" errors, no graph errors).
