# GenFill research: model catalogues, cost quotes, balances (2026-10-15)

All data below came from free/read-only calls made on 2026-10-15. Nothing was generated or uploaded.
Items marked UNVERIFIED were not tested live.

## 1. Catalogues per provider

### 1a. Magnific (MCP https://mcp.magnific.com)

Two catalogue tools:
- `retouch_models_list(search?)` returns masked inpaint/erase models, each with `retouchModes` (replace/erase), `retouchTypes` (precise/creative), optional `resolutions` + `defaultResolution`, and `beta`/`private` flags.
- `images_models_list(search?, onlyRecommended?, includePreviews?)` returns images_generate models. Each has `supportsReferences`, `referenceTypes` (includes "image" for edit refs), `aspectRatios`, `resolutions`, `qualities`, `customSize{maxAspectRatio}`, `variants`, `agentRecommendation`, `requiresPremium`, `private`.
- `images_models_settings(mode)` is only needed for models flagged `extraSettings`. gpt-2-mini returns `extraSettings:{}`.
- There is no per-model "accepts mask" flag in images_models_list. Masks only exist on the retouch tool (`images_retouch` with `maskCreationIdentifier`).

Response shape of retouch_models_list (TOON-ish text, not JSON):
```
models[21]:
  - slug: retouch-inpaint-lab-gpt-2-mini
    name: GPT 2.5
    family: retouch
    tool: retouch
    beta: true
    expectedGenerationTime: 43
    retouchModes[2]: replace,erase
    retouchTypes[1]: creative
    resolutions[3]: 1k,2k,4k
    defaultResolution: 2k
    requiresPremium: true
    private: true
```
GenFill's client must tolerate this indented-list text format for retouch_models_list. images_models_list returns real JSON.

**GPT 2.5 exists.** Exact ids:
- images_generate: `gpt-2-mini` (name "GPT 2.5", beta). Aspect ratios auto, 1:1, 2:1, 3:1, 2:3, 3:2, 3:4, 4:3, 16:9, 9:16, 21:9. Resolutions 1k/2k/4k. Qualities low/medium/high/xhigh/max. Variants flare/sunburst. `transparentBackground`, `customSize.maxAspectRatio` 8, image refs yes.
- `gpt-2` is "GPT 2": same sizes, qualities low/medium/high, `agentRecommendation` sota (text layout).
- Retouch: `retouch-inpaint-lab-gpt-2-mini` (creative, replace+erase, 1k/2k/4k, default 2k) and `retouch-precise-gpt-2-mini` (precise, replace+erase). Both are flagged `private:true` (feature-flagged, "not generally available"), but the simulate_cost call accepted them.
- Retouch GPT 2: `retouch-inpaint-lab-gpt-2`, `retouch-precise-gpt-2` (also private).

**Ideogram:**
- images_generate: `ideogram` (v3-ish, refs style/character), `ideogram-4` (qualities turbo/mid/high, style refs), `ideogram-4-5` (beta, private, qualities very_low/low/medium/high, refs style/character/product/image), `p-image-ideogram`.
- Retouch: `retouch-ideogram-4-5` (creative) and `retouch-precise-ideogram-4-5` (precise). Both private/beta. `retouch-reference` is named "Ideogram Character" (replace only).
- images_generate description mentions `preciseEdit` (mode=ideogram-4-5; references[0] type image). This is an Ideogram 4.5 masked edit path inside images_generate. Not explored.

**Other newest models in images_generate:**
- Flux: `flux-3-image` (Flux.3, beta/private, resolutions 768p..4k, 16 aspect ratios), `flux-2` (Pro), `flux-2-max`, `flux-2-flex`, `flux-2-klein`, `flux-kontext`, `flux-kontext-high`.
- Nano Banana: `imagen-nano-banana-2-1` (2.1, private, featured), `imagen-nano-banana-2-flash` (= "Nano Banana 2"), `imagen-nano-banana-2` (= "Nano Banana Pro"), `imagen-nano-banana-2-lite`, `imagen-nano-banana`.
- Seedream: `seedream-5-pro` (1.5k/2k, customSize, sota default for image_edit), `seedream-5-lite` (2k/3k/4k), `seedream-5-flash` (1.5k/2k), `seedream-4-5` (2k/4k), `seedream-4`, `seedream-4-4k`.
- Qwen: `qwen`, `qwen-image-3-0`, `qwen-image-3-0-pro` (the last two beta/private).
- Others: `grok-imagine-2` (private), `krea-2` (private), `luma-uni-1`, `mai-image-2-5` (private), `magnific-one`, `recraft-v4-1` and others.

#### Catalogue diff, Magnific (GenFill `magnific.py` CATALOGUE)

| GenFill id | Label | Status in provider catalogue | Recommendation |
|---|---|---|---|
| retouch-auto | Retouch Auto | exists, replace+erase, 10 credits | keep |
| retouch-pro | Retouch Pro | exists, replace+erase, precise | keep |
| retouch-classic | Retouch Classic | exists, replace only | keep |
| retouch-imagen-nano-banana-2-flash | "Retouch Nano Banana 2" | exists (replace only, 1k/2k/4k, default 2k) | keep. GenFill pins resolution 1k; the API supports 2k/4k |
| retouch-imagen-nano-banana-2 | "Retouch Nano Banana Pro" | exists (replace only, 2k/4k) | GenFill pins resolution 2k, which is valid |
| retouch-inpaint-lab-nano-banana-2-flash | beta | exists | keep |
| retouch-inpaint-lab-nano-banana-pro | beta | exists (2k/4k) | keep |
| retouch-inpaint-lab-seedream-5-pro | beta | exists (1.5k/2k, default 2k) | keep |
| retouch-erase | erase only | exists, 30 credits | keep |
| (missing) | | `retouch-inpaint-lab-gpt-2-mini` "GPT 2.5" (private) | **add** (user asked for GPT 2.5). 180 credits at 2k |
| (missing) | | `retouch-precise-gpt-2-mini`, `retouch-inpaint-lab-gpt-2`, `retouch-precise-gpt-2` | add GPT 2 and 2.5 precise as optional |
| (missing) | | `retouch-inpaint-lab-nano-banana-2-1` and `retouch-precise-nano-banana-2-1` | add as optional (private) |
| (missing) | | `retouch-ideogram-4-5`, `retouch-precise-ideogram-4-5` | add as optional (private) |
| (missing) | | `retouch-precise-nano-banana-2-flash`, `retouch-precise-nano-banana-pro` | optional |
| (missing) | | `retouch-reference` (Ideogram Character), `retouch-lora` | skip (niche) |
| seedream-5-pro | edit | exists, sota rank 1, customSize | keep |
| imagen-nano-banana-2-flash | "Nano Banana 2" | exists | keep |
| imagen-nano-banana-2 | "Nano Banana Pro" | exists | keep (GenFill passes resolution 1k) |
| imagen-nano-banana-2-lite | exists | | keep |
| gpt-2 | "GPT 2" | exists, qualities low/medium/high, resolutions 1k/2k/4k | keep. GenFill sends resolution "1k" but not `quality` |
| (missing) | | `gpt-2-mini` "GPT 2.5" (beta, public in images_models_list) | **add** with `quality` passthrough (low..max) and `variant` flare/sunburst |
| flux-kontext / flux-kontext-high | exists | | keep |
| flux-2 | "Flux.2" | actual name "Flux.2 Pro", resolutions 1k/2k | rename label to "Flux.2 Pro"; add `flux-2-max`, `flux-2-flex` optional |
| seedream-4-5 | exists | | keep (optionally drop) |
| (missing) | | `flux-3-image`, `imagen-nano-banana-2-1`, `ideogram-4-5`, `qwen-image-3-0-pro`, `seedream-5-lite`, `seedream-5-flash` | all private or beta except seedream-5-lite/flash. Add seedream-5-lite and seedream-5-flash. Add the private ones only behind an "advanced" flag |

#### Dynamic listing, Magnific
- `retouch_models_list` is authoritative for inpaint models. Use `retouchModes` to decide whether a model can erase. Use `resolutions`/`defaultResolution` to fill the `resolution` arg. `private:true` models still work if the account has access (cost simulation accepted them), but treat them as "may fail". Filter or mark them.
- `images_models_list` is authoritative for edit models. Accept entries with `supportsReferences` and "image" in `referenceTypes`. Use `aspectRatios`, `customSize`, `resolutions`, `qualities`, and `agentRecommendation` for sorting.
- Neither tool needs arguments, so the cost to the client is two calls.

### 1b. Higgsfield (MCP https://mcp.higgsfield.ai/mcp)

`models_explore(action: list|search|get|recommend, type:"image", input:"image", limit<=100, query, model_id)` returns JSON:
`{items:[{id,name,provider_name,description,output_type,parameters:[{name,required,type,options,default,min,max}],medias:[{name,type,max,roles:[...]}],aspect_ratios:[...],tags,supports_unlim}], has_more, unlim:{available,remaining,expires_at}}`.

Mask support is visible in the `medias[].roles` list (it contains `"mask"`) and in the `is_inpaint` parameter:
- `nano_banana_2`: roles image_references, mask; `is_inpaint` bool; resolution 1k/2k/4k.
- `nano_banana_2_1`: roles image_references, mask, video_references; `is_inpaint`; `thinking_level`; `seed`; resolution 1k/2k/4k.
- `nano_banana_2_lite`: roles image_references, mask; `is_inpaint`; resolution 1k only.
- `ideogram_4_5`: roles image_references, mask; quality very_low/low/medium/high; `batch_size` 1-4; `seed`; resolution 1k/2k ("Masked edits use source geometry").
- `seedream_v5_pro` and `3d_jutsu_angles`: `is_inpaint` bool, no mask role.
- All other models take only references (no mask).

Ideogram: only `ideogram_4_5` (Ideogram 4.5). No v3.
GPT image: `gpt_image_2_5` (variant flare/sunburst, quality low/medium/high/xhigh/max, resolution 1k/2k/4k, background auto/opaque/transparent, role image_references), `gpt_image_2` (resolution + quality low/medium/high, role "image"), `openai_hazel` (quality low/medium/high; ratios 1:1, 3:2, 2:3, auto).
Other edit-capable: `nano_banana`, `nano_banana_pro`, `seedream_v4_5`, `seedream_v5_lite`, `seedream_5_0_flash`, `seedream_v5_pro`, `flux_2` (variant pro/flex/max), `flux_3_image` (resolution 768sq..4k, batch_size 1-4), `flux_kontext`, `kling_omni_image`, `grok_image`, `grok_image_2_0`, `flux_2_pro_outpaint`, `soul_*`.

#### Catalogue diff, Higgsfield (`higgsfield.py` _CATALOGUE)

| GenFill id | Status | Recommendation |
|---|---|---|
| gpt_image_2_5 | exists | keep. Real params: variant, quality (5 levels), resolution, background |
| gpt_image_2 | exists | keep (role "image") |
| nano_banana_2_1 | exists | keep. Has mask role + `is_inpaint` + `seed` + `thinking_level` |
| nano_banana_2 | exists | keep. Has mask role |
| nano_banana_2_lite | exists | keep. Has mask role. Only 1k |
| nano_banana_pro | exists | keep |
| nano_banana | exists | keep |
| ideogram_4_5 | exists | keep. Has mask role, `batch_size` up to 4 |
| seedream_v5_pro | exists | keep. Also supports `is_inpaint` |
| seedream_5_0_flash | exists | keep |
| seedream_v5_lite | exists | keep |
| seedream_v4_5 | exists | keep |
| flux_kontext, flux_2, flux_3_image | exist | keep |
| (missing) | `openai_hazel`, `grok_image_2_0`, `grok_image`, `kling_omni_image` | optional adds |
| (missing) | `flux_2_pro_outpaint`, `outpaint` | out of scope |

Consider using the mask role plus `is_inpaint:true` for nano_banana_2 / 2_1 / 2_lite / ideogram_4_5 / seedream_v5_pro. That would give true masked edits. I did not test whether the mask role changes behavior. UNVERIFIED.

#### Dynamic listing, Higgsfield
`models_explore(action:"list", type:"image", limit:100)` returned 54 image items in one page (`has_more:false`). Filter on `medias[].roles` containing `image_references`/`image`/`mask`. The `parameters[]` entries give exact option lists, defaults, and min/max, enough to generate the quality/resolution dropdowns. Items with an empty `provider_name` or description are internal (soul_*, topaz, upscalers). Exclude them by whitelist.

### 1c. Comfy Cloud MCP (https://cloud.comfy.org/mcp)

`search_models(source:"partner", limit:100)` returns the whole partner registry: `{data:[{model_name,display_name,type,directory:"partner",source:"partner",tags,tiers?,description}], metadata:{total:25,returned:25,catalog_total:2324}}`. `tiers` is present for some entries (grok, qwen, seedance, ltx, veo). `get_prompting_guide(model:"partner")` returns the full playbook as text.

Image partner slugs (17), exact:
`bfl/flux-pro-1.1-ultra`, `bfl/flux-kontext-pro`, `bfl/flux-kontext-max`, `bfl/flux-pro-expand`, `bfl/flux-pro-fill`, `bfl/flux-2-pro`, `bfl/flux-2-max`, `xai/grok-image-generate`, `xai/grok-image-edit`, `vertexai/nano-banana-2`, `vertexai/nano-banana-2-lite`, `vertexai/nano-banana-pro`, `openai/images-generations`, `ideogram/generate`, `ideogram/p-image`, `qwen/qwen-image-3`, `byteplus/images-generations`.

Edit/inpaint relevant:
- `bfl/flux-pro-fill`: `medias:[{role:"image"},{role:"mask"}]`, mask white = repaint. Async persisted.
- `openai/images-generations`: `params.model` in gpt-image-2.5-flare / gpt-image-2.5-sunburst / gpt-image-2 / gpt-image-1.5 / gpt-image-1. Edit via `medias[{role:"image"}]`. Adding `{role:"mask"}` goes synchronous direct (instant URL, not saved), and the model must be named explicitly then. Never pass the variant as the top-level `model`.
- `ideogram/generate`: edit via `medias[{role:"image"}]` (+ `mask`, `reference_image`, `character_image`, `character_mask`) routes to Ideogram V3 edit. There is no 4.5 slug in partner_generate. Ideogram 4.5 appears in the billing (`ideogram-4-5`), so a template or node may exist. UNVERIFIED.
- `xai/grok-image-edit`: `params.model` grok-imagine-image-2.0 (best for mask/region edits), `params.mask` supported (switches to synchronous). Up to 10 refs as `<IMAGE_0>`.
- `vertexai/nano-banana-2|-lite|-pro`: `image`/`images[]`; the multi-image Pro call is synchronous.
- `qwen/qwen-image-3`: tier in `params.model` (qwen-image-3.0-pro / qwen-image-3.0), 1-3 images (`@Image1`), `width`+`height` optional.
- `bfl/flux-kontext-pro|max`, `bfl/flux-2-pro|max`: roles `image` and/or `reference_image`.
- `byteplus/images-generations`: `params.model` e.g. `seedream-4-0-250828` (older ids; no 5.x documented).
- Local images: `upload_file`, then `medias[{role, name}]`. `value` must be a public https URL.

Comfy node-based (non-partner) fill nodes used by GenFill's workflow path (per `comfy_cloud.py` NODES): FluxProFillNode, FluxEraseNode, BriaGenFill, BriaEraser, RecraftImageInpaintingNode. Billing shows `flux-pro-1.0-fill` and `flux-tools/erase-v1` recently used.

#### Catalogue diff, Comfy Cloud (`comfy_cloud.py`)

| GenFill entry | Status | Recommendation |
|---|---|---|
| PARTNER_SLUGS flux-kontext-pro/max, flux-2-pro, nano-banana-2/-lite/-pro, qwen-image-3 | all valid slugs | keep |
| (missing) | `openai/images-generations` (gpt-image-2.5-flare/sunburst, gpt-image-2, with mask) | **add** (supports masks, GPT 2.5 via `params.model`) |
| (missing) | `xai/grok-image-edit` (`grok-imagine-image-2.0`, mask) | add |
| (missing) | `ideogram/generate` edit (V3 edit with mask) | add |
| (missing) | `bfl/flux-2-max` | add |
| (missing) | `bfl/flux-pro-fill` via partner_generate | alternative to NodeSpec flux-pro-fill (partner path persists to library; slower to wire) |
| `qwen-image-3` tier | `qwen-image-3.0-pro` default, `qwen-image-3.0` cheaper | expose tier |

#### Dynamic listing, Comfy Cloud
`search_models(source:"partner", limit:100, detail:"lean")` lists all slugs with `type`. It has no explicit mask/reference flag. Capabilities are only in the tags (`edit`, `inpaint`, `fill`, `image-edit`, `multi-reference`) and in the description text. So GenFill must keep a local capability map (slug to mask/ref/params) and use the live list only to hide slugs that were removed. Verify `metadata.returned == metadata.total`.

### 1d. fal.ai

Public model catalogue, no key needed (verified with curl, HTTP 200):
- `GET https://api.fal.ai/v1/models?category=image-to-image&limit=100` (max limit is 100; response `{models:[{endpoint_id, metadata:{display_name,category,description,status,tags,updated_at,model_url,license_type,group,...}}], ...}`; pagination via cursor, not examined). Add `&expand=openapi-3.0` with `endpoint_id=<id>` to get the full OpenAPI input schema per endpoint (verified). Search is `?q=`.
- `GET https://fal.ai/api/models?categories=image-to-image&limit=500` returned 404 items in one page, with `{items:[{id,title,category,tags,shortDescription,pricingInfoOverride (markdown price text),deprecated,removed,...}], page,size,pages,total}`. Undocumented site API, but it is the only keyless source of price text.
- Pricing API `GET https://api.fal.ai/v1/models/pricing?endpoint_id=...` returns 401 "API key authentication required". It needs the user's FAL_KEY (the key GenFill already has).

Whether a model takes a mask: inspect the OpenAPI schema for `mask_url` / `mask`. Verified examples:
- `ideogram/v4.5/edit`: image_url, **mask_url**, reference_image_urls, edit_precision regular/high, quality very_low/low/medium/high, num_images, seed, image_size.
- `openai/gpt-image-2.5/flare/edit` and `.../sunburst/edit`: image_urls, **mask_url**, quality auto/low/medium/high/xhigh/max, image_size, num_images, background, output_format.
- `openai/gpt-image-2/edit`: image_urls, mask_url, quality auto/low/medium/high.
- `alibaba/qwen-image-3/edit`: image_urls only (no mask), negative_prompt, seed.
- `google/nano-banana-2.1/edit`: image_urls, resolution 1K/2K/4K, seed, aspect_ratio, thinking_level, **no mask**.

Newest edit/inpaint endpoints present (not deprecated): `ideogram/v4.5/edit`, `ideogram/v4/image-to-image`, `fal-ai/ideogram/v3/edit`, `openai/gpt-image-2.5/flare/edit`, `openai/gpt-image-2.5/sunburst/edit`, `openai/gpt-image-2/edit`, `fal-ai/gpt-image-1.5/edit`, `google/nano-banana-2.1/edit`, `fal-ai/nano-banana-2/edit`, `fal-ai/nano-banana-pro/edit`, `google/nano-banana-lite/edit`, `blackforestlabs/flux-3/edit-image`, `fal-ai/flux-2-pro/edit`, `fal-ai/flux-2-max/edit`, `fal-ai/flux-2-flex/edit`, `fal-ai/flux-pro/v1/fill`, `fal-ai/flux-pro/v1/erase`, `fal-ai/flux-pro/kontext`, `bytedance/seedream/v5/pro/edit`, `bytedance/seedream/v5/lite/edit`, `bytedance/seedream/v5/flash/edit`, `alibaba/qwen-image-3/edit`, `fal-ai/qwen-image-max/edit`, `fal-ai/qwen-image-edit-2511`, `fal-ai/qwen-image-edit/inpaint`, `xai/grok-imagine-image/v2.0/edit`, `fal-ai/bria/genfill`, `bria/genfill/v2`, `fal-ai/bria/eraser`, `fal-ai/finegrain-eraser/mask`, `fal-ai/ideogram/object-removal`, `luma/agent/uni-1/v1/edit`, `microsoft/mai-image-2.5/edit`, `fal-ai/z-image/turbo/inpaint`.

#### Catalogue diff, fal (`fal.py` CATALOGUE)

| GenFill id | Status | Recommendation |
|---|---|---|
| fal-ai/flux-pro/v1/fill | exists | keep |
| (ERASE_MODEL; check id in fal.py) | `fal-ai/flux-pro/v1/erase` exists ($0.03/MP) | keep |
| fal-ai/ideogram/v3/edit | exists (v3) | keep, but **add `ideogram/v4.5/edit`** (mask_url, quality very_low..high, edit_precision). Newest Ideogram is 4.5 |
| openai/gpt-image-2/edit | exists, mask_url | keep; **add `openai/gpt-image-2.5/flare/edit` and `/sunburst/edit`** (qualities up to xhigh/max). Both take mask_url |
| fal-ai/qwen-image-edit/inpaint | exists | keep. Newer, no-mask: `alibaba/qwen-image-3/edit` |
| fal-ai/flux-lora-fill | exists | keep |
| fal-ai/onereward | not in image-to-image listing (not re-checked) | verify id, may be in another category |
| fal-ai/bria/genfill | exists; `bria/genfill/v2` also exists | keep; consider v2 |
| fal-ai/finegrain-eraser/mask | exists ($0.04 / $0.13 / $0.22 by mode) | keep |
| fal-ai/nano-banana-pro/edit | exists | keep; **add `google/nano-banana-2.1/edit`** and `fal-ai/nano-banana-2/edit` |
| blackforestlabs/flux-3/edit-image | exists | keep |
| bytedance/seedream/v5/pro/edit | exists | keep; optional `.../v5/lite/edit`, `.../v5/flash/edit` |
| (missing) | `xai/grok-imagine-image/v2.0/edit` | optional |

#### Dynamic listing, fal
- Keyless: list endpoint plus per-endpoint OpenAPI (`expand=openapi-3.0`). Detect mask support by the presence of `mask_url`, and image fields by `image_url` / `image_urls`. Detect quality/resolution enums from the schema.
- Prices: only with a key (pricing endpoint) or from `pricingInfoOverride` text on the website API.

### 1e. OpenAI and Stability (static lists)

- OpenAI models currently listed on the pricing page: gpt-image-2, gpt-image-2.5-sunburst, gpt-image-2.5-flare, gpt-image-1.5, gpt-image-1-mini, gpt-image-1. GenFill's list lacks `gpt-image-2.5-flare` and `gpt-image-2.5-sunburst` (add; `/v1/images/edits` accepts them per Comfy's guide, UNVERIFIED against the raw API). `GET https://api.openai.com/v1/models` works with a normal key but does not flag image-edit capability. Filter by prefix `gpt-image`.
- Stability: edit/inpaint and edit/erase unchanged. No model dropdown.

## 2. Cost before generating

### Magnific: `simulate_cost(tool, arguments)`
Pass the real tool name and the same args. `arguments:{}` returns the arg list. Response: `{"credits":<int>,"isUnlimited":false,"certainty":"exact"}`. The same call with a count returns the total for all images.

Real responses (credits; account has 243,317 available):
| Call | credits |
|---|---|
| images_retouch model=retouch-auto, replace | 10 |
| images_retouch retouch-pro, replace | 50 |
| images_retouch retouch-erase, erase | 30 |
| images_retouch retouch-inpaint-lab-nano-banana-2-flash, 1k | 75 |
| images_retouch retouch-inpaint-lab-seedream-5-pro, 2k | 100 |
| images_retouch retouch-ideogram-4-5 | 80 |
| images_retouch retouch-inpaint-lab-gpt-2-mini, 2k | 180 |
| images_generate gpt-2, 1024x1024, 1k, quality low, count 1 | 15 |
| images_generate gpt-2-mini, 1024x1024, 1k, medium, count 3 (count 1 not measured; the reference variant errored) | 300 |
| images_generate seedream-5-pro 1024x1024 1.5k count 1 | 75 |
| images_generate imagen-nano-banana-2-flash 1024x1024 1k count 1 | 75 |

Note: retouch has no count arg (GenFill loops N calls, so total = N x quote). A quote with `references:[{type:"image",identifier:"x"}]` errors "Creation not found" because the identifier must be a real uploaded creation. Quote without `references` (price is the same in the tests above) or after uploading. The credits-to-USD rate is not given by the API.

### Higgsfield: `generate_image(params:{model, prompt, get_cost:true, count, aspect_ratio, resolution, quality, ...})`
`get_cost:true` preflights without submitting a job. Response: `{"cost":{"credits":0.25,"credits_exact":0.25}}`.
Real responses (account has 143.97 credits):
| Call | credits |
|---|---|
| gpt_image_2_5, 1k, quality low, count 1 | 0.25 |
| gpt_image_2_5, 1k, quality medium, count 3 | 0.5 |
| ideogram_4_5, 1k, medium, count 1 | 2 |

The count-3 medium result (0.5) looks lower than expected; the quote might not scale per count or quality as expected. Validate before showing a total to the user. There is also `use_unlim` handling (a free-trial allowance); `get_cost` with `use_unlim` omitted works. No separate quote tool for generate_image. (`ads_studio_quote` exists for ads only.) Adding a medias entry with a real media id may change the price (input images); not tested.

### Comfy Cloud: `estimate_credits(workflow | template_name)`
Prices come from bundled node declarations; it prices graph nodes, not partner_generate slugs ("partner_generate cannot be priced by model id"). Per-unit nodes are listed but excluded from the total. Real response for a LoadImage + LoadImageMask + FluxProFillNode + SaveImage graph:
```
1 paid API node(s):
  FluxProFillNode (node 3): 11 credits

Estimated total: 11 credits
Estimate only — nothing was submitted and no credits were spent. ...
```
The text is not JSON. Parse with a regex like `Estimated total: ([\d.]+) credits`. For partner models without a graph there is no quote. Actual usage, for comparison, in `get_billing_activity`: `flux-pro-1.0-fill` credits_used=10.55, `flux-tools/erase-v1` credits_used=6.33.

### fal.ai
- Official: `GET https://api.fal.ai/v1/models/pricing?endpoint_id=<id>` needs `Authorization: Key <FAL_KEY>` (401 without). Response shape not verified (no key used). UNVERIFIED.
- Keyless fallback: `pricingInfoOverride` (markdown text) in `https://fal.ai/api/models?...` for some models. Examples: nano-banana-pro/edit $0.15 per image; nano-banana-2/edit $0.08; nano-banana/edit $0.039; ideogram/v4.5/edit $0.008 (very low) / $0.03 (low) / $0.06 (medium) / (high not shown, text truncated); ideogram/v3/edit $0.03/$0.06/$0.09 (TURBO/BALANCED/QUALITY); flux-pro/v1/erase $0.03 first MP then $0.004; finegrain-eraser/mask $0.04/$0.13/$0.22; qwen-image-3/edit $0.04 (1K), $0.075 (2K); flux-2-pro/edit $0.03 first MP + $0.015 per extra MP; xai grok-imagine v2.0 edit $0.04-$0.08; token-priced models (GPT image 2/2.5) are described per 1M tokens (text $5 in / $10 out; image $8 in), no flat price. fal-ai/flux-pro/v1/fill and bytedance seedream edits have no price text.

### OpenAI (direct API)
Pricing page (https://developers.openai.com/api/docs/pricing) lists only per-1M-token image prices; per-image figures are in a calculator in the image guide. Standard: image input / image output per 1M: gpt-image-2 $8 / $30; gpt-image-2.5-flare and -sunburst $8 / $30; gpt-image-1.5 $8 / $32; gpt-image-1-mini $2.50 / $8; gpt-image-1 $10 / $40. Batch is half. No quote endpoint. GenFill must estimate from tokens, or hardcode a small table.

### Stability
Stable Image Edit Inpaint and Erase are 5 credits per call (official price update, effective 2025-08-01); a credit is $0.01, so about $0.05 per call (third-party sources; not confirmed on the official page, which WebFetch could not read). No quote endpoint.

## 3. Credit balance

| Provider | Tool/endpoint | Args | Response shape (real) |
|---|---|---|---|
| Magnific | `account_balance` | none | `{"plan":{"tier":"magnific","productName":"Premium+","isPaidPlan":true,"isUnlimitedMode":false,"unlimitedAppliesHere":false},"credits":{"available":243317,"totalPlan":570000,"spent":326683,"hasExtraCredits":true,"scope":"personal"}}` |
| Higgsfield | `balance` | none | `{"credits":143.97,"subscription_plan_type":"creator"}`. Also `transactions(cursor,size)` (history) and `show_plans_and_credits` (opens a widget, not for data) |
| Comfy Cloud | No remaining-credits tool found. `get_usage_report(months, group_by:model\|endpoint\|product, granularity)` returns spend in USD (text summary + JSON with `summary.spend_micros`, `breakdown[]{group_key,cost_micros,share}`, `buckets[]`). `get_billing_activity(limit,page)` returns `{total,page,limit,total_pages,returned,events:[{event_type,event_id,created_at,params:{job_id,model,credits_used}}]}` (event types seen: `api_usage_completed`, `cloud_subscription_paid`) | | Not a balance. Sum `credits_used` or show spend only |
| fal.ai | Billing/credits API exists (`GET https://api.fal.ai/v1/account/billing`) but needs an ADMIN-scope key, not a normal API key. UNVERIFIED, not tested (no key) | | treat as unavailable |
| OpenAI | No balance endpoint for a normal API key (the usage/costs endpoints need an admin key). UNVERIFIED | | unavailable |
| Stability | `GET https://api.stability.ai/v1/user/balance` with `Authorization: Bearer <key>` returns `{"credits": <float>}` (from memory of the v1 API, not tested because it requires a key and the pricing page could not confirm) | | UNVERIFIED shape |

## 4. Recommended implementation notes
1. Magnific: call `retouch_models_list` + `images_models_list` at dropdown build time and cache. Keep the static CATALOGUE as fallback. Add `gpt-2-mini` (GPT 2.5) as an edit model with quality passthrough; add `retouch-inpaint-lab-gpt-2-mini` as an inpaint model.
2. Higgsfield: dynamic from `models_explore(list,type=image)`. Enable mask when roles contain `mask` (use `is_inpaint:true`).
3. Cost preview: Magnific `simulate_cost` (exact), Higgsfield `get_cost:true` (exact), Comfy `estimate_credits` (graph models only), fal pricing API with the user's key, OpenAI/Stability from static tables.
4. Balance: Magnific and Higgsfield only; Stability if the user supplies a key (unverified shape).
5. Fix labels: Magnific `flux-2` is "Flux.2 Pro"; `imagen-nano-banana-2` is "Google Nano Banana Pro".
