# fal.ai REST API for GenFill (Nuke, stdlib urllib)

Research date: 2026-10-15. This is docs research only; no API key was available, so nothing was called with auth.
Sources: the live OpenAPI specs at `https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>` (fetched for
every endpoint below; HTTP 200 means the id exists), the per-model `https://fal.ai/models/<id>/llms.txt` (pricing),
`https://fal.ai/docs/.../queue.md`, `.../fal-cdn.md`, `.../errors.md`, `.../request-errors.md`, the catalog search
API `https://api.fal.ai/v1/models?q=<term>` (works without a key), and the official Python client source
`fal-ai/fal: projects/fal_client/src/fal_client/client.py` (main branch) for the upload flow and URL building.

---

## 1. Auth and queue API

### Auth
Every request to `queue.fal.run` and `rest.fal.ai` uses this header:
```
Authorization: Key <FAL_KEY>
```
(`FAL_KEY` may also be `key_id:key_secret`; the header is still `Key <that string>`.)
Send `Content-Type: application/json` on POSTs. Result media URLs (`https://v3.fal.media/...`, `v3b.fal.media`)
are public and need no auth to download.

### Submit
```
POST https://queue.fal.run/<endpoint_id>
Authorization: Key <FAL_KEY>
Content-Type: application/json

{ ...model input JSON... }
```
Response (HTTP 200):
```json
{
  "request_id": "764cabcf-b745-4b3e-ae38-1200304cf45b",
  "response_url": "https://queue.fal.run/<app_base>/requests/<id>",
  "status_url":   "https://queue.fal.run/<app_base>/requests/<id>/status",
  "cancel_url":   "https://queue.fal.run/<app_base>/requests/<id>/cancel",
  "queue_position": 0
}
```

**Subpath quirk.** `<app_base>` is only the first two path segments of the endpoint id (`owner/alias`); the rest of the
path is dropped for status, result and cancel. The official client builds them like this (`AppId.from_endpoint_id`):
`parts = id.split("/")`; `owner=parts[0]`, `alias=parts[1]`, `path=parts[2:]`. The exceptions are the namespaces
`workflows` and `comfy`, where the base is the first three segments. Examples:

| endpoint_id (submit) | status/result base |
|---|---|
| `fal-ai/flux-pro/v1/fill` | `fal-ai/flux-pro/requests/<id>` |
| `fal-ai/qwen-image-edit/inpaint` | `fal-ai/qwen-image-edit/requests/<id>` |
| `openai/gpt-image-2.5/flare/edit` | `openai/gpt-image-2.5/requests/<id>` |
| `bria/genfill/v2` | `bria/genfill/requests/<id>` |

The OpenAPI specs list the full path (`/fal-ai/flux-pro/v1/fill/requests/{id}`), but the client does not use it.
**Always use the `status_url` / `response_url` / `cancel_url` returned by the submit call.** Only construct them
yourself (with the owner/alias rule) when you resume from a stored `request_id`.

Optional submit headers:
- `X-Fal-No-Retry: 1` turns off fal's automatic re-queue on runner failures (default is up to 10 retries).
- `X-Fal-Request-Start-Timeout: <seconds>` is a server deadline that applies only until processing starts. Past it the
  server returns 504 with `X-Fal-Request-Timeout-Type: user`.
- `X-Fal-Queue-Priority: low|normal`.
- `X-Fal-Object-Lifecycle-Preference: {"expiration_duration_seconds": 86400}` sets the retention of output media.
- Query string `?fal_webhook=<url>` sets a webhook. A Nuke plugin does not need it.

### Poll status
```
GET <status_url>            (append ?logs=1 to include runner logs)
Authorization: Key <FAL_KEY>
```
The `status` values are `IN_QUEUE`, `IN_PROGRESS` and `COMPLETED`.
```json
{"status":"IN_QUEUE","request_id":"...","queue_position":2,"response_url":"..."}
{"status":"IN_PROGRESS","request_id":"...","response_url":"...","logs":[{"message":"...","timestamp":"..."}]}
{"status":"COMPLETED","request_id":"...","response_url":"...","logs":[...],"metrics":{"inference_time":3.42}}
```
A failed request still reaches `COMPLETED`, but then carries `"error": "<human text>"` and `"error_type": "<code>"`.
Always check for `error` when the status is COMPLETED. The status endpoint returns HTTP 200 while IN_QUEUE, IN_PROGRESS
and COMPLETED (older behavior sends 202 while queued, so treat 200 and 202 alike). Poll every 1-2 s.

There is also SSE: `GET <status_url>/stream?logs=1` returns `text/event-stream` until COMPLETED. It is not needed;
plain polling is simpler with urllib.

### Fetch the result
```
GET <response_url>
Authorization: Key <FAL_KEY>
```
On success this returns HTTP 200 with the model's output JSON (section 3). Then download every
`images[i].url` (or `image.url` on some endpoints) with a plain GET. If the input set `"sync_mode": true`, `url` is a
`data:` URI instead, so handle both forms.

On failure the result GET returns a non-2xx code with a JSON body:
- Model or validation errors (usually 422, or 400): `{"detail": [{"loc": ["body","image_url"], "msg": "...", "type": "image_too_large", "url": "...", "ctx": {...}}]}`.
  Use `type` for logic and `msg` for display. Pydantic types such as `missing` and `enum` pass through.
  `content_policy_violation` is a 422 and is not retryable.
- Infrastructure errors (5xx, 499): `{"detail": "Request timed out", "error_type": "request_timeout"}`; the header
  `X-Fal-Error-Type` carries the same value. Types such as `runner_*` and `startup_timeout` are transient.
- `detail` is a list for model errors and a string for infra errors. Parse both.
- The header `X-Fal-Needs-Retry` may be present.

### Cancel
```
PUT <cancel_url>
Authorization: Key <FAL_KEY>
```
- 202 `{"status":"CANCELLATION_REQUESTED"}`. If the job was IN_QUEUE it is removed. If it was IN_PROGRESS, the job may
  still finish and be billed.
- 400 `{"status":"ALREADY_COMPLETED"}`.
- 404 `{"status":"NOT_FOUND"}`.

### Limits and billing
- Concurrency: a new account runs 2 requests at once. The limit rises with credit purchases, up to 40. Extra
  requests wait in the queue; they are not rejected.
- 5xx errors are never billed. A 422 may be billed if GPU time was already spent.
- Media URLs expire according to the account or request lifecycle setting, so download results right away.

### Synchronous alternative (not recommended)
`POST https://fal.run/<endpoint_id>` blocks until the result is ready and returns the output JSON directly. Long
generations can hit HTTP timeouts; use the queue.

---

## 2. Input images: data URIs vs CDN upload

### Data URIs
The docs (fal-cdn page) say: "Some models also accept data URIs (`data:image/png;base64,...`) ... URLs are the universal
format that works with every model". They warn that data URIs "inflate the request size" and are "not recommended for
files larger than a few KB".
- Explicitly documented as accepted: `blackforestlabs/flux-3/edit-image` ("Reference image URLs or data URIs"),
  `meta/muse-image/edit` ("HTTP(S) or data URLs"), and `video_url`/`pdf_url` on nano-banana-2 ("http(s)/data URL ...
  max 15MB").
- Every other endpoint's schema just says "URL". In practice fal-ai/* endpoints download inputs through the fal
  toolkit, which also decodes data URIs, but that is not guaranteed for partner endpoints (openai/, bria/, ideogram/,
  bytedance/, google/, alibaba/).
- No total request-body limit is documented. Per-model limits apply. The generic `file_too_large` error shows
  `ctx.max_size: 10485760` (10 MB). Qwen-image-2/max/3 state "Max size: 10MB each". Ideogram v3 caps style references
  at 10 MB total. flux-kontext/dev states "Max width/height 14142px, download timeout 20s".
- A 1024x768 PNG crop is about 1-2 MB, or about 1.3-2.7 MB as base64. That is workable, but it is resent for every
  variation and every endpoint.

**Recommendation:** upload the plate crop and the mask to the fal CDN once per Generate click, and reuse the two URLs
for all N requests and endpoints. Keep a data-URI mode as a fallback setting. A test with a real key is needed to
confirm data URIs on partner endpoints.

### CDN upload: current flow, taken from the official Python client (fal_client, main branch)
`rest.alpha.fal.ai` is the old host. The current client uses `REST_URL = "https://rest.fal.ai"` and
`CDN_URL = "https://v3.fal.media"`. The default chain is: v3 CDN upload, then a fallback to storage initiate.

**Primary: v3 CDN (two calls)**
1. Get a CDN token. Cache it until `expires_at`.
   ```
   POST https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3
   Authorization: Key <FAL_KEY>
   Accept: application/json
   Content-Type: application/json

   {}
   ```
   Response: `{"token": "...", "token_type": "Bearer", "base_url": "https://v3b.fal.media", "expires_at": "<ISO8601>"}`.
   `token_type` is used verbatim in the next header. The `Bearer` value is what it normally contains.
2. Upload the raw bytes:
   ```
   POST https://v3.fal.media/files/upload
   Authorization: <token_type> <token>
   Content-Type: image/png
   X-Fal-File-Name: genfill_plate.png          (optional)
   X-Fal-Object-Lifecycle: {"expiration_duration_seconds": 86400}   (optional)

   <raw PNG bytes>
   ```
   Response JSON: `{"access_url": "https://v3b.fal.media/files/b/<prefix>/<name>.png", ...}`. Use `access_url` as
   `image_url` or `mask_url`.
   Files over 100 MB use multipart at `<base_url>/files/upload/multipart`. GenFill never needs that.

**Fallback: storage initiate plus a signed PUT**
1. Initiate:
   ```
   POST https://rest.fal.ai/storage/upload/initiate?storage_type=gcs
   Authorization: Key <FAL_KEY>
   Accept: application/json
   Content-Type: application/json
   X-Fal-Object-Lifecycle-Preference: {"expiration_duration_seconds": 86400}   (optional)

   {"file_name": "genfill_plate.png", "content_type": "image/png"}
   ```
   Response: `{"upload_url": "<signed URL>", "file_url": "<public URL>"}`.
2. Upload:
   ```
   PUT <upload_url>
   Content-Type: image/png

   <raw bytes>
   ```
   Send no Authorization header on this PUT. Afterwards use `file_url` as the input.

Lifecycle values the client uses: `immediate` = 60 s, `1h` = 3600, `1d` = 86400, `7d` = 604800, `30d`, `1y`. If the
header is omitted, account retention applies. Uploads are public by default; anyone who has the URL can read them.

---

## 3. Endpoint catalogue

### Shared shapes
- **Output image object** (`Image` / `ImageFile` / `File`): `{"url": str, "content_type": str?, "width": int?, "height": int?, "file_name"?, "file_size"?}`.
  Only `url` is always present. `width` and `height` are required on the FLUX/Qwen `Image` type but missing on `File`
  (Ideogram, Qwen-image-2/3) and optional on `ImageFile`. Read the dimensions from the decoded PNG or JPEG, not from JSON.
- **Most endpoints return `{"images": [ ... ]}`. A few return a single `{"image": {...}}`:** `fal-ai/bria/eraser`,
  `bria/eraser`, `fal-ai/lama`, `fal-ai/finegrain-eraser/mask`, `fal-ai/inpaint`. `bria/genfill/v2` and
  `bria/fibo-edit*` return both `image` and `images`. Code should accept `images` or fall back to `[image]`.
- **`image_size`** where it is an `ImageSize | enum`: you can pass a custom object `{"width": W, "height": H}` (each
  integer at most 14142, model-specific bounds apply) or a preset: `square_hd`, `square`, `portrait_4_3`,
  `portrait_16_9`, `landscape_4_3`, `landscape_16_9` (plus `auto` / `auto_2K` / `auto_4K` / `auto_1K` / `auto_3K`
  where listed).
- **Common optional fields:** `seed` (int or null), `output_format` (`jpeg`/`png`[/`webp`]), `sync_mode` (bool; true
  returns data URIs and keeps the result out of request history), `enable_safety_checker` (bool, default true; only
  authorized accounts may turn it off), `safety_tolerance` (string `"1"`-`"6"`, or int 0-4 on flux-3).
- **The result size is not guaranteed to equal the crop size** on most edit models. GenFill must resize every result
  to the crop's width and height before compositing through the mask. Most models also snap to multiples of 8, 16 or 32.

### Mask polarity (critical)
| Convention | Endpoints |
|---|---|
| **White = fill/edit, black = keep** (stated in the schema) | `fal-ai/flux-pro/v1/erase`, `fal-ai/finegrain-eraser/mask`, `fal-ai/object-removal/mask`, `fal-ai/onereward`, `fal-ai/inpaint`, `bria/fibo-edit-1.5/edit`, `openai/gpt-image-2/edit` (model page: "White regions indicate areas to edit") |
| White = edit (stated by example or by the upstream API convention) | `fal-ai/flux-pro/v1/fill` (example mask is white on the subject), `fal-ai/ideogram/v3/edit` (fal's example mask is opaque RGBA, white = the edited bag), `fal-ai/qwen-image-edit/inpaint`, `fal-ai/flux-lora-fill`, `fal-ai/flux-lora/inpainting`, `fal-ai/flux-general/inpainting`, `fal-ai/flux-kontext-lora/inpaint`, `fal-ai/z-image/turbo/inpaint`, `fal-ai/bria/genfill`, `fal-ai/bria/eraser`, `bria/genfill/v2`, `fal-ai/lama` |
| **BLACK = edit, white = keep (invert the mask)** | `ideogram/v4.5/edit` ("Black edits, white preserves. Must contain both regions after rounding.") |
| Not documented | `fal-ai/gpt-image-1.5/edit` (`mask_image_url`), `openai/gpt-image-2.5/*` (`mask_url`). OpenAI's native API uses alpha (transparent = edit). Send an opaque black/white PNG and verify with a real key. |

Mask requirements: send the mask at exactly the same pixel dimensions as `image_url` (required for flux-pro fill,
ideogram, erase and bria fibo). Send it as an opaque single-channel or RGB PNG, binarized: GenFill's soft mask should be
thresholded or slightly dilated for the upload, and the soft mask kept for its own composite.

### 3a. True mask inpainting / fill (prompted)
All ids verified (OpenAPI 200). "max N" is the `num_images` maximum. Prices are from each model's `llms.txt` (USD).

| endpoint_id | mask field | prompt | max N | size control | other notable fields | output | price |
|---|---|---|---|---|---|---|---|
| `fal-ai/flux-pro/v1/fill` | `mask_url` (req) | `prompt` (req) | 4 | none; output = input size; mask must match image | `seed`, `output_format` jpeg\|png (default jpeg), `safety_tolerance` "1"-"6" (default "2"), `enhance_prompt` | `images[]` (url,width,height,content_type), `seed`, `has_nsfw_concepts` | $0.05/MP |
| `fal-ai/flux-pro/v1/fill-finetuned` | `mask_url` | `prompt` | 4 | none | requires `finetune_id`, `finetune_strength` 0-2 | as above | $0.06/MP |
| `fal-ai/flux-lora-fill` (FLUX.1 Fill **dev**; `fal-ai/flux/dev/fill` and `fal-ai/flux-1/dev/fill` do **not** exist) | `mask_url` (req) | `prompt` optional, default `""` | 4 | `image_size` preset or {w,h}; `resize_to_original` bool | `paste_back` (default **true**), `guidance_scale` 28-35 (default 30), `num_inference_steps` 1-50 (28), `loras`, `fill_image`, `seed`, `acceleration` none\|regular | `images[]`, `seed` | $0.035/MP |
| `fal-ai/flux-lora/inpainting` | `mask_url` (req) | req | 4 | `image_size` | `strength` 0.01-1 (0.85; use 1.0 for real fill), `guidance_scale` 0-35 (3.5), steps 1-50 (28), `loras`, `seed` | `images[]` | $0.035/MP |
| `fal-ai/flux-general/inpainting` | `mask_url` (req) | req | **10** | `image_size` | `strength` (0.85), controlnets, ip_adapters, `negative_prompt`, NAG, `seed` | `images[]` | $0.075/MP |
| `fal-ai/flux-krea-lora/inpainting` | `mask_url` | req | 4 | `image_size` | same as flux-lora/inpainting | `images[]` | $0.035/MP |
| `fal-ai/flux-kontext-lora/inpaint` | `mask_url` (req) | req | 4 | none | **also requires `reference_image_url`** (pass the plate again), `strength` (0.88), steps 10-50 (30), `guidance_scale` (2.5), `loras` | `images[]` | $0.035/MP |
| `fal-ai/qwen-image-edit/inpaint` | `mask_url` (req) | req | 4 | `image_size` | `strength` 0.01-1 (0.93), `negative_prompt`, steps 2-50 (30), `guidance_scale` (4), `acceleration` none\|regular\|high, `seed` | `images[]` | $0.03/MP |
| `fal-ai/z-image/turbo/inpaint` | **`mask_image_url`** (req) | req | 4 | `image_size` (+`auto`) | steps 1-8 (8), `strength`, `control_scale`, `seed` | `images[]` | $0.01/MP |
| `fal-ai/ideogram/v3/edit` | `mask_url` (req; exact same dims) | req | **8** | none (output = input) | `rendering_speed` TURBO\|BALANCED\|QUALITY, `expand_prompt` (default **true**; set false for VFX), `style_preset`, `image_urls` (style refs), `seed` | `images[]` (File: url only), `seed` | $0.03 / $0.06 / $0.09 per image |
| `ideogram/v4.5/edit` (2026-09) | `mask_url` optional, **black = edit** | req (max 10000 chars) | **8** | `image_size` only for unmasked edits; masked output = source geometry | `edit_precision` regular\|high ("high" restores unchanged pixels), `quality` very_low\|low\|medium\|high, `reference_image_urls` (max 3 with a mask), `seed` | `images[]`, `seed` | $0.008 / $0.03 / $0.06 / $0.22 per image by quality |
| `openai/gpt-image-2/edit` (= `fal-ai/gpt-image-2/edit`, same schema) | **`mask_url`** per OpenAPI (the model page prose says `mask_image_url`; unknown fields are likely ignored silently, so test which one works) | req | 4 | `image_size` preset, {w,h}, or `auto` | `image_urls` (req, max 16), `quality` auto\|low\|medium\|high (default high), `background` | `images[]` | token-based; at 1024x768: low $0.011, medium $0.043, high $0.151 |
| `openai/gpt-image-2.5/flare/edit`, `openai/gpt-image-2.5/sunburst/edit` (2026-09) | `mask_url` | req | **10** | `image_size` / `auto` | `quality` auto\|low\|medium\|high\|xhigh\|max (default high), `image_urls` max 16, `output_compression` | `images[]` | token-based (text $5/$10 per M, image in $8, image out $30 per M) |
| `fal-ai/gpt-image-1.5/edit` | **`mask_image_url`** | req | 4 | only `auto`, `1024x1024`, `1536x1024`, `1024x1536` | `image_urls` (req), `input_fidelity` low\|high, `quality` low\|medium\|high | `images[]` | high: $0.133 (1024²) to $0.20 per image |
| `fal-ai/gpt-image-1/edit-image` | **no mask field** (the request assumed one; the schema has none) | req | 4 | fixed 3 sizes | `image_urls`, `input_fidelity`, `quality` | `images[]` | high: $0.167 to $0.25 |
| `fal-ai/bria/genfill` | `mask_url` (req) | req | 4 | none | `negative_prompt`, `seed` | `images[]`, `seed` | $0.04/generation |
| `bria/genfill/v2` (2026-06) | `mask_url` (req) | **`instruction`** (req; not `prompt`) | 1 (no `num_images`) | none | `seed` (default 5555, so vary it), `steps_num` 20-50 | `image` + `images[]` | $0.04/MP |
| `bria/fibo-edit-1.5/edit` (2026-08) | `mask_url` optional, white = edit, single reference only | `instruction` | 1 | masked output = reference resolution | `image_urls` (1-4), `seed` (5555) | `image` + `images[]` | $0.04/image |
| `fal-ai/onereward` (FLUX Fill finetune: inpaint, outpaint, removal) | `mask_url` (req) | `prompt` **optional** | 4 | none | `true_cfg` 1-10 (4), `guidance_scale` 0-5 (1), steps (28), `negative_prompt` (default "nsfw"), `seed` | `images[]` | $0.035/MP |
| `fal-ai/fooocus/inpaint` (SDXL, legacy) | `mask_image_url` | optional | 4 | `aspect_ratio` "WxH" | many fields; input field is `inpaint_image_url` | `images[]` | compute-billed |
| `fal-ai/inpaint` (SD/SDXL any model) | `mask_url` | req | 1 | none | requires `model_name` (HF id) | `image` | compute-billed |

### 3b. Object removal (use for an empty prompt)
| endpoint_id | fields | N / seed | output | price |
|---|---|---|---|---|
| `fal-ai/flux-pro/v1/erase` (BFL, 2026-05) | `image_url`, `mask_url` (white = erase; same dims), `dilate_pixels` 0-100 (default 10), `output_format`, no prompt | **no `num_images`, no `seed`**; N variations means N requests, which may come back near-identical | `images[]`, `seed` | $0.03 first MP, then +$0.004/MP (refs billed at a 3 MP minimum, so about $0.04-0.05 for a 1024x768 crop) |
| `fal-ai/finegrain-eraser/mask` | `image_url`, `mask_url` (white = erase), `mode` express\|standard\|premium, `seed` 0-999 | 1 per request, seedable | **`image`** (File), `used_seed` | $0.04 / $0.13 / $0.22 |
| `fal-ai/bria/eraser` (also `bria/eraser`) | `image_url`, `mask_url`, `mask_type` manual\|automatic (use manual), `preserve_alpha` | 1, no seed | **`image`** | $0.04/generation (`bria/eraser`: compute) |
| `fal-ai/object-removal/mask` | `image_url`, `mask_url` (white = remove), `mask_expansion` 0-50 (15), `model` low/medium/high/best_quality | 1, no seed | `images[]` | $0.006 to $0.024 |
| `fal-ai/lama` | `image_url`, **`mask_image_url`** | 1 | **`image`** | compute-billed (cheap) |
| `fal-ai/onereward` | as in 3a with prompt omitted or "" | 4, seed | `images[]` | $0.035/MP |
| `fal-ai/qwen-image-edit-2509-lora-gallery/remove-element` | no mask; `image_urls`, `prompt` (default "Remove the specified element from the scene") | 4 | `images[]` | $0.035/MP |
| `bria/fibo-edit/erase_by_text`, `fal-ai/object-removal` | text-targeted (`object_name` / `prompt`), no mask | 1 | | $0.04 / up to $0.024 |

### 3c. Reference-edit models (no mask; they re-render the whole frame)
For these, GenFill sends the crop as `image_urls[0]` and composites through its own mask. To localize the edit, send
the mask as `image_urls[1]` or a marked-up copy of the crop (for example a red overlay on the fill region) and phrase
the prompt as "In image 1, replace only the area marked in image 2 with ...; keep everything else identical".
Empty prompt: "Remove the marked object and reconstruct the background naturally".

| endpoint_id | image field | max N | size control | other | output | price |
|---|---|---|---|---|---|---|
| `fal-ai/nano-banana/edit` (Gemini 2.5 Flash Image) | `image_urls` (req) | 4 | `aspect_ratio` enum (auto, 21:9 ... 9:16) only | `seed`, `output_format` (png), `safety_tolerance` "1"-"6" (4), `limit_generations` (true) | `images[]` (ImageFile), `description` | $0.039/image |
| `fal-ai/nano-banana-pro/edit` (Gemini 3 Pro Image) | `image_urls` (req) | 4 | `aspect_ratio` + `resolution` 1K\|2K\|4K | `seed`, `system_prompt`, `enable_web_search` | same | $0.15/image (4K x2) |
| `fal-ai/nano-banana-2/edit` (Gemini 3.1 Flash Image, 2026-02) | `image_urls` | 4 | `aspect_ratio` (adds extreme ratios) + `resolution` 0.5K\|1K\|2K\|4K | `thinking_level` minimal\|high, `seed` | same | $0.08/image (2K x1.5, 4K x2, 0.5K x0.75) |
| `google/nano-banana-2.1/edit` (2026-10) | `image_urls` | 4 | `aspect_ratio` + `resolution` 1K\|2K\|4K | `thinking_level` minimal\|medium\|high, `seed` | same | token-based; about $0.044 (1K), $0.063 (2K), $0.138 (4K) per edit |
| `blackforestlabs/flux-3/edit-image` (2026-10-01, newest BFL) | `image_urls` (1-10; data URIs OK; at least 256 px; downscaled to 4 MP max) | **1** (no `num_images`, no `seed`) | `resolution` 512sq\|768sq\|1k\|2k\|4k + `aspect_ratio` (default auto = from image 1) | `safety_tolerance` int 0-4, `enable_prompt_expansion`, `version` ("latest") | `images[]` | from $0.048 per 1 MP after the launch discount ended Oct 8 (was $0.024) |
| `fal-ai/flux-2-pro/edit` | `image_urls` | 1 (no num_images) | `image_size` preset / {w,h} / `auto` | `seed`, `safety_tolerance`, `output_format` | `images[]`, `seed` | $0.03 first MP, then +$0.015/MP (in and out) |
| `fal-ai/flux-2-max/edit` | `image_urls` | 1 | same | same | same | $0.07 first MP, then +$0.03 |
| `fal-ai/flux-2-flex/edit` | `image_urls` | 1 | same | `guidance_scale`, steps | same | $0.05/MP in and out |
| `fal-ai/flux-2/edit` (FLUX.2 dev) | `image_urls` (max 4) | 4 | `image_size` {w,h} 512-2048 | `guidance_scale` (2.5), steps 4-50 (28), `acceleration`, `seed` | `images[]` | $0.012/MP in and out |
| `fal-ai/flux-2/turbo/edit`, `fal-ai/flux-2/flash/edit` | `image_urls` (max 4) | 4 | `image_size` 512-2048 | `seed` | `images[]` | $0.008 / $0.005 per MP in and out |
| `fal-ai/flux-2/klein/9b/edit` (2026-01) | `image_urls` (max 4) | 4 | `image_size` (default = input size) | steps 4-8 | `images[]` | $0.011/MP in and out |
| `fal-ai/flux-pro/kontext` / `fal-ai/flux-pro/kontext/max` | **`image_url`** (singular) | 4 | `aspect_ratio` only | `guidance_scale` (3.5), `safety_tolerance`, `seed` | `images[]` | $0.04 / $0.08 per image |
| `fal-ai/flux-kontext/dev` | `image_url` | 4 | `resolution_mode` (default `match_input`) | steps, `guidance_scale`, `seed` | `images[]` | $0.025/MP |
| `fal-ai/bytedance/seedream/v4/edit` | `image_urls` (max 10) | 6 (+`max_images`) | `image_size` {w,h}, area at least 921600 px (1024x768 = 786432 gets upscaled), `auto*` | `seed`, `enhance_prompt_mode` | `images[]`, `seed` | $0.03/image |
| `fal-ai/bytedance/seedream/v4.5/edit` | `image_urls` | 6 | `image_size` area 2560x1440 to 4096x4096 (minimum about 3.7 MP) | `seed` | `images[]` | $0.04/image |
| `bytedance/seedream/v5/pro/edit` (2026-07; note **no `fal-ai/` prefix**; `fal-ai/bytedance/seedream/v5/edit` does not exist) | `image_urls` | 6 | `image_size` area 1024² to 2048², `auto_1K`/`auto_2K` | `enhance_prompt_mode`, `output_format`, **no seed** | `images[]` | $0.0675/image up to 1536², $0.135 above |
| `bytedance/seedream/v5/lite/edit` | `image_urls` | 6 | area 2560x1440 to 4096² | `max_images` | `images[]`, `seed` | $0.035/image |
| `bytedance/seedream/v5/flash/edit` (2026-09) | `image_urls` | 6 | area 1024² to 2048² | no seed | `images[]` | $0.027/image |
| `fal-ai/qwen-image-edit-plus` (= Qwen-Image-Edit-2509) and `fal-ai/qwen-image-edit-2509` | `image_urls` | 4 | `image_size` | `negative_prompt`, steps 2-100 (50), `guidance_scale` (4), `seed` | `images[]` | $0.03/MP |
| `fal-ai/qwen-image-edit-2511` | `image_urls` | 4 | `image_size` (default = input dims) | steps (28), `guidance_scale` (4.5), `seed` | `images[]` | $0.03/MP |
| `fal-ai/qwen-image-edit` (original) | `image_url` | 4 | `image_size` | | `images[]` | $0.03/MP |
| `fal-ai/qwen-image-2/edit`, `fal-ai/qwen-image-2/pro/edit`, `fal-ai/qwen-image-max/edit` | `image_urls` (1-3, at most 10 MB each, PNG without alpha) | 6 | `image_size` (default = input), area 512² to 2048² | `enable_prompt_expansion` (default **true**), `negative_prompt`, `seed` | `images[]` (File) | $0.035 / $0.075 / $0.075 per image |
| `alibaba/qwen-image-3/edit` (2026-07) | `image_urls` (1-3, 384-2048 px, 10 MB) | 6 | `image_size` area 512² to 2048² | `seed`, prompt expansion (default true) | `images[]` | $0.04 (1K) / $0.075 (2K) |
| `meta/muse-image/edit` (2026-08) | `image_urls` (data URLs OK) | 10 | `aspect_ratio` "W:H" only, about 2.5 MP fixed | no seed | `images[]` | $0.01/image |
| `fal-ai/gpt-image-1-mini/edit` | `image_urls` | 4 | 3 fixed sizes | `quality` | `images[]` | $0.005 to $0.052 |

Other 2026 edit endpoints exist but were not schema-checked: `xai/grok-imagine-image/v2.0/edit`,
`microsoft/mai-image-2.5/edit`, `luma/agent/uni-1/v1/edit`, `fal-ai/wan/v2.7/edit`, `fal-ai/hunyuan-image/v3/instruct/edit`,
`fal-ai/firered-image-edit-v1.1`, `fal-ai/longcat-image/edit`. Use the catalog search to find more:
`GET https://api.fal.ai/v1/models?q=<term>&limit=100` (no key needed; each item has `endpoint_id` and
`metadata.category == "image-to-image"`).

**Ids that returned 404 (do not use):** `fal-ai/flux-1/dev/fill`, `fal-ai/flux/dev/fill`,
`fal-ai/bytedance/seedream/v5/edit`, `fal-ai/qwen-image-edit-plus/inpaint`, `fal-ai/qwen-image-edit-2509/inpaint`,
`fal-ai/sdxl-inpainting`, `fal-ai/flux-pro/v1.1-ultra/fill`, `fal-ai/flux-2/inpaint`, `blackforestlabs/flux-3/{fill,inpaint,erase}`,
`bria/fibo-edit` (the bare id; sub-routes exist).

---

## 4. Recommended curated default list for GenFill

Ordered. Each entry gives the request body template, with `{IMG}` and `{MASK}` as CDN URLs, `{W}` and `{H}` as the crop
size, and `{N}` as the variation count.

| # | endpoint_id | role | body template | N handling |
|---|---|---|---|---|
| 1 | `fal-ai/flux-pro/v1/fill` | **Default when a prompt is given.** Real mask inpainting, output at input size, strong photoreal fill. | `{"image_url":IMG,"mask_url":MASK,"prompt":P,"num_images":min(N,4),"output_format":"png","safety_tolerance":"5","seed":S}` | one call if N<=4; extra calls with seed+k |
| 2 | `fal-ai/flux-pro/v1/erase` | **Default for an empty prompt (removal).** Dedicated BFL eraser, cheap, mask-exact. | `{"image_url":IMG,"mask_url":MASK,"dilate_pixels":10,"output_format":"png"}` | no seed or num_images; send N parallel requests, or use #7 when N>1 matters |
| 3 | `fal-ai/ideogram/v3/edit` | Mask fill; up to 8 variations per call; good at text and graphics. | `{"image_url":IMG,"mask_url":MASK,"prompt":P,"num_images":min(N,8),"rendering_speed":"QUALITY","expand_prompt":false,"seed":S}` | single call |
| 4 | `openai/gpt-image-2/edit` | Mask edit with strong instruction following. | `{"image_urls":[IMG],"mask_url":MASK,"prompt":P,"num_images":min(N,4),"image_size":{"width":W,"height":H} (or "auto"),"quality":"high","output_format":"png"}` | single call |
| 5 | `fal-ai/qwen-image-edit/inpaint` | Cheap mask inpaint; open model; size control. | `{"image_url":IMG,"mask_url":MASK,"prompt":P,"num_images":min(N,4),"image_size":{"width":W,"height":H},"strength":0.93,"output_format":"png","seed":S}` | single call |
| 6 | `fal-ai/flux-lora-fill` | FLUX.1 Fill [dev]: cheap, prompt optional, `paste_back`. | `{"image_url":IMG,"mask_url":MASK,"prompt":P or "","num_images":min(N,4),"resize_to_original":true,"paste_back":true,"output_format":"png","seed":S}` | single call |
| 7 | `fal-ai/onereward` | Mask fill **and** removal with seeds and N up to 4. The best removal choice when variations are wanted. | `{"image_url":IMG,"mask_url":MASK,"prompt":P or omit,"num_images":min(N,4),"output_format":"png","seed":S}` | single call |
| 8 | `fal-ai/bria/genfill` | Commercially safe (licensed training data) mask fill. | `{"image_url":IMG,"mask_url":MASK,"prompt":P,"num_images":min(N,4),"seed":S}` | single call |
| 9 | `fal-ai/finegrain-eraser/mask` | Premium removal that also removes shadows and reflections. | `{"image_url":IMG,"mask_url":MASK,"mode":"standard","seed":S}` | N calls with seed S+k (0-999); result is `image` |
| 10 | `fal-ai/nano-banana-pro/edit` | Reference edit (no mask), highest semantic quality. | `{"image_urls":[IMG, MASK_OR_OVERLAY],"prompt":P',"num_images":min(N,4),"resolution":"1K","aspect_ratio":"auto","output_format":"png"}` | single call; resize the result to WxH |
| 11 | `blackforestlabs/flux-3/edit-image` | Newest BFL reference edit, local edits that "don't change the rest". | `{"image_urls":[IMG],"prompt":P',"resolution":"1k","aspect_ratio":"auto","output_format":"png"}` | N parallel calls (no seed or N) |
| 12 | `bytedance/seedream/v5/pro/edit` | Region-precise reference edit, up to 6 per call. | `{"image_urls":[IMG],"prompt":P',"num_images":min(N,6),"image_size":"auto_1K","output_format":"png"}` | single call |

Defaults:
- **Prompt given:** `fal-ai/flux-pro/v1/fill`.
- **Empty prompt:** `fal-ai/flux-pro/v1/erase`. If the user wants N>1 distinct removal variants, use
  `fal-ai/onereward` (no prompt, seeds).
- For mask models that **require** a prompt (flux-pro fill, ideogram, qwen inpaint, bria genfill, gpt-image) and get an
  empty one, substitute a removal prompt such as: `"empty background, seamlessly continue the surrounding scene and
  texture, no objects, no people, no text"`.
- Fallback for an empty prompt: `fal-ai/bria/eraser` (`{"image_url":IMG,"mask_url":MASK,"mask_type":"manual"}`,
  returns `image`).

Implementation checklist:
1. Read the result as `images` or fall back to `[image]`. Download each `url` (handle `data:` too) and resize to the
   crop's W x H.
2. Mask: same dimensions as the image; opaque binary PNG; white = fill. **Invert it for `ideogram/v4.5/edit`.** Note
   that the mask field name varies: `mask_url` / `mask_image_url` (`fal-ai/lama`, `fal-ai/z-image/turbo/inpaint`,
   `fal-ai/gpt-image-1.5/edit`, `fal-ai/fooocus/inpaint`).
3. The image field also varies: `image_url` (mask models, kontext, qwen-image-edit) vs `image_urls` (list; gpt-image,
   nano-banana, seedream, flux-2, flux-3, qwen-edit-plus/2511/2/3). The prompt field is `instruction` on
   `bria/genfill/v2` and `bria/fibo-edit*`.
4. When `num_images` exceeds the endpoint maximum, or the endpoint has no `num_images`, submit several requests in
   parallel with distinct seeds. Remember the account concurrency limit (2 on new accounts).
5. Keep a table per endpoint: `{image_key, image_is_list, mask_key, mask_invert, prompt_key, prompt_required,
   n_key, n_max, seed_key, size_mode, extra_defaults}`. This catalogue maps directly onto it.
6. Things to confirm with a real key: data-URI acceptance on partner endpoints; which gpt-image mask field name and
   polarity actually works (`mask_url` vs `mask_image_url`; white vs alpha); status HTTP code while IN_QUEUE.
