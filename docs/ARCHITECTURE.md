# GenFill architecture

## Design goals

1. **Behave like Photoshop's Generative Fill**: select, prompt (optional), Generate, browse
   variations, and the result lives as a masked layer.
2. **Stay colour managed.** The artist never leaves Nuke's working space. All conversion to
   what a model expects happens inside the node and is exactly inverted on the way back.
3. **Never break a render.** The node is a Group of native Nuke nodes. Generation needs the
   plugin; rendering does not.
4. **Backends are swappable.** Cloud providers and any ComfyUI server (local, network, RunPod) behind one interface.

## The node

`genfill.node.create()` builds a Group:

```
Source ─┬─ FrameHold ── Crop ── Reformat ── Write(source.png)            ┐
        │                                                               │ send side
Roto(gf_roto, alpha) ─┐
Mask ─────────────────┼─ Switch ── Dilate(expand) ── FrameHold ── Expression(rgb=m, a=1-m)
                           │                 ├─ Reformat(probe) ── Write(probe.png)
                           │                 └─ Crop ── Reformat ── Write(mask.png, mask_alpha.png)
                           └─ Blur(feather) ─────────────┐
Read(var_XX.png) ── Reformat(crop size) ── Transform(crop xy) ── Merge(copy, masked) ─┐
                                              └─ Copy(alpha) ── Premult ─────────────┤
Source ───────────────────────────────────────────────────────────────── Switch ── Output
```

- The selection Switch (`gf_selection`) picks 0 = the built-in Roto `gf_roto` (fed by the Source
  for its format, alpha only, *replace* on), 1 = the Mask input, 2 = the Source alpha. The Roto's
  `curves` knob is exposed on the Group as the Link_Knob `gf_shapes`, so its shape list and Viewer
  tools are available from the GenerativeFill panel; *Draw Selection* also opens the Roto's own
  panel as a fallback. Nuke only puts a Roto's Viewer tools up while that Roto's control
  panel is open, so on `showPanel` GenFill opens `gf_roto`'s panel floating (`forceFloat`) and hides
  that window via Qt (the tools stay); `hidePanel` closes it. Nodes from before 0.7 are upgraded in `ensure_current()`.
- The four Write nodes are **disabled** except during the few milliseconds GenFill renders
  them, so *Render All* and farm submissions never touch them.
- The output Switch is `gf_has_result ? gf_output : 2`: until something is generated the node
  is a pass-through.
- `gf_frame` drives both FrameHolds: the plate and selection are sampled on the frame the
  selected variation was generated from.

## One Generate click

1. **Probe** (main thread): render the selection at ~192 px wide and find its bounds in
   Python (`pngio` + `region.mask_bbox`). This works for any matte, not just Roto.
2. **Region**: grow the bounds by `context` (default 35% of the selection's larger side,
   at least 32 px, region at least 512 px), ask the backend which resolution it wants for that
   shape, then refit the region to that exact aspect and keep it inside the format.
3. **Crops**: set the Crop/Reformat knobs, render `source.png` (8-bit, project colourspace),
   `mask.png` (white = fill) and `mask_alpha.png` (alpha 0 = fill, OpenAI's convention).
4. **Generate** (background thread): the backend returns N PNGs and they're saved as
   `var_01.png…`. Nuke's ProgressTask provides Cancel.
5. **Apply** (main thread via `executeInMainThread`): append to the history JSON in
   `gf_state` and select the first variation. Selecting sets the Read path, Reformat box
   (crop size) and Transform (crop position) from that generation's stored region.

Each generation stores its own region, frame and model size, so selecting an old variation
restores its placement even if the selection has changed since.

## Files

```
<script dir>/genfill_cache/<node uid>/
    g001_20261012-201500/
        probe.png  source.png  mask.png  mask_alpha.png
        var_01.png var_02.png var_03.png
```

The uid is fixed at creation, so renaming the node doesn't orphan its cache. Paths in the
state are relative to the node's cache folder; the absolute folder is kept as a fallback for
scripts saved after generating.

The root, the generation folder and the variation file names come from patterns
(`naming.py`): the node's *cache folder* knob, else `GENFILL_CACHE`, else the default above;
`GENFILL_NAME` (default `g{gen:03d}_{date}-{time}`) and `GENFILL_VARIATION_NAME` (default
`var_{var:02d}`). Patterns take `$VAR` / `${VAR}` / `%VAR%` environment variables and `{token}`
values and are read on every Generate. The generation number is GenFill's: stored as `number`,
never reused (one above every earlier number), bumped while the folder exists, and appended
when a pattern leaves it out. A root pattern that names `{uid}` or `{node}` gets no extra
per-node folder. Invalid patterns stop Generate before anything is rendered or paid for.

## Backends

`backends.base.Backend` is pure Python (stdlib `urllib`) and knows nothing about Nuke, so every
backend is unit tested with mocked HTTP. Its interface:

- **Size:** `model_size(w, h, model, resolution)` returns the size to send for a crop of
  `w` x `h` plate pixels at a processing resolution (`auto`, `1K`, `2K`, `4K`).
  `size_plan(w, h, model, resolution)` wraps it in a `SizePlan` (width, height, requested and
  actual tier, clamp note, `native`) for the panel's "sends 2048 × 1152 (2K)" line, the log and
  the cost preview. `resolutions(model)`, `pixel_limits(model)` and `resolution_plan(...)` give
  a model's tiers and limits; `request_tier(request)` is the tier to ask the provider for in
  `generate` and `estimate`.
- **Generate:** `generate(request, api_key, progress, cancelled)` returns a list of PNG (or
  JPEG) bytes, one per variation. The engine takes width, height and tier from `size_plan`, so
  the quoted request and the sent request are the same.
- **Cost:** `estimate(request, api_key)` returns a `pricing.Quote` (or None) without submitting
  anything; `balance(api_key)` returns a `pricing.Balance` (or None). Both run on worker
  threads and never open a browser.
- **Models:** `catalogue` is the curated tuple of `ModelSpec`s (id, label, kind
  `inpaint` | `edit` | `erase`, erase partner, max count, note, `extra`). A `discoverable`
  backend implements `discover(api_key)`; `all_models()` returns the catalogue followed by the
  cached discovered models it doesn't list (curated entries always win), and `model_spec(id)`
  looks up both, with an ad-hoc spec for any other typed id. Shared `ModelSpec.extra` keys:
  `resolutions` (supported tiers; `()` = fixed size; missing = the backend's
  `default_resolutions`), `min_pixels` and `max_pixels`.

To add a backend, subclass, give it a `catalogue` and a `default_model`, implement
`model_size` and `generate` (plus `estimate` / `balance` / `discover` where the provider
supports them) and register it in `backends/__init__.py`.

### Processing resolution

`base.py` defines the tiers `1K`, `2K`, `4K` = 1024², 2048², 4096² pixels. `auto` keeps each
backend's own budget (about 1 MP, the sizes before 0.9). For a tier:

1. `clamp_resolution` picks the tier the model supports (nearest lower one, or its minimum),
   with a note for the panel ("model max 2K"); fixed-size models stay at `auto`.
2. `tier_budget` turns it into a pixel budget: never more pixels than the crop has in the
   plate (no upscaling), then clamped to the model's `max_pixels` / `min_pixels`.
   `model_size` lays that budget out in the crop's aspect with the model's ratio / multiple
   rules.
3. `sent_tier` is the tier the provider is asked for. A crop that is smaller than the tier is
   sent at its own size, so the provider gets the smallest supported tier that holds it
   (and bills no more pixels than the crop has). The threshold is `NATIVE_SHARE` = 0.8: a
   tier holds a crop when its pixel count is at least 0.8 x the crop's (provider tiers are
   nominal sizes, so a crop up to 1.25 x a tier still counts as that tier). Example: 4K
   requested, a 2500 x 1400 crop (3.5 MP) is sent at its own size and asked for as 2K.
   `SizePlan.native` ("crop is smaller than 4K") is set when both the crop and the sent size
   are below 0.8 x the tier's pixels.

### Cost preview and model discovery

- **Pricing** (`pricing.py`): on the main thread the panel takes a plain `snapshot` of the
  settings (backend, model, quality, resolution, count, the `size_plan` width and height,
  whether the prompt is empty, and the auth state: key set / signed in). `request(snapshot)`
  starts worker threads that call `Backend.estimate` and `Backend.balance`; `lookup` reads the
  cache. Quotes are cached for 5 minutes per settings key, balances for 60 seconds per backend
  and auth state, failures for 30 seconds; a generation drops the balance. Sources: provider
  preflights (Magnific `simulate_cost`, Higgsfield `get_cost`, Comfy `estimate_credits`, fal's
  pricing API) and static tables (OpenAI per-token prices, fal fallbacks, Stability). Nothing
  here touches Qt or Nuke.
- **Discovery** (`backends/discovery.py`): Magnific, Higgsfield and fal.ai implement
  `discover`. The panel starts a fetch on a worker thread once a day (or with *Refresh model
  list…*); results are stored atomically, mode 0600, in `~/.nuke/genfill/models_cache.json`
  (`{"<backend id>": {"fetched": ..., "models": [...]}}`, model metadata only) and are fresh for
  24 hours. A failed or empty fetch keeps the cache and is retried after an hour. `merge` puts
  discovered specs (marked `discovered`, shown with "(new)") after the curated ones and relabels
  label collisions. Discovery never runs on Generate and never opens a browser.

## Cloud backends over MCP

Magnific, Higgsfield and Comfy Cloud are reached through their remote MCP servers. GenFill is
its own MCP client (stdlib only, so it runs in Nuke's Python on every OS); fal.ai uses its plain
REST queue API instead.

```
engine.generate ──▶ Backend.generate(request, api_key, progress, cancelled)
                      │  McpBackend (cloud.py): server_url, client(), provider()
                      ▼
                    McpClient (mcp.py) ── JSON-RPC over HTTPS ──▶ MCP server
                      │  Authorization: Bearer / X-API-Key         initialize, tools/call
                      ▼
                    OAuthProvider (oauth.py) ── browser + 127.0.0.1:47321-47328/callback
                      │                          tokens: config.json  oauth.<id>
                      └─ 401 ─▶ refresh_token ─▶ (still 401) interactive sign-in
```

- **MCP client** (`backends/mcp.py`): Streamable HTTP. `initialize`, `notifications/initialized`,
  `tools/call`. Replies may be JSON or an SSE stream (the response with the matching `id` is
  picked). It keeps `Mcp-Session-Id` and re-initialises if the session expires. `call_tool`
  returns `structuredContent`, else the parsed JSON text, and turns `isError` into a
  `BackendError`. A 401 triggers one token refresh, then (if allowed) a browser sign-in.
- **OAuth 2.1** (`backends/oauth.py`): discovery from the server's 401 (`WWW-Authenticate`
  resource metadata, RFC 9728) and the authorization server's metadata (RFC 8414); dynamic client
  registration as a public client; authorization code with PKCE (S256), `state` and `resource`.
  A one-shot `http.server` on `127.0.0.1:47321-47328/callback` receives the code (first free
  port; waiting is cancellable and times out after 5 minutes). Tokens, expiry and the
  registration are stored under `oauth.<id>` in `~/.nuke/genfill/config.json`; refresh is silent.
  Sign out deletes them. If a key is set and the backend has an `api_key_header` (Comfy's
  `X-API-Key`), the key is used instead of OAuth.
- **`McpBackend` / `cloud.py`**: shared plumbing for these backends: the client factory, one
  `OAuthProvider` per backend, `ref_prompt` (the instruction for edit models, which never see
  the mask), `put_bytes` (presigned PUT), `poll` (cancellable). Sign-in happens inside the
  generation thread, so Nuke's UI never blocks; the panel shows "Sign in to <provider> in your browser...".
- **Results** are named `.png` or `.jpg` by their content. Models answer at their own size;
  the node's Reformat resamples the result to the crop.

**Magnific** (`magnific.py`): `creations_request_upload` -> PUT -> `creations_finalize_upload`
for the plate and mask, once. Retouch models call `images_retouch` (one call per variation);
edit models call `images_generate` with the crop as reference. `creations_wait` long-polls the
creations; results are downloaded. Empty prompt = `images_retouch` in erase mode.

**Higgsfield** (`higgsfield.py`): `media_upload` -> PUT -> `media_confirm`, then
`generate_image_batch` and `jobs_wait` until all jobs are terminal. The server rejects a mask
role, so every model is a reference edit with `ref_prompt`.

**Comfy Cloud** (`comfy_cloud.py`): `upload_file` returns a single-use PUT URL. Mask models run
as one API-format graph via `submit_workflow`: `LoadImage` and `LoadImageMask` feeding N fill
nodes (`FluxProFillNode`, `BriaGenFill`, `RecraftImageInpaintingNode`, or `FluxEraseNode` /
`BriaEraser` for an empty prompt) into one `SaveImage` each; then `wait_for_job` and
`get_output`. Edit models (Kontext, Nano Banana, Qwen, Flux 2) use `partner_generate` per
variation. Cancel calls `cancel_job`.

**fal.ai** (`fal.py`, REST queue): upload the crop and mask to the fal CDN, `POST` one request
per variation (or one with `num_images`) to `queue.fal.run/<model>`, poll the returned
`status_url`, read the `response_url`, download the images. `Authorization: Key <FAL_KEY>`.

## ComfyUI (local, network or cloud GPU)

```
Nuke (GenFill plugin)                          ComfyUI server (this machine, LAN, RunPod)
  backends/comfyui.py       --- HTTP(S) --->     POST /upload/image   plate crop + mask
  backends/comfy_workflows.py                    POST /prompt         one patched graph per variation
                                                 GET  /history/<id>   poll
                                                 GET  /view           download PNGs
                                                 GET  /queue, POST /queue, POST /interrupt (cancel)
```

- **Why a server:** PyTorch and the model weights never load into Nuke. A crash or GPU
  out-of-memory becomes an error in the panel; Nuke and the script stay alive, and the model
  can live on another machine. This replaced the built-in local helper process of 0.8.
- **Workflows** (`comfy_workflows.py`): API-format graphs (`<id>.json`) with an optional
  `<id>.genfill.json` sidecar (label, `kind` inpaint/erase, `empty_prompt` erase partner, crop
  `pixels`/`multiple`/`min_side`/`max_side`, `api_nodes`, `timeout`, `requires`). GenFill finds
  its inputs by node title (`GenFill Image`, `GenFill Mask`, `GenFill Prompt`, `GenFill
  Negative`, `GenFill Seed`, `GenFill Output`; the table is in the module docstring and in
  `nuke/workflows/comfyui/README.md`). `validate()` rejects UI-format files and graphs without
  image and mask nodes; such a workflow is listed as "Not usable: ..." instead of crashing.
- **Discovery:** `config.comfyui_workflow_dirs()` = the bundled `nuke/workflows/comfyui`, then
  `comfyui.workflow_dirs` from the config file, then `GENFILL_COMFY_WORKFLOWS` (os.pathsep list),
  read on every call. `library()` merges them in that order, so a later folder overrides an
  earlier workflow id, and re-reads only when a file changed. Each workflow is one entry of the model
  (version) menu; the model id is the workflow id (default `flux_fill`).
- **Generate** (`ComfyUIBackend.generate`): an empty prompt switches to the workflow's
  `empty_prompt` partner; upload the crop and the mask (`LoadImage` workflows get the alpha mask
  file, `LoadImageMask` the red/white one); queue one deep-copied, patched graph per variation
  with seed `base + i`; poll `/history/<id>` (a few failed polls are tolerated); download the
  images of the output nodes. Queue position is shown while waiting. Cancel or timeout deletes
  queued jobs and interrupts the running one. A variation that fails keeps the finished ones.
- **Auth:** `Authorization: Bearer <token>` (`GENFILL_COMFY_TOKEN`) for protected servers.
  Workflows with `api_nodes` (Comfy API / partner nodes) also need the Comfy.org account key
  (`COMFY_API_KEY`, shared with Comfy Cloud), sent as `extra_data.api_key_comfy_org`.
- **Errors:** `POST /prompt` 400 answers are turned into readable messages; a missing model file
  names the file and the `models/` subfolder to put it in. An unreachable server reports
  "Cannot reach ComfyUI at <url>".
- **Settings:** the ComfyUI group of the API Keys dialog (`ui/settings.py`) edits URL and token
  and has a connection test; env vars win over the stored values.
- **Upgrade:** `node.py` maps a stored "Local..." backend label to ComfyUI when a node loads.
  The crop is sent as an 8-bit PNG and results come back as 8-bit PNG.

## Colour

`colorspace.py` decides, per generation, how the plate is encoded for the model and stores the
decision (`transfer`) with the generation:

```
Source -> Crop -> Reformat -> [gf_to_model: Expression(ACEScct) | OCIOColorSpace | none]
       -> Write 8-bit PNG, raw
model works in 0..1 (8-bit in, 8-bit out)
Read (raw) -> [gf_from_model: inverse Expression | inverse OCIOColorSpace | none] -> Reformat -> Transform
```

- *Auto*: the Read feeding the plate decides. Display-referred colorspaces (matte_paint, sRGB,
  Rec.709, log encodings such as LogC/ACEScct) are used as-is: the model sees the original file
  values. Scene-linear (ACEScg, linear, EXR) goes through the display transform (e.g. ACES
  `Output - Rec.709`, picked from the OCIO config), whose tone curve compresses highlights into
  0..1; falls back to ACEScct when the config has no output transform.
- The ACEScct curve is implemented as Expression nodes (and in Python for previews/tests), so it
  needs no OCIO config. 0..1 in ACEScct covers scene-linear -0.007..222.9.
- Every backend, ComfyUI included, takes and returns 8-bit PNG (the float TIFF path of the
  removed local helper is gone). The inverse transfer is steep near white, so check bright
  gradients for banding.
- Generations made before 0.6 have no `transfer`; they're read the old way (8-bit file in the
  default colourspace, no decode).

## Known limitations (v0.1)

- Anamorphic plates: pixel aspect is ignored when building the crop.
- Proxy mode is turned off temporarily while rendering the crops.
- The panel is Qt (PySide2/6). If it fails to load, the standard knobs still work and the
  result still renders; generation then needs the Python call
  `genfill.engine.generate(nuke.selectedNode())`.
- One generation at a time per node.
