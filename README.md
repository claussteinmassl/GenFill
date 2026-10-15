# GenFill: Generative Fill for Nuke

Photoshop's Generative Fill as a Nuke node. Draw a selection on the node, type what you want (or
leave the prompt empty to remove what's there), click **Generate** and pick from the variations.
The plate never leaves your color-managed, scene-linear comp: no round trip through Photoshop,
no 8-bit sRGB export, no clipped highlights.

![node](nuke/icons/GenFill.png)

- **One node, one panel.** The selection, prompt, variations, blend controls and output all
  live on the GenerativeFill node. The Roto tools appear in the Viewer as soon as its panel is open.
- **Free and local through ComfyUI.** Point GenFill at a ComfyUI on your machine, your network or
  a RunPod pod and run any inpainting workflow, such as the shipped FLUX.1 Fill. Nothing is
  uploaded with a local server and there are no tokens. Cloud models (Stability AI, OpenAI,
  fal.ai, Magnific, Higgsfield, Comfy Cloud) are optional.
- **Linear-safe.** An invertible color encoding means the model sees a display-like image and
  its result is decoded back into your working space, with the plate's highlights above 1.0 outside the fill untouched. See
  [Color science](#color-science-a-linear-pipeline-with-display-referred-models).
- **Farm-safe.** The node is a Group of native Nuke nodes. Once you've picked a variation, the
  script renders anywhere, even where GenFill isn't installed.

Current version: **0.9.0** (see [CHANGELOG.md](CHANGELOG.md)).

---

## Contents

- [Photoshop vs GenFill](#photoshop-vs-genfill)
- [Install](#install)
- [Quick start](#quick-start)
- [The node](#the-node)
- [Models](#models)
- [Color science: a linear pipeline with display-referred models](#color-science-a-linear-pipeline-with-display-referred-models)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Development](#development)
- [Licenses](#licenses)

---

## Photoshop vs GenFill

| Photoshop | GenFill in Nuke |
|---|---|
| Lasso / marquee selection | **✏ Draw Selection**: Roto shapes drawn on the node itself, with the shape list in the same panel. Or use any matte in the **Mask** input, or the Source alpha |
| Contextual task bar: prompt + **Generate** | The same bar at the top of the node's properties. Enter generates |
| Empty prompt = remove and fill from surroundings | Same. ComfyUI: the workflow's erase partner (e.g. FLUX.1 Fill with a fixed empty-background prompt). Stability AI: their *Erase* model |
| 3 variations per click, browse with ‹ › | Same: thumbnail grid of every generation (newest first), ‹ 1/6 › arrows; right-click to reuse a prompt, reveal or delete |
| Generative layer with a layer mask | **output: Comp** (fill blended over the plate through the expanded, feathered selection) or **Layer** (just the fill, in the plate's format, premultiplied by that selection) |
| 8-bit sRGB document | Scene-linear float. The model sees an invertible encoding of the plate and its result is decoded back |

---

## Install

### 1. The plugin

Double-click **`installers\install.bat`**. It:

1. runs the unit tests with Nuke's own Python (and installs nothing if they fail),
2. copies the build to `%USERPROFILE%\.nuke\plugins\GenFill`, stamped with version and git commit,
3. adds one managed block to your `.nuke\init.py` (backed up first).

Run it again whenever you want the latest build. Restart Nuke afterwards. Details, dev mode
(live from the repo) and uninstall are in [installers/README.md](installers/README.md).

Supported: Nuke 13 to 17 (Python 3, PySide2 or PySide6). The installer is Windows-only for now;
on macOS/Linux add the repo's `nuke` folder to `NUKE_PATH`.

### 2. ComfyUI (optional, recommended)

GenFill runs workflows on a ComfyUI server. Install [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
yourself (desktop app, portable build or a RunPod template), start it, then open
**GenFill > API Keys & Accounts...** and look at the **ComfyUI** group.

| Setting | Meaning |
|---|---|
| URL | Default `http://127.0.0.1:8188` (ComfyUI on this machine). Another machine on your network: `http://192.168.1.20:8188`. RunPod: `https://<pod-id>-8188.proxy.runpod.net`. Set by `GENFILL_COMFY_URL`, or `comfyui.url` in the config file |
| Token | Optional. Sent as `Authorization: Bearer <token>` for servers behind a reverse proxy. `GENFILL_COMFY_TOKEN` |
| Workflow folders | The bundled folder first, then `comfyui.workflow_dirs` from the config file, then the folders in `GENFILL_COMFY_WORKFLOWS` (a list separated by `;` on Windows, `:` elsewhere). Later folders override earlier ones with the same workflow id, so a studio can replace a shipped workflow |

The dialog has a **Test connection** button. Pick **ComfyUI** in the Model menu on the node; the
version menu lists every usable workflow, and a workflow that cannot load shows as "Not usable"
with the reason.

**Upgrading.** GenFill 0.9 removed the built-in local model (its ~25 GB, Windows-only installer
and helper process); ComfyUI replaces it. Nodes that were set to the old local model switch to
ComfyUI automatically the next time they load. The old helper folder in your user profile is no
longer used and can be deleted (see the [CHANGELOG](CHANGELOG.md)).

#### Model files for the shipped workflows

The workflows are listed in
[nuke/workflows/comfyui/README.md](nuke/workflows/comfyui/README.md). Put the model files into
the folders of the ComfyUI that runs them (on RunPod: the pod's ComfyUI folder).

| Workflow | Needs |
|---|---|
| `flux_fill`, `flux_fill_erase` (default `flux_fill`) | `flux1-fill-dev.safetensors` in `models/diffusion_models/`, `clip_l.safetensors` and `t5xxl_fp8_e4m3fn.safetensors` in `models/text_encoders/`, `ae.safetensors` in `models/vae/`. `flux1-fill-dev` is gated on Hugging Face (black-forest-labs/FLUX.1-Fill-dev): accept the license first. **Non-commercial license** |
| `api_flux_pro_fill`, `api_flux_erase`, `api_bria_genfill`, `api_bria_eraser`, `api_recraft_inpaint` | No model files. These use Comfy API nodes (partner models) and bill your Comfy.org account: set `COMFY_API_KEY` (the same key as Comfy Cloud) or add it under API Keys & Accounts |

#### Your own workflows

Export a graph from ComfyUI with **Workflow > Export (API)**, title its input and output nodes
and drop it into a workflow folder. GenFill finds the nodes by title (case-insensitive):

| Node title | Node | Purpose |
|---|---|---|
| `GenFill Image` | `LoadImage` | the plate crop (required) |
| `GenFill Mask` | `LoadImageMask` (channel red, white = fill) or `LoadImage` (mask from alpha) | the mask (required) |
| `GenFill Prompt` | node with a `text` / `prompt` / `value` / `string` input | the prompt |
| `GenFill Negative` | same | the negative prompt (optional) |
| `GenFill Seed` | node with `seed` / `noise_seed` / `value` | without one, every `seed` / `noise_seed` input gets the seed |
| `GenFill Output` | `SaveImage` / `PreviewImage` | the result to read; without one, every `SaveImage` |

An optional `<id>.genfill.json` next to the workflow sets the label, the crop size, and the
erase partner used for an empty prompt. The full format and the sidecar keys are in
[nuke/workflows/comfyui/README.md](nuke/workflows/comfyui/README.md).

### 3. Cloud keys and accounts (optional)

Open **GenFill > API Keys & Accounts...** (also on the node: *API Keys & Accounts...*).

| Provider | How to connect |
|---|---|
| Stability AI, OpenAI | Paste the key, or set `STABILITY_API_KEY` / `OPENAI_API_KEY` |
| fal.ai | Paste the key, or set `FAL_KEY` |
| Comfy Cloud | Optional API key (`COMFY_API_KEY`; keys start with `comfyui-`), or **Sign in** |
| Magnific, Higgsfield | **Sign in**: your browser opens once; tokens then refresh silently |

Keys and sign-in tokens are stored in `~\.nuke\genfill\config.json`, never in .nk scripts.
**Generate** also starts the sign-in automatically if it's needed, so you can skip the dialog.

### 4. Pipeline setup (environment variables)

Everything a studio deploys can come from environment variables, which a pipeline tool sets
when it launches Nuke or opens a shot. GenFill reads them on **every Generate**, so changing
them per shot (e.g. `os.environ["SHOT"] = "sh020"` in an open-script callback) needs no restart.

| Variable | Meaning |
|---|---|
| `STABILITY_API_KEY`, `OPENAI_API_KEY`, `FAL_KEY`, `COMFY_API_KEY` | API keys (`COMFY_API_KEY` also pays for ComfyUI workflows that use Comfy API nodes). They win over keys saved in the dialog. Magnific and Higgsfield have no API keys (browser sign-in only) |
| `GENFILL_COMFY_URL` | ComfyUI server URL. Wins over the URL saved in the dialog |
| `GENFILL_COMFY_TOKEN` | Bearer token for a protected ComfyUI server |
| `GENFILL_COMFY_WORKFLOWS` | Extra ComfyUI workflow folders, separated by `;` on Windows and `:` elsewhere. Added after the bundled folder and `comfyui.workflow_dirs`; a later folder wins on the same workflow id |
| `GENFILL_CACHE` | Root folder for results. Used unless the node's *cache folder* knob is set |
| `GENFILL_NAME` | Name of each generation's folder. Default `g{gen:03d}_{date}-{time}` |
| `GENFILL_VARIATION_NAME` | File name of each variation (without extension). Default `var_{var:02d}` |
| `GENFILL_LOG` | Log file path |

The three path and name variables are **patterns**: they can use any environment variable as
`$VAR`, `${VAR}` or `%VAR%` (on every platform), plus these tokens, with optional
[format](https://docs.python.org/3/library/string.html#formatspec) such as `{gen:04d}`:

| Token | Value | In |
|---|---|---|
| `{script_dir}` | Folder of the .nk script (temp folder if unsaved) | cache |
| `{script}` | Script name without `.nk` (`untitled` if unsaved) | all |
| `{node}`, `{uid}` | Node name, node id | all |
| `{user}` | Login name | all |
| `{gen}` | Generation number, given by GenFill | name, variation |
| `{date}`, `{time}` | `20261015`, `143000` | name, variation |
| `{frame}`, `{backend}`, `{model}` | Reference frame, backend id (`fal`), model id (`default` if empty) | name, variation |
| `{name}`, `{var}` | The generation's folder name, variation number | variation |

Example: a pipeline tool sets `SHOW`, `SHOT` and `TASK`, and the studio's Nuke environment sets

```
GENFILL_CACHE=/jobs/$SHOW/$SHOT/comp/genfill/{node}
GENFILL_NAME=${SHOT}_${TASK}_genfill_v{gen:03d}
GENFILL_VARIATION_NAME={name}_var{var:02d}
```

which writes `/jobs/demo/sh010/comp/genfill/GenerativeFill1/sh010_comp_genfill_v001/sh010_comp_genfill_v001_var01.png`.

- GenFill always numbers generations itself, and never reuses a number. If `GENFILL_NAME` has no
  `{gen}` (or `GENFILL_VARIATION_NAME` no `{var}`), the number is appended.
- A cache pattern without `{uid}` or `{node}` gets a folder per node (`<root>/<node id>`), so two
  nodes never mix their results. A relative cache path is taken from the script's folder.
- Names keep only letters, digits, `.`, `_` and `-`; anything else becomes `_`. An unset
  variable expands to nothing and is logged. An unknown token stops Generate with a message
  before anything is rendered or paid for.
- Without a pipeline the same settings can go into `~/.nuke/genfill/config.json` as
  `cache_root` (unsaved scripts only), `name_pattern` and `variation_pattern`; environment
  variables win.

### Check the build

**GenFill > Developer > Run Smoke Test** builds a throwaway setup, generates with the offline
backend and writes a report plus images to `test_output\`. **GenFill > Developer > Build Info**
shows which build is loaded.

---

## Quick start

1. Select your plate, create **GenFill > Generative Fill** (or Tab → "Generative Fill") and view it.
2. With its panel open, the Roto tools are in the Viewer. Draw around what you want to replace.
   Edit, toggle or animate shapes in the **shapes** list right in the panel.
3. Type a prompt (*"a weathered wooden bench"*) or leave it empty to remove the object. Press
   **Generate** (or Enter).
4. Click thumbnails or use ‹ › to pick a variation. Generate again to add more.
5. Adjust **expand** / **feather** to blend, and set **output** to *Comp* or *Layer*.

Prefer your own matte? Connect it to the **Mask** input and *selection from* switches to it
automatically when no shapes are drawn.

The fill is generated on the current frame and held. The *reference frame* (Settings tab) shows
which frame a variation came from.

---

## The node

```
         Mask (optional)
            │
Source ── GenerativeFill ── output
```

### Generative Fill tab

| Control | Meaning |
|---|---|
| Prompt bar + **Generate** | What to generate. Empty = remove. Enter generates |
| Model menu | Backend picker (ComfyUI, cloud providers) |
| Variations | Thumbnails of every generation; ‹ › to browse; right-click for reuse prompt / reveal / delete |
| **selection from** | *Shapes (built-in Roto)* (default), *Mask input*, or *Source alpha* |
| **✏ Draw Selection** | Switches to Shapes and makes sure the Roto tools are in the Viewer |
| **shapes** | The built-in Roto's shape list (add, rename, hide, lock, animate, delete) |
| **expand** | Grow the selection before generating, in pixels (like Select > Modify > Expand) |
| **feather** | Soften the edge where the fill is blended back |
| **output** | *Comp*: plate with the fill blended in. *Layer*: only the fill, premultiplied by the expanded, feathered selection, in the plate's format. *Bypass*: the plate |

### Settings tab

| Knob | Meaning |
|---|---|
| model / model version | Backend, and the model of a cloud backend: pick from the version menu next to *Model* (empty = default) or type any id the provider accepts (e.g. `gpt-image-1.5`) |
| quality | Speed vs quality (OpenAI: quality level; ComfyUI workflows ignore it) |
| variations | Variations per click (default 3, like Photoshop) |
| seed | 0 = new random seed each click |
| negative prompt | Things to avoid (Stability, and ComfyUI workflows with a `GenFill Negative` node) |
| context | How much surrounding image the model sees, as a fraction of the selection size (default 0.35) |
| min region | Smallest crop sent, in plate pixels (default 512) |
| **color pipeline** | How the plate is encoded for the model. See [Color science](#color-science-a-linear-pipeline-with-display-referred-models) |
| display colorspace | The display transform to use (`auto` = picked from your OCIO config, e.g. `Output - Rec.709`) |
| custom colorspace | Used when *color pipeline* is *Custom* |
| cache folder | Where results are stored. Empty = `GENFILL_CACHE` if set, else `<script folder>/genfill_cache/<node id>`. See [Pipeline setup](#4-pipeline-setup-environment-variables) |
| reference frame | Frame the selected variation was generated on |

### What happens on Generate

1. The selection is probed at low resolution to find its bounds (works for any matte).
2. A **context crop** is chosen like Photoshop does: the selection plus a band of surroundings,
   at an aspect ratio and resolution the model likes, so nothing gets stretched.
3. The crop is **encoded** for the model (next section) and written as an 8-bit PNG, together
   with the mask.
4. The model generates N variations. With ComfyUI, GenFill uploads the crop, queues one graph
   per variation and polls until they finish; the panel shows progress and Cancel.
5. Each result is read back raw, **decoded** with the exact inverse, placed back at the crop's
   position and blended through the expanded, feathered selection.

Everything is stored per generation (region, frame, encoding, files), so selecting an older
variation restores it exactly, even if you've changed the selection or color settings since.

---

## Models

| Model | Good at | Notes |
|---|---|---|
| **ComfyUI** (default) | Any workflow you can build in ComfyUI. Shipped: FLUX.1 Fill [dev] (default), plus Comfy API workflows (FLUX Pro Fill, Bria, Recraft) | Your ComfyUI server: this machine, the network or RunPod. Free with local models (**FLUX.1 Fill [dev] is non-commercial**); Comfy API workflows bill your Comfy.org account. Empty prompt = the workflow's erase partner. Results come back as 8-bit PNG |
| Stability AI | Masked inpainting; *Erase* for removal | Cloud, paid. 8-bit |
| OpenAI GPT Image | Prompt following | Cloud, paid. Re-renders the crop; GenFill composites it back through the mask. 8-bit |
| fal.ai | One key, many models: FLUX.1 Fill [pro] (default), Ideogram, GPT Image 2, Qwen, Bria, OneReward; reference edits | Cloud, paid. Empty prompt runs FLUX Erase. Mask inpainting at the crop's size. 8-bit |
| Magnific | Pixel-preserving *Retouch* (default Retouch Auto) plus reference-edit models | Cloud, Magnific credits, sign-in. Empty prompt = erase. 8-bit |
| Higgsfield | Reference edits (GPT Image 2.5 default, Nano Banana, Seedream, FLUX, Ideogram) | Cloud, Higgsfield credits, sign-in. Re-renders the crop; GenFill composites it back. Ignores the mask. Empty prompt = a "remove it" instruction. 8-bit |
| Comfy Cloud | Mask inpainting (Flux Pro Fill default, Bria GenFill, Recraft) and partner edit models | Cloud, Comfy credits, API key or sign-in. Empty prompt = Flux Erase / Bria Eraser. 8-bit |
| Offline Preview | Testing the pipeline | No AI; smears surrounding color into the selection |

### Cloud models and the version menu

The menu next to **Model** picks the model of the current cloud backend. *Default* (empty) uses
the backend's default; you can also type any model id the provider accepts.

- **Inpaint** models (and *erase* models for an empty prompt) change only the selection.
- **Edit** models see only the crop and an instruction. They re-render the whole crop and
  GenFill composites the result back through the expanded, feathered selection.

| Backend | Default | Inpaint / erase | Edit |
|---|---|---|---|
| fal.ai | `fal-ai/flux-pro/v1/fill` | `fal-ai/flux-pro/v1/fill`, `fal-ai/flux-pro/v1/erase`, `fal-ai/ideogram/v3/edit`, `openai/gpt-image-2/edit`, `fal-ai/qwen-image-edit/inpaint`, `fal-ai/flux-lora-fill`, `fal-ai/onereward`, `fal-ai/bria/genfill`, `fal-ai/finegrain-eraser/mask` | `fal-ai/nano-banana-pro/edit`, `blackforestlabs/flux-3/edit-image`, `bytedance/seedream/v5/pro/edit` |
| Magnific | `retouch-auto` | `retouch-auto`, `retouch-pro`, `retouch-classic`, `retouch-imagen-nano-banana-2-flash`, `retouch-imagen-nano-banana-2`, `retouch-inpaint-lab-nano-banana-2-flash`, `retouch-inpaint-lab-nano-banana-pro`, `retouch-inpaint-lab-seedream-5-pro`, `retouch-erase` | `seedream-5-pro`, `imagen-nano-banana-2-flash`, `imagen-nano-banana-2`, `imagen-nano-banana-2-lite`, `gpt-2`, `flux-kontext`, `flux-kontext-high`, `flux-2`, `seedream-4-5` |
| Higgsfield | `gpt_image_2_5` | none (the server rejects masks) | `gpt_image_2_5`, `gpt_image_2`, `nano_banana_2_1`, `nano_banana_2`, `nano_banana_2_lite`, `nano_banana_pro`, `nano_banana`, `ideogram_4_5`, `seedream_v5_pro`, `seedream_5_0_flash`, `seedream_v5_lite`, `seedream_v4_5`, `flux_kontext`, `flux_2`, `flux_3_image` |
| Comfy Cloud | `flux-pro-fill` | `flux-pro-fill`, `flux-erase`, `bria-genfill`, `bria-eraser`, `recraft-inpaint` | `flux-kontext-pro`, `flux-kontext-max`, `flux-2-pro`, `nano-banana-2`, `nano-banana-2-lite`, `nano-banana-pro`, `qwen-image-3` |

Hover an entry in the menu for its note (cost, strengths). Magnific was verified end to end
against the live service through GenFill's own code (browser sign-in, upload, Retouch erase,
download). The other new backends are covered by unit tests with mocked services.

### How ComfyUI runs

```
Nuke (GenFill plugin)                         ComfyUI server (local, LAN or RunPod)
  backends/comfyui.py                           POST /upload/image   crop + mask
  backends/comfy_workflows.py  ── HTTP ──────▶  POST /prompt         one patched graph per variation
  (finds nodes by title,                        GET  /history/<id>   poll until finished
   patches image, mask, prompt, seed)           GET  /view           download the PNGs
```

- GenFill loads the workflow's API-format graph, points `GenFill Image` and `GenFill Mask` at the
  uploaded files, sets the prompt and gives each variation its own seed (`seed + i`).
- Nothing heavy runs inside Nuke. A crash or out-of-memory error on the server becomes an error
  in the panel; Nuke and your script stay alive.
- Cancel (or a timeout) removes the queued jobs from the server and interrupts the running one.
- Variations are queued together, so on a busy server the panel shows your position in the
  ComfyUI queue.
- A variation that fails does not discard the ones that finished.

---

## Color science: a linear pipeline with display-referred models

### The problem

Every image generator available today (FLUX, Stable Diffusion, GPT Image, Firefly) is
**display-referred**. They were trained on hundreds of millions of ordinary photographs:
gamma-encoded, sRGB-ish images where values run from 0 (black) to 1 (display white). There is no
concept of scene-linear light, nothing above 1.0 and no color management. Inside, FLUX turns the
image into a latent with a VAE trained on those same images, and the output comes back in the
same 0..1 display range.

A Nuke comp is the opposite: **scene-linear** values (ACEScg, linear sRGB...) where 0.18 is mid
gray, a white shirt might be 0.9, a window 4.0 and a specular glint 50.0.

Feed scene-linear values straight into a model and two things go wrong:

1. **It looks wrong to the model.** Linear values viewed as display values look dark and
   contrasty: mid gray 0.18 reads as near-black. The model "fixes" what it sees, so the fill
   comes out too bright, too flat or the wrong color once you're back in linear.
2. **Highlights are destroyed.** Everything above 1.0 has to be clamped to 1.0 before the model
   sees it, and nothing the model generates can be brighter than 1.0. A sky at 3.0 comes back
   as a flat 1.0 plateau.

That's exactly what happens when you export a plate to Photoshop as an 8-bit sRGB / matte_paint
image: highlights clipped at 1, quantized to 256 levels.

### The principle: encode, generate, decode

You can't make the model work in linear, but you don't have to. Pick an **invertible transfer
function** *E* that maps the plate's whole range into 0..1 *and* makes it look like a normal
picture. Run the model in that space and map the result back with the exact inverse *E⁻¹*:

```
scene-linear plate ──E──▶ 0..1 "picture" ──▶ model ──▶ 0..1 "picture" ──E⁻¹──▶ scene-linear fill
   (0.18, 4.0, 50.0)       (what the model                (what the model         (0.18, 4.0, 50.0
                            understands)                   generates)               style values)
```

- **Unchanged pixels round-trip exactly.** If the model leaves a pixel alone, E⁻¹(E(x)) = x
  (to float precision), so the context around the fill matches the plate.
- **Generated pixels inherit the plate's range.** When the model paints a highlight at 0.95 in
  encoded space, E⁻¹ turns it back into a real scene-linear highlight, say 30.0, not a clipped
  1.0. The model never needs to know what linear light is. It paints a believable picture and
  the inverse transfer turns that picture into believable light.
- **Only the selection is replaced.** The comp merges the decoded fill over the *original*
  Source through the feathered selection. Outside the matte the plate is passed through
  untouched, so nothing outside the matte is affected by the encoding or the model.

In Nuke this is built from native nodes inside the Group, so it renders on the farm without
GenFill:

```
Source ─ Crop ─ Reformat ─ [to_model:   Expression (ACEScct) | OCIOColorSpace | none] ─ Write  (8-bit PNG, raw)
                                                                                         │
                                                                                       model
                                                                                         │
Read (raw) ─ [from_model: inverse Expression | inverse OCIOColorSpace | none] ─ Reformat ─ Transform ─▶ Merge over Source
```

The Write and Read are set to **raw**, so Nuke applies no extra color conversion of its own and
the encoding nodes are the only color transform in the chain.

### The encodings

Choose on the Settings tab under **color pipeline**.

#### Display transform (e.g. ACES `Output - Rec.709`): what the model sees best

The show's display transform, such as ACES RRT + Rec.709 ODT, is literally the function that
turns scene-linear light into the picture you look at. Run the plate through it and the model sees
the same image you see in the Viewer, which looks like the photographs it was trained on. The
transform's filmic tone curve rolls highlights off smoothly into the top of 0..1 instead of
clipping them, and Nuke's OCIO inverse expands them back.

This is the default for scene-linear plates. In practice it gives the most natural fills,
because the model is working on an image that looks the way it expects (this matches artists'
experience that fills made "in Output - Rec.709" integrate better than ones made in
matte_paint / sRGB).

Things to know:

- **The inverse is steep at the top.** The tone curve squeezes a huge range of highlight
  values into the last few percent of 0..1, so the inverse stretches that region back out.
  A tiny difference near display white becomes a large difference in linear. With the 8-bit
  PNGs that every backend returns, each step near white becomes a big jump in linear, which can
  show up as banding and posterized highlights after the inverse (see
  [Precision](#precision-why-8-bit-results-need-care)).
- **There's a ceiling.** Display white (1.0) decodes to the display transform's maximum
  scene value. Anything brighter in the plate is clamped to that maximum inside the fill. Pixels
  outside the selection are untouched.
- **Gamut.** Display transforms map to the display's gamut (Rec.709). Very saturated
  scene colors outside it come back approximated. Use Log when exact saturated color matters.
- It needs an output transform in your OCIO config. GenFill finds one automatically
  (`Output - Rec.709`, Rec.1886, sRGB display...) or uses the *display colorspace* you set. If the
  config has none, it falls back to Log.

#### Log (ACEScct): maximum headroom, exact maths

ACEScct is the Academy's log encoding for grading. GenFill implements it as plain Expression
nodes, with no OCIO config needed:

```
encode:  y = (log2(x) + 9.72) / 17.52           for x > 0.0078125
         y = 10.5402377416545 · x + 0.0729055341958355   otherwise (linear toe)

decode:  x = 2^(17.52 · y − 9.72)               for y > 0.155251141552511
         x = (y − 0.0729055341958355) / 10.5402377416545   otherwise
```

| Scene-linear | ACEScct value |
|---|---|
| −0.0069 | 0.0 |
| 0 | 0.073 |
| 0.18 (mid gray) | 0.414 |
| 1.0 | 0.555 |
| 4.0 | 0.669 |
| 16.0 | 0.783 |
| 100.0 | 0.934 |
| 222.86 | 1.0 |

0..1 in ACEScct covers scene-linear −0.0069 to **222.86**, about 10 stops above mid gray, so
practically every highlight survives. Every stop takes the same share of the range (1/17.52 ≈
0.057), which is ideal for precision. The inverse is exact to float precision, and because it is pure
maths it behaves the same in every config and on the farm.

The trade-off: a log image looks flat and washed out to the model, which is unlike its
training images, so fills can come out lower in contrast or a little less convincing than with
the display transform. Use it for plates with extreme highlights (sun, practicals, speculars),
when the display transform's ceiling or gamut gets in the way, or when your config has no
output transform.

#### Input colorspace: display-referred plates stay as they are

If the plate came from a display-referred file (a PNG/JPG/TIFF in matte_paint / sRGB, a still
from Photoshop) or from a camera log encoding (LogC, S-Log3, ACEScct), the best encoding is the
one it was stored in: the model sees the **original file values**, and the round trip is the
identity on those values. GenFill reads the colorspace off the Read node feeding the plate.

#### Custom

Any OCIO colorspace you type into *custom colorspace*.

### Auto (match input): the default

**Auto** walks up the Source input to the plate's Read node and decides:

| Plate | Example | Encoding |
|---|---|---|
| Display-referred file | PNG/JPG in matte_paint, sRGB, Rec.709 | Its own colorspace (*Input*) |
| Camera / grading log | LogC, S-Log3, ACEScct | Its own colorspace (*Input*) |
| Scene-linear | EXR in ACEScg / ACES2065-1 / linear | **Display transform**, or Log if the config has none |
| No Read found | Generated / CG upstream | Display transform |

The decision is logged with each generation, e.g.

```
color: plate Read Read1 (colorspace 'ACES - ACES2065-1', .exr) -> display: Output - Rec.709 (auto: ACES - ACES2065-1 is scene-linear)
```

### Precision: why 8-bit results need care

Encoding solves the *range* problem. Bit depth decides how *finely* that range is sampled, and
any quantization is magnified by the inverse transfer:

| Transport | ACEScct: linear change per code value | Result after decode |
|---|---|---|
| **8-bit PNG** (every backend, including ComfyUI) | ×1.049 (**4.9 %** per step) | Can show banding in skies and soft gradients |
| 16-bit | ×1.0002 (0.019 % per step) | Smooth |

The display transform's inverse is steeper still near white, so 8-bit is worse there.

GenFill 0.9 removed the float TIFF path of the old built-in local model: the crop goes to the
model and the result comes back as an 8-bit PNG, locally or in the cloud. The colour pipeline
(encode, generate, decode) is unchanged. To keep banding down:

- Prefer **Display transform** or a display-referred plate for bright areas, and check skies
  and soft gradients after decoding.
- Regrain or add a little noise over the fill: grain hides the 8-bit steps.
- Check bright fills against the plate (an Exposure node down a few stops is a quick test).

### What this does and doesn't give you

- ✅ Fills that sit in your working space with the plate's dynamic range: highlights above 1.0
  are generated as highlights, shadows keep their floor, and pixels outside the matte are
  untouched.
- ✅ Per-variation bookkeeping: each variation stores the encoding it was made with, so switching
  modes later never breaks earlier variations. Variations made before 0.6 decode the old way.
- ⚠️ Generated light is a plausible re-expansion, not measured light. A highlight the model
  paints at 0.95 in display space becomes whatever the inverse transform says that means. That's
  usually convincing, but check bright fills against the plate (an Exposure node down a few
  stops is a quick check).
- ⚠️ The model's view is clamped to the encoding's range (display transform ceiling, or ~222 in
  ACEScct), and display transforms limit gamut to the display's.
- ⚠️ Grain: generated areas are clean. Regrain the fill to match the plate (built-in grain
  matching is on the roadmap).

---

## Privacy and security

**ComfyUI runs where you point it.** GenFill sends the crop around your selection (not the whole
frame), its mask and the prompt to the ComfyUI server in the URL setting, over HTTP. With the
default `http://127.0.0.1:8188` and local model files nothing leaves your machine. A server on your
network or on RunPod receives them, so use HTTPS (the RunPod proxy URL is HTTPS) and a token when the
server is not on a trusted network. Workflows that use Comfy API nodes additionally send the
crop, mask and prompt through ComfyUI to the partner (Black Forest Labs, Bria, Recraft) and bill your
Comfy.org account (`COMFY_API_KEY`).

**Cloud models (Stability AI, OpenAI, fal.ai, Magnific, Higgsfield, Comfy Cloud) are
different:** the crop around your selection (not the whole frame), its mask and the prompt are
uploaded to that company. Use them only when your client / TPN rules allow it.

- Magnific, Comfy Cloud and Higgsfield keep generations in your account, so the uploaded crop and
  the results also appear in that provider's own library (and fal.ai stores uploads on its CDN).
  Delete them there if needed.
- Sign-in tokens never leave your machine except to their own provider. **Sign out** in the
  *API Keys & Accounts* dialog deletes the stored tokens.

| What | Where |
|---|---|
| Crops, masks, variations | `genfill_cache\` next to the saved script (`%TEMP%\genfill_cache` if unsaved), or where `GENFILL_CACHE` points. If scripts live in OneDrive/Dropbox this syncs; set **cache folder** or `GENFILL_CACHE` to keep it local |
| GenFill log (includes prompts) | `test_output\genfill.log` when installed from the repo, else `~\.nuke\genfill\genfill.log` |
| API keys, the ComfyUI URL and token, sign-in tokens | `~\.nuke\genfill\config.json`, never in .nk scripts. Sign out removes the tokens |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No Roto tools in the Viewer | Open the GenerativeFill panel (it must stay open while drawing) with *selection from* = Shapes, and view the node. Click **✏ Draw Selection** if needed |
| "Nothing is selected yet" | Draw a shape, or connect a matte / switch *selection from* |
| "The selection is empty on frame N" | The shapes are hidden or keyed off on this frame, or the matte is black here |
| "Cannot reach ComfyUI at ..." | ComfyUI is not running, or the URL is wrong. Start ComfyUI, then check **API Keys & Accounts > ComfyUI > Test connection**. On RunPod use `https://<pod-id>-8188.proxy.runpod.net` and wait until the pod's ComfyUI has finished starting. A 401/403 means the server wants a token (`GENFILL_COMFY_TOKEN`) |
| "The server has no file '...' for ... Loader" (model missing) | The workflow needs a model file ComfyUI doesn't have. Put it into the folder named in the message (`models/diffusion_models`, `models/text_encoders`, `models/vae`, `models/checkpoints`, ...) of the ComfyUI that runs the job, refresh ComfyUI, or pick another workflow. The per-workflow file list is in [Install](#2-comfyui-optional-recommended) |
| "Not usable: ..." in the model list, or "Unknown ComfyUI workflow" | The workflow file or its sidecar is broken, or its folder isn't listed. Check the node titles against the [title convention](#your-own-workflows) and `GENFILL_COMFY_WORKFLOWS` / `comfyui.workflow_dirs` |
| "uses Comfy API nodes and needs a Comfy account key" | Set `COMFY_API_KEY` (starts with `comfyui-`) or add it under **API Keys & Accounts** |
| "Waiting in the ComfyUI queue" for long | Another job is ahead of you on the server. Wait, or cancel and try again |
| Out of GPU memory | An error from the ComfyUI server. Close other GPU apps there (Nuke's own GPU cache counts on the same machine), or use a workflow with a smaller model or crop size (`pixels` in its sidecar) |
| Sign-in doesn't finish | Port 47321-47328 on `127.0.0.1` must be free (a firewall or other program may hold it). Finish in the browser within 5 minutes, then Generate (or Sign in) again |
| "Not signed in" | Use **Sign in** in *API Keys & Accounts*, or just click Generate and finish in the browser |
| Credits / payment error from a cloud provider | Top up or check the plan on the provider's site; or switch to a cheaper model in the version menu |
| Higgsfield ignores the mask / changes more than the selection | Higgsfield offers edit models only: they re-render the crop and GenFill blends back through the selection. Use Magnific, Comfy Cloud or fal.ai for true mask inpainting |
| Fill looks washed out | You're probably on *Log (ACEScct)*; try *Display transform* |
| Banding in bright fills | Results are 8-bit PNGs; see [Precision](#precision-why-8-bit-results-need-care). Regrain the fill |
| Bright highlights flatten inside the fill | Display transform ceiling; use *Log (ACEScct)* for that shot |

Send `test_output\genfill.log` with any bug report.

---

## Roadmap

- Track-and-apply: stick a still fill to a moving plate via a tracker / CornerPin.
- Grain matching on the generated patch.
- Reference image input, like Photoshop's "Reference image".
- A commercially licensed local workflow (e.g. Qwen-Image-Edit, Apache 2.0).
- Adobe Firefly Fill for studios with Firefly Services access.
- Anamorphic plates (pixel aspect is currently ignored when building the crop).

---

## Development

```
nuke/                       <- add this folder to NUKE_PATH (or use installers\install_dev.bat)
  init.py, menu.py          menus: Nodes toolbar > GenFill, and the GenFill top menu
  icons/GenFill.png
  python/genfill/
    node.py                 builds the Group, knobs, built-in Roto, upgrades of older nodes
    engine.py               generate pipeline, variations, threading, state
    colorspace.py           encodings (ACEScct maths, OCIO display/input), Auto decisions
    region.py               selection bounds and context-crop maths (pure Python)
    pngio.py, state.py      dependency-free PNG IO, variation history (pure Python)
    config.py, log.py       keys, cache and naming settings, logging
    naming.py               cache folder and file name patterns (pure Python)
    devtools.py             Reload GenFill, in-Nuke smoke test
    backends/               comfyui.py + comfy_workflows.py (ComfyUI server and its workflows),
                            stability.py, openai_images.py, fal.py, magnific.py, higgsfield.py,
                            comfy_cloud.py (+ mcp.py, oauth.py, cloud.py), mock.py
    ui/                     Photoshop-style panel, API keys & ComfyUI settings, Qt shim
  workflows/comfyui/        shipped ComfyUI workflows (<id>.json + <id>.genfill.json), README.md
installers/                 install / dev install / uninstall / smoke test (Windows), live_check.py
tests/                      stdlib unittest with a fake nuke module; runs without Nuke
docs/ARCHITECTURE.md        internals: node graph, generate flow, ComfyUI backend, color
```

```
python -m unittest discover -s tests -v
```

`tests/fake_nuke.py` stands in for the `nuke` module, so the node build, upgrades and the full
generate → select → delete flow are tested with the offline backend. Internals are documented in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Licenses

- **FLUX.1 Fill [dev]** (used by the shipped `flux_fill` workflows; you download it yourself):
  [FLUX.1 [dev] Non-Commercial License](https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev/blob/main/LICENSE.md).
  The model may only be used non-commercially (personal, research, evaluation); read the license
  for the terms on outputs before using them in paid work.
- T5 / CLIP text encoders: Apache 2.0 / MIT.
- ComfyUI itself: GPL-3.0; other workflows bring their own model licenses.
- Cloud backends: governed by your agreement with that provider.
