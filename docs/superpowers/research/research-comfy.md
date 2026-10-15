# Comfy Cloud remote MCP server: research notes for GenFill (Nuke, Python stdlib MCP client)

Probed on 2026-10-15 against production (`get_server_info`: server `comfyui-cloud` v0.71.2, 41 tools,
host `https://cloud.comfy.org`, partner proxy `https://api.comfy.org`).
Everything marked **[verified]** was observed live; **[inferred]** means docs or spec, not observed.

---

## 0. TL;DR for the implementer

- Endpoint: `POST https://cloud.comfy.org/mcp` (MCP Streamable HTTP). Auth: `X-API-Key: comfyui-...` **or**
  `Authorization: Bearer <oauth access token>`.
- Best route for GenFill: **`bfl/flux-pro-fill`** (image + mask, white = fill). With an empty prompt,
  use **`FluxEraseNode`** (object removal). Both were tested end to end and returned PNGs at the input
  size (512x512 in, 512x512 out).
- Flow per request:
  1. Call `upload_file` twice (plate, mask). Each call returns a one-time PUT URL. PUT the bytes with urllib.
     The response gives `{"name": ...}`.
  2. Submit the work. Either call `partner_generate` (model `bfl/flux-pro-fill`, `medias` with roles
     image + mask, `confirm: true`), or call `submit_workflow` with a hand-built API graph (recommended
     for N variations: one graph with N fill nodes and N `SaveImage` nodes = one job).
  3. Call `wait_for_job`, then `get_output`. Each result carries a `url` (`https://cloud.comfy.org/api/s/<id>?raw=1`):
     no auth, it 302-redirects to a signed GCS URL. urllib follows it. Expect `image/png`.
- Cost (live billing feed): Flux Pro Fill **10.55 credits** per image; Flux Erase **6.33 credits** per image.

---

## 1. Transport and auth

### 1.1 Unauthenticated probe [verified]

```
POST https://cloud.comfy.org/mcp   (initialize, no auth)
HTTP/2 401
content-type: application/json
access-control-expose-headers: Mcp-Session-Id, WWW-Authenticate
www-authenticate: Bearer realm="comfy-cloud-mcp", resource_metadata="https://cloud.comfy.org/mcp/.well-known/oauth-protected-resource", scope="comfy-mcp:tools:call"

{"jsonrpc":"2.0","error":{"code":-32001,"message":"Authentication required. Provide an X-API-Key header or Authorization: Bearer token."},"id":null}
```
- A `GET /mcp` (SSE stream request) without auth also returns 401.
- An invalid bearer gets 401, `error="invalid_token"` in WWW-Authenticate, body message `"Invalid or expired token."`.
- An invalid `X-API-Key: comfyui-bogus` gets 401 with the same header and body message `"Invalid or expired API key."`.
  The client can tell a bad key from a missing one by the message text. JSON-RPC error code `-32001` in both cases.

### 1.2 SSE vs JSON, Mcp-Session-Id [inferred, not verifiable without a credential]
I had no API key in this environment (only Claude's own OAuth connector session), so I could not watch a raw
authenticated exchange. The server exposes `Mcp-Session-Id` via CORS, which means it follows the Streamable HTTP
spec with sessions. The client must handle both of the following:
- Send `Accept: application/json, text/event-stream` and `Content-Type: application/json` on every POST.
- `initialize`: read the `Mcp-Session-Id` response header if present and echo it on every later request.
  Send `MCP-Protocol-Version: <negotiated>` (e.g. `2025-06-18`) after init.
- Then send the `notifications/initialized` notification (expect 202, empty body).
- Response handling: if `Content-Type: application/json`, parse the body. If `text/event-stream`, read
  `data:` lines until the event whose JSON has your request `id` (ignore `notifications/progress` etc.),
  then close. A ~40-line stdlib SSE reader is enough.
- Long calls (`wait_for_job` up to `max_wait_seconds`, `partner_generate` with `wait_for_output`, ~25 s) hold
  the HTTP request open, so use a socket timeout of at least `max_wait + 30` s.
- On 404 with a session id, the session expired. Re-run `initialize`.
- Tool results: `result.content[0].text` holds the payload. For most tools it is a JSON string (see the raw
  samples below). `upload_file` returns **plain prose text** in this session. Parse it defensively (regex, §2.1).
  Also check `result.structuredContent` first if present (partner_generate docs mention
  `structuredContent.results[]`), and check `result.isError`.

### 1.3 API key [docs]
- Header `X-API-Key: comfyui-…`. Keys are created at https://platform.comfy.org/profile/api-keys, are
  workspace-scoped and long-lived, and have no refresh. The docs also say `Authorization: Bearer <api key>` is accepted
  (OpenClaw recommends it because it "survives proxies better").
- Generation needs an active Comfy Cloud subscription. Discovery tools work on a free account.
- Simplest option for GenFill v1: an API-key field in the plugin prefs, stored outside the .nk script.

### 1.4 OAuth (all verified)
Protected-resource metadata (RFC 9728), at the URL named in the challenge:
```
GET https://cloud.comfy.org/mcp/.well-known/oauth-protected-resource
{"resource":"https://cloud.comfy.org/mcp","authorization_servers":["https://cloud.comfy.org"],
 "scopes_supported":["comfy-mcp:tools:read","comfy-mcp:tools:call"],"bearer_methods_supported":["header"]}
```
(The root `/.well-known/oauth-protected-resource` describes `resource: https://cloud.comfy.org/api` with the
`comfy-cloud:*` scopes. That is not the one to use for MCP.)

Authorization-server metadata: `GET https://cloud.comfy.org/.well-known/oauth-authorization-server`
(no OIDC discovery; `/.well-known/openid-configuration` returns 404):
```
issuer                  https://cloud.comfy.org
authorization_endpoint  https://cloud.comfy.org/oauth/authorize
token_endpoint          https://cloud.comfy.org/oauth/token
registration_endpoint   https://cloud.comfy.org/oauth/register        (DCR, open, no initial access token)
jwks_uri                https://cloud.comfy.org/.well-known/jwks.json
response_types          ["code"]
grant_types             ["authorization_code","refresh_token"]        (no device-code grant)
code_challenge_methods  ["S256"]                                      (PKCE S256 required)
token_endpoint_auth     ["none"]                                      (public clients only)
scopes                  comfy-mcp:tools:read comfy-mcp:tools:call (+ comfy-cloud:*, comfy-router:*)
```

DCR test (harmless, public client):
```
POST https://cloud.comfy.org/oauth/register
{"client_name":"GenFill probe (research)","redirect_uris":["http://127.0.0.1:53682/callback"],
 "grant_types":["authorization_code","refresh_token"],"response_types":["code"],
 "token_endpoint_auth_method":"none","scope":"comfy-mcp:tools:read comfy-mcp:tools:call"}
-> HTTP 201
{"application_type":"native","client_id":"<redacted>",
 "client_id_issued_at":1792023126,"client_name":"GenFill probe (research)",
 "grant_types":["authorization_code","refresh_token"],"redirect_uris":["http://127.0.0.1:53682/callback"],
 "response_types":["code"],"token_endpoint_auth_method":"none"}
```
(The `scope` field is not echoed back. `http://localhost/callback` is also accepted.)

Authorize-request checks with that client:
| redirect_uri sent | result |
|---|---|
| `http://127.0.0.1:53682/callback` (as registered) | **302** to `https://cloud.comfy.org/cloud/login?oauth_request_id=…` (browser login) |
| `http://127.0.0.1:49999/callback` (other port) | **400** `{"code":"invalid_request","message":"invalid authorization request parameters"}` |
| `http://127.0.0.1:53682/other` | 400 |
| `https://evil.example/cb` | 400 |

The loopback redirect works, but the server matches **exactly, including the port** (no RFC 8252 any-port
loopback rule). The client has two options:
- (a) Bind a fixed port, register once, and persist the `client_id`.
- (b) Pick a free port, then DCR a new client with that exact redirect URI before each login. Registration is cheap and open.
  Option (b) is the most robust when the port might be taken.

Token endpoint: `POST https://cloud.comfy.org/oauth/token`, `application/x-www-form-urlencoded` (verified that it
parses form bodies: a bad code returned `{"error":"invalid_grant","error_description":"authorization code not found"}`).
Fields: `grant_type=authorization_code&code=…&client_id=…&redirect_uri=…&code_verifier=…`. Refresh uses
`grant_type=refresh_token&refresh_token=…&client_id=…`.

Full flow (standard): generate the PKCE verifier and S256 challenge, plus a `state`. Open the browser to
`/oauth/authorize?response_type=code&client_id=…&redirect_uri=…&scope=comfy-mcp:tools:read%20comfy-mcp:tools:call&state=…&code_challenge=…&code_challenge_method=S256&resource=https%3A%2F%2Fcloud.comfy.org%2Fmcp`.
Run a stdlib `http.server` on 127.0.0.1:port to catch `?code=&state=`, then exchange the code. Send
`Authorization: Bearer <access_token>` and refresh on 401 `invalid_token`.
Not verified: whether `resource` is required (it was accepted), the token lifetime, and whether refresh tokens rotate.
Docs mention refresh-token-reuse problems in Codex, so **assume rotation**: always persist the newest refresh token and
serialize refreshes.

---

## 2. Tools needed by GenFill (schemas loaded from the live server)

### 2.1 `upload_file` [verified]
Args: `{"file_path": "<absolute path>", "client_os": "darwin"|"linux"|"windows"}`.
Allowed extensions: .jpg/.jpeg/.png/.webp/.gif (+ audio/video). Max 50 MiB (413 consumes the URL).

In a REMOTE session the server **does not read the file**. It only templates the path into a shell command, so
the path does not even need to exist for the client. The entire returned text (darwin) looks like this:
```
This client cannot run shell commands, so hand this command to the user to run in a Bash terminal ... 

curl -sS --fail-with-body -X PUT -H 'Content-Type: image/png' --upload-file '/…/gf_test_plate.png' -- 'https://cloud.comfy.org/api/uploads/aFMt-H5pWwP-pVf80gk95g'

On success it prints a JSON body like {"name":"...","subfolder":"","type":"input"} ... use "<subfolder>/<name>"
if subfolder is non-empty ... The URL is single-use with a short TTL: if the command fails (404 = used/expired),
re-invoke upload_file ... at most 50 MiB ...
```
Client implementation:
```python
m = re.search(r"https://[^\s'\"]+/api/uploads/[A-Za-z0-9_-]+", text)
ct = re.search(r"Content-Type: ([\w/.+-]+)", text).group(1)   # image/png, derived from extension
req = urllib.request.Request(m.group(0), data=png_bytes, method="PUT", headers={"Content-Type": ct})
info = json.load(urllib.request.urlopen(req, timeout=120))
# -> {"name":"cc2cc02c…6b4.png","subfolder":"","type":"input"}
```
Verified: the PUT via `urllib` (default UA) returned 200 and
`{"name":"c3c9154cae6c3380f33bc4b38c4655fa31c6d415209f539a2a650d9ede860a0f.png","subfolder":"","type":"input"}`.
No auth header is needed or wanted. Re-PUT to a used URL returns
`404 {"code":"NOT_FOUND","message":"Unknown, expired, or already-used upload URL; mint a new one"}`.
The returned `name` is a server-side hash, **not** the sha256 of the file bytes, so you cannot precompute it or skip
re-uploads by hash. Reuse the names within a session instead (they persist in the user's input storage).
The ".png" extension in `file_path` drives the Content-Type, so pass a path ending in `.png`.

### 2.2 `partner_generate`
Args (main): `type:"image"`, `model:<slug>`, `client_os`, `prompt`, `seed`, `medias[]`, `params{}`,
`confirm:bool`, `wait_for_output:bool` (holds ~25 s and returns outputs inline), `idempotency_key`
(to collect an already-paid failed generation).
`medias[]` items are exactly one of `{role, name[, subfolder, type:"input"]}` (an uploaded file),
`{role, prompt_id, output_index}` (a previous Comfy output), or `{role, value:"https://public-url"}`.
Roles: `image`, `mask`, `reference_image`, `character_image`, `character_mask` (subset per model).

**Spend gate [verified]**: without `confirm` you get (as `content[0].text` JSON):
```json
{"result":{"status":"tool_error","warnings":[],"message":"Generating with paid partner model \"bfl/flux-pro-fill\" will spend credits ... was NOT performed ...","error_type":"spend.unconfirmed","error_owner":"user","error_signal":"spend_gate.unconfirmed"}}
```
GenFill should either always send `confirm: true` (the user pressing "Generate" counts as consent), or show its own
confirmation when `error_type == "spend.unconfirmed"`. `submit_workflow`, `run_template` and `submit_batch` take the
same `confirm` flag.

**Live test 1 [verified]**: `bfl/flux-pro-fill`, 512x512 RGB plate plus 512x512 8-bit grayscale mask:
```json
{"type":"image","model":"bfl/flux-pro-fill","client_os":"darwin","prompt":"green grass meadow, seamless continuation of the surrounding lawn",
 "medias":[{"role":"image","name":"cc2cc02c…6b4.png","subfolder":"","type":"input"},
           {"role":"mask","name":"c3c9154c…a0f.png","subfolder":"","type":"input"}],
 "seed":1234,"confirm":true,"wait_for_output":true}
```
Response (trimmed; completed within the 25 s window):
```json
{"results":[{"source_node_id":"4","class_type":"SaveImage",
   "filename":"779b7364…e29.png",
   "url":"https://cloud.comfy.org/api/s/gEzEOnKFTSjyfvrHl2mm_Q?raw=1",
   "suggested_save_path":"~/Downloads/ComfyUI/comfy_output.png","saved_to_disk":false,
   "download_command":"curl -L --fail-with-body ... -- 'https://cloud.comfy.org/api/s/gEzEOnKFTSjyfvrHl2mm_Q?raw=1' ...",
   "inline_url":"https://storage.googleapis.com/comfy-cloud-assets/779b…e29.png?X-Goog-...&X-Goog-Expires=21599&...",
   "width":512,"height":512}],
 "prompt_id":"a8d98669-1b7b-4e09-a8d8-9ea39c74407a","status":"completed",
 "result":{"status":"succeeded_with_warnings","warnings":[
   {"code":"paid_api_nodes","detail":"... Node #3: FluxProFillNode ..."},
   {"code":"input_validation","detail":"Node #2 (LoadImageMask): \"image\" value ... was not found in the bundled node index"}],
  "job_status":"completed"}}
```
The `input_validation` warning is harmless: the server's bundled catalog does not know about fresh uploads. Ignore it.
If the job is still running at the window edge, the response is `{prompt_id, status:"in_progress", timed_out:true}`;
continue with `wait_for_job`.

What the server actually ran (from `get_job_status(include_workflow:true)`), which is the graph to copy for
`submit_workflow`:
```json
{"1":{"class_type":"LoadImage","inputs":{"image":"<plate name>"}},
 "2":{"class_type":"LoadImageMask","inputs":{"image":"<mask name>","channel":"red"}},
 "3":{"class_type":"FluxProFillNode","inputs":{"image":["1",0],"mask":["2",0],"prompt":"…","prompt_upsampling":false,"guidance":60,"steps":50,"seed":1234}},
 "4":{"class_type":"SaveImage","inputs":{"images":["3",0],"filename_prefix":"comfy_partner_bfl-flux-pro-fill_bfl-flux-pro-fill"}}}
```
Mask convention: white = repaint. A single-channel grayscale PNG works with `channel:"red"`. An RGB mask works too.

### 2.3 `submit_workflow` [verified]
Args: `workflow` (API format: `{id:{class_type, inputs, _meta:{title}}}`, as a JSON object and not a string),
`confirm`, `dry_run` (free validation), `from_prompt_id` + `input_overrides` (re-run a past graph with changes),
`extra_data`. Editor-format graphs (top-level `nodes`) are rejected.

**Live test 2 [verified]**: Flux Erase (the empty-prompt "remove object" path):
```json
{"1":{"class_type":"LoadImage","inputs":{"image":"<plate>"}},
 "2":{"class_type":"LoadImageMask","inputs":{"image":"<mask>","channel":"red"}},
 "3":{"class_type":"FluxEraseNode","inputs":{"image":["1",0],"mask":["2",0],"dilate_pixels":10,"seed":7}},
 "4":{"class_type":"SaveImage","inputs":{"images":["3",0],"filename_prefix":"genfill_erase"}}}
```
Response:
```json
{"result":{"prompt_id":"41a7494a-3a22-436e-b7df-21cfcebd3fd0","lifted_from_save":false,
  "lifecycle":"Job queued — poll wait_for_job ...","status":"succeeded_with_warnings",
  "warnings":[{"code":"paid_api_nodes",...},{"code":"input_validation",...}]}}
```
The `dry_run:true` response is `{"result":{"status":"validated","warnings":[…],"submitted":false}}`.

### 2.4 `wait_for_job` / `get_job_status` [verified]
- `wait_for_job {"prompt_id":…, "max_wait_seconds":120}` returned
  `{"result":{"status":"succeeded","warnings":[],"job_status":"completed","raw_status":"completed"}}` (the erase job took about 10 s).
  Terminal `job_status` values are `completed | error | cancelled`. A timeout gives `{timed_out:true}`, so call it again.
  `stalled:true` is possible. Max 600 s per call (the server may cap it lower; `hold_seconds` reports the cap).
- `get_job_status` is a snapshot with `raw_status` (submitted, queued_limited, executing, completed…).
  `include_workflow:true` returns the stored API graph. On error/cancel, `billed:false` means it never ran.

### 2.5 `get_output` [verified]
Args: `{"prompt_id":…, "client_os":"darwin", "inline_urls":false}`. `offset` pages large outputs.
```json
{"results":[{"source_node_id":"4","filename_prefix":"genfill_erase","class_type":"SaveImage",
  "filename":"21b66995…41d.png","url":"https://cloud.comfy.org/api/s/kcNiuS2E4t_ORcBVGeFw4Q?raw=1",
  "suggested_save_path":"~/Downloads/ComfyUI/comfy_output.png","saved_to_disk":false,
  "download_command":"curl -L ...","inline_url":"https://storage.googleapis.com/…(signed, X-Goog-Expires=21599)",
  "width":512,"height":512}]}
```
- **Where the image is**: `results[i].url`. It is a **public capability link (no auth)**. Verified by fetching it
  with no headers: `302 Location: https://storage.googleapis.com/comfy-cloud-assets/<sha>.png?X-Goog-…` and
  then 200 `image/png`. `urllib.request.urlopen(url)` follows the redirect automatically (verified: 216434 bytes,
  PNG 512x512 RGB). `?raw=1` still redirected in my test.
- **Expiry**: docs say the shortlink lives "~5 min". A link minted about 2 min earlier still worked. The signed GCS
  URL has `X-Goog-Expires=21599` (6 h). Download right away; if it fails, call `get_output` again for a fresh link.
- Each call mints a new shortlink (the second `get_output` for job 1 gave a different `/api/s/` id).
- `width`/`height` are reported for PNGs, which lets you sanity-check before downloading.
- Results are ordered per SaveImage node, and `source_node_id` tells you which branch each one came from.
- Every output is also saved to the user's Comfy Cloud asset library. There is no opt-out.

### 2.6 `submit_batch` (for N variations, alternative)
`items[]` (1–50) of `{tool:"partner_generate"|"submit_workflow"|"run_template", …}`. There is one spend gate for the
whole batch. Returns a `batch_id` plus job ids. Then `wait_for_batch` / `get_batch_status` / `get_batch_output`.
Not live-tested.

**Recommended for N variations**: ONE `submit_workflow` whose graph has a single `LoadImage` and `LoadImageMask`, N
`FluxProFillNode` nodes with different seeds, and N `SaveImage` nodes. You get one prompt_id, one wait, and one
`get_output` returning N results (use `offset` if the response notes a size limit). The fill nodes call the partner
API, so they presumably run concurrently inside one job; this was not measured.

### 2.7 Billing / usage [verified]
- `get_billing_activity {"limit":N}` returns events with `credits_used` per job.
  Before the tests the newest API event was from 2026-10-01. After the tests:
  - `job a8d98669… model=flux-pro-1.0-fill credits_used=10.55`
  - `job 41a7494a… model=flux-tools/erase-v1 credits_used=6.33`
- `estimate_credits {workflow|template_name}` is a free quote: FluxProFillNode 11, FluxEraseNode ~6–13,
  BriaGenFill 9, BriaEraser 6, RecraftImageInpaintingNode 12, IdeogramV3 unknown. `partner_generate` slugs cannot be
  priced directly, so price the equivalent node graph.
- `get_usage_report` gives the dollar spend by model/month (invoice-level).

---

## 3. Mask-capable / edit-capable models

### 3.1 Partner slugs for `partner_generate` (from `get_prompting_guide(model:"partner")`, 17 image slugs total)
| slug | mask? | how | notes |
|---|---|---|---|
| **`bfl/flux-pro-fill`** | **yes** | `medias:[{role:image},{role:mask}]` | async, library-persisted. Same size as input. Params: `guidance` (1.5–100, default 60), `steps` (15–50, default 50), `prompt_upsampling`. **Tested.** |
| `openai/images-generations` | yes | `medias:[{role:image},{role:mask}]`, `params.model` e.g. `gpt-image-1.5` | With a mask it runs on the **synchronous direct path**: the result is `structuredContent.results[]{url, download_command}` and is not saved to the library. A mask edit "sends no model" unless you name `params.model`. Output sizes are fixed (1024², 1536x1024, …), not the crop size. |
| `ideogram/generate` | yes | `medias:[{role:image},{role:mask}]` routes to Ideogram V3 edit | Image/mask edits persist. `style_type`/`negative_prompt`/`reference_image` go to the direct path. Fixed resolutions. |
| `xai/grok-image-edit` | yes (via `params.mask`) | `params.image`/`params.mask` | A mask switches it to the synchronous direct path. `params.model` e.g. `grok-imagine-image-2.0`. |
| `bfl/flux-kontext-pro` / `-max` | no mask | `medias:[{role:image}]` + instruction prompt | Re-renders the whole image (fine, since GenFill composites through the mask). Needs `aspect_ratio`. |
| `vertexai/nano-banana-2`, `-2-lite`, `-pro` | no mask | `medias:[{role:image}]` + prompt | Edit by instruction. `-2-lite` is the cheap ~1K tier. |
| `qwen/qwen-image-3` | no mask | 1–3 `medias role:image` | Output matches the input size unless width/height are given. `params.n` 1–6. |
| `byteplus/images-generations` | no mask | `params.model` e.g. `seedream-4-0-250828`, `params.image` (URL/base64) | Seedream/SeedEdit. |
| `bfl/flux-2-pro` / `-max` | no mask | `medias role:image / reference_image` (≤9) | |
| `bfl/flux-pro-expand` | n/a | outpaint, `params.top/bottom/left/right` | |

### 3.2 Mask-taking partner API nodes (for `submit_workflow`; `search_nodes input_type:MASK api_nodes_only`)
- `FluxProFillNode` (image, mask, prompt, prompt_upsampling, guidance, steps, seed): about 11 credits.
- **`FluxEraseNode`** (image, mask, `dilate_pixels` 0–25 default 10, optional seed): remove object, no prompt. About 6.3 credits. **Tested, clean result.**
- `BriaGenFill` (image, mask, prompt, negative_prompt, refine_prompt, seed ≥1, `moderation:"false"`): about 9 credits.
  The mask is binarized and must have the same aspect ratio as the image. Bria keeps pixels outside the mask exactly unchanged.
- `BriaEraser` (image, mask, `mask_type:"manual"|"automatic"`, `moderation:"false"`): about 6 credits.
- `RecraftImageInpaintingNode` (image, mask, prompt, `n` 1–6, seed, optional negative_prompt): about 12 credits.
  Native `n` gives variations in one call.
- `IdeogramV3` (prompt, image, mask, `num_images` 1–8, rendering_speed TURBO/DEFAULT/QUALITY, …): price unknown.
- `OpenAIGPTImageNodeV2` (model dynamic combo gpt-image-2.5-flare/sunburst/2/1.5/1, `n`, …). The older
  `OpenAIGPTImage1` (image + mask + n) is deprecated.
- `BeebleSwitchXImageEdit` (alpha_mode with mask): a niche option.
- Note the Bria nodes use dynamic-combo inputs: send `"moderation":"false"`. If `"true"`, the sub-fields must be
  named `moderation.prompt_content_moderation` etc.

### 3.3 Templates (`search_templates q:"inpaint"` / `tag:"Inpainting"`)
- `flux_fill_inpaint_example`: **FLUX.1 Fill [dev]** (OSS, GPU-time billed, 0 API credits). A subgraph template with
  slots `47.pixels`, `47.mask`, `47.text`, `47.unet_name` (flux1-fill-dev.safetensors), `47.clip_name1/2`
  (clip_l / t5xxl_fp16), `47.vae_name` (ae), and the seed via `input_overrides {"47":{"seed":…}}`. Top-level nodes:
  LoadImage(17), subgraph(47), SaveImage(9). [inferred] The template probably feeds the mask from LoadImage's alpha (MASK output 1); I did not read the full graph.
  (The schema listing showed shifted `default` values; trust the names, not those defaults.)
- `image_flux.1_fill_dev_OneReward`: a Flux Fill dev fine-tune for inpaint, outpaint and object removal (a good OSS removal option).
- `image_qwen_image_instantx_inpainting_controlnet`: Qwen-Image + InstantX inpaint ControlNet.
- `api_bria_genfill`, `api_bria_eraser`, `api_flux_erase_image`, `api_openai_image_1_inpaint`: API wrappers of the nodes above.

**OSS FLUX.1 Fill dev graph that I hand-built in API format** (passed `dry_run`, status `validated`; not executed):
```json
{"1":{"class_type":"LoadImage","inputs":{"image":"<plate>"}},
 "2":{"class_type":"LoadImageMask","inputs":{"image":"<mask>","channel":"red"}},
 "3":{"class_type":"UNETLoader","inputs":{"unet_name":"flux1-fill-dev.safetensors","weight_dtype":"default"}},
 "4":{"class_type":"DualCLIPLoader","inputs":{"clip_name1":"clip_l.safetensors","clip_name2":"t5xxl_fp16.safetensors","type":"flux"}},
 "5":{"class_type":"VAELoader","inputs":{"vae_name":"ae.safetensors"}},
 "6":{"class_type":"CLIPTextEncode","inputs":{"text":"<prompt>","clip":["4",0]}},
 "7":{"class_type":"CLIPTextEncode","inputs":{"text":"","clip":["4",0]}},
 "8":{"class_type":"FluxGuidance","inputs":{"conditioning":["6",0],"guidance":30}},
 "9":{"class_type":"InpaintModelConditioning","inputs":{"positive":["8",0],"negative":["7",0],"vae":["5",0],"pixels":["1",0],"mask":["2",0],"noise_mask":true}},
 "10":{"class_type":"DifferentialDiffusion","inputs":{"model":["3",0]}},
 "11":{"class_type":"KSampler","inputs":{"model":["10",0],"positive":["9",0],"negative":["9",1],"latent_image":["9",2],"seed":1,"steps":20,"cfg":1,"sampler_name":"euler","scheduler":"normal","denoise":1}},
 "12":{"class_type":"VAEDecode","inputs":{"samples":["11",0],"vae":["5",0]}},
 "13":{"class_type":"SaveImage","inputs":{"images":["12",0],"filename_prefix":"genfill"}}}
```
For N variations, set `KSampler.seed` and add `RepeatLatentBatch` (amount N) between `9:2` and the latent input, or
duplicate the sampler/decoder/save chain per seed. Dimensions should be multiples of 16 (pad or crop the plate crop).
OSS runs are billed as GPU seconds (`gpu_seconds` in the billing feed), and cold starts may be slow.

---

## 4. Recommended GenFill client design

1. **Auth**: support an API key (simple, no refresh). Add OAuth as an option: DCR per login with a
   `127.0.0.1:<free port>/callback` redirect, PKCE S256, scope `comfy-mcp:tools:read comfy-mcp:tools:call`,
   `resource=https://cloud.comfy.org/mcp`.
2. **Session**: `initialize`, then `notifications/initialized`, then `tools/call`. Handle both JSON and SSE response
   bodies and the `Mcp-Session-Id` header. Retry init on 404/expired session.
3. **Upload**: `tools/call upload_file {file_path:"/tmp/genfill_plate.png", client_os:"darwin"}`, regex the PUT URL,
   PUT the bytes, and keep the `name`. Do the same for the mask (white = fill, same size, grayscale or RGB PNG).
4. **Generate N** with one `submit_workflow`, `confirm:true`:
   - prompt non-empty: N × `FluxProFillNode` (seed = base+i, guidance 60, steps 50) → N × `SaveImage`;
   - prompt empty: N × `FluxEraseNode` (dilate_pixels ~10, seed = base+i) → N × `SaveImage`,
     or a single erase (it is near-deterministic).
   Alternatively, for one image, use `partner_generate` model `bfl/flux-pro-fill` with `wait_for_output:true`.
5. **Wait**: loop `wait_for_job {max_wait_seconds:120}` until terminal. Surface `error` / `cancelled`.
6. **Fetch**: call `get_output {inline_urls:false}`. For each result, `urllib.request.urlopen(result["url"])`
   (it follows the 302 to GCS) and decode the PNG bytes. If the URL has expired, re-call `get_output`.
7. **Composite** the result through the mask in Nuke. Output size matched the input (512→512) for both Flux Fill and
   Flux Erase. Re-check this for 1024x768 (BFL may snap to multiples of 32; untested).

## 5. Open points / not verified
- Raw MCP wire format (JSON vs SSE, session header behaviour) with a real key: no key was available here.
- Whether `upload_file`'s raw MCP result has a `structuredContent` field with the URL (it would be cleaner than regex).
- Flux Fill output size for non-square or non-multiple-of-32 inputs (e.g. 1024x768).
- OAuth access-token lifetime and refresh-token rotation.
- Variation quality: Flux Pro Fill on the synthetic test plate produced a visible faint grid/disc texture inside the
  fill area (test-image artefact, low-detail input). Flux Erase on the same input was seamless.

## 6. Test artefacts (scratchpad)
- `gf_test_plate.png` (512x512 RGB: sky, grass, red disc), `gf_test_mask.png` (512x512 L, white disc r=90)
- `out/flux_fill.png`: Flux Pro Fill result, PNG 512x512 RGB, 54 KB (job a8d98669-1b7b-4e09-a8d8-9ea39c74407a)
- `out/flux_erase.png`: Flux Erase result, PNG 512x512 RGB, 216 KB (job 41a7494a-3a22-436e-b7df-21cfcebd3fd0)
- DCR test clients created (harmless, unused): `comfy-dyn-<redacted>`,
  `comfy-dyn-<redacted>`
