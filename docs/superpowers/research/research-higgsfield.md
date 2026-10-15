# Higgsfield MCP as a backend for GenFill: research notes

Date: 2026-10-15. Server build header: `x-higgsfield-build: 42a2911`, `x-higgsfield-env: prod`.
Everything below was observed live unless marked UNVERIFIED.

## TL;DR for the implementer

- Endpoint: `POST https://mcp.higgsfield.ai/mcp` (Streamable HTTP MCP). Requests without a token get `401` plus a `WWW-Authenticate` header that points to the RFC 9728 metadata.
- Auth: OAuth 2.1 authorization code + PKCE S256, using a **public client** from Dynamic Client Registration (`token_endpoint_auth_method: "none"`). DCR **accepts loopback redirect URIs** such as `http://127.0.0.1:<port>/callback`. Request scope `openid email offline_access` (offline_access gives you a refresh token). Send `resource=https://mcp.higgsfield.ai/mcp`.
- Device code flow is **not available** to DCR clients: the device grant is stripped at registration.
- Upload: `media_upload` returns a presigned S3 PUT URL. PUT the bytes with `Content-Type` set to exactly the returned `content_type`. Then call `media_confirm` with `type: "image"`.
- Generate: `generate_image_batch` (headless, no widget) or `generate_image`, with `params.model`, `params.prompt`, `params.medias=[{"role":"image_references","value":<media_id>}]` and `use_unlim:false`. Then poll `jobs_wait` until `all_terminal`. Each job's `result_url` is a **public, unauthenticated CloudFront PNG** that you download with plain HTTP GET.
- **Mask inpainting does not work today.** The catalog lists a `mask` role for `nano_banana_2`, `nano_banana_2_1`, `nano_banana_2_lite` and `ideogram_4_5`, but every submit and every cost estimate that uses it fails with `Unknown media role: mask`. No credits are charged when that happens. Use reference-image editing and composite through your own mask, as GenFill already plans to.
- Recommended default: `gpt_image_2_5` with `quality:"low"` and `resolution:"1k"` costs **0.25 credits per image**, follows the prompt well and keeps the geometry (live test below). The output size is **not** the input size: a 4:3 request at 1k returned 1168x880, so always resize the result back to the crop size.

---

## 1. Transport and auth (probed with curl)

### 1.1 Unauthenticated MCP request

```
POST https://mcp.higgsfield.ai/mcp
Content-Type: application/json
Accept: application/json, text/event-stream
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}

-> HTTP/2 401
content-type: application/json; charset=utf-8
www-authenticate: Bearer resource_metadata="https://mcp.higgsfield.ai/.well-known/oauth-protected-resource/mcp", scope="openid email offline_access"
{"error":"Unauthorized"}
```

`GET /mcp` with `Accept: text/event-stream` returns the same 401.
The traffic passes through Cloudflare, which sets a `__cf_bm` cookie. You can ignore it, but a cookie jar does no harm.

### 1.2 Protected resource metadata

`GET https://mcp.higgsfield.ai/.well-known/oauth-protected-resource/mcp` and the same path without `/mcp` return the same document:

```json
{"resource":"https://mcp.higgsfield.ai/mcp",
 "authorization_servers":["https://clerk.higgsfield.ai","https://fnf-device-auth.higgsfield.ai"],
 "scopes_supported":["openid","email","offline_access"],
 "bearer_methods_supported":["header"],
 "higgsfield_auth_hints":{"selection":"client_capability_based","options":[
   {"flow":"authorization_code_pkce","authorization_server":"https://clerk.higgsfield.ai",
    "potential_clients":["anthropic","claude","claude-ai","claude-code"],
    "requires":["authorization_endpoint","token_endpoint","redirect_uri_receiver","pkce"]},
   {"flow":"device_code","authorization_server":"https://fnf-device-auth.higgsfield.ai",
    "potential_clients":["openclaw","hermes","memoclaw"],
    "requires":["device_authorization_endpoint","token_polling"]}]}}
```

### 1.3 Authorization server metadata (use this one)

`GET https://mcp.higgsfield.ai/.well-known/oauth-authorization-server` returns the same document as `/.well-known/openid-configuration`:

```json
{"issuer":"https://mcp.higgsfield.ai",
 "authorization_endpoint":"https://mcp.higgsfield.ai/oauth2/authorize",
 "token_endpoint":"https://mcp.higgsfield.ai/oauth2/token",
 "registration_endpoint":"https://mcp.higgsfield.ai/oauth2/register",
 "client_id_metadata_document_supported":true,
 "response_types_supported":["code"],
 "response_modes_supported":["form_post","query"],
 "grant_types_supported":["authorization_code","refresh_token","urn:ietf:params:oauth:grant-type:device_code"],
 "token_endpoint_auth_methods_supported":["client_secret_basic","none","client_secret_post"],
 "code_challenge_methods_supported":["S256"],
 "scopes_supported":["openid","email","offline_access"]}
```

`mcp.higgsfield.ai/oauth2/authorize` is a thin proxy. It answers `302` to `https://clerk.higgsfield.ai/oauth/authorize?...` with the same parameters plus `&surface=custom_mcp`. The DCR `client_id` is a Clerk OAuth client (16-character id). Use the `mcp.higgsfield.ai` endpoints throughout, as the metadata says.

`clerk.higgsfield.ai/.well-known/oauth-authorization-server` also exists. It lists `/oauth/token`, `/oauth/token/revoke` (revocation), `/oauth/device_authorization` and more scopes. `fnf-device-auth.higgsfield.ai/.well-known/oauth-authorization-server` returns 404.

### 1.4 Dynamic Client Registration (live, harmless)

```
POST https://mcp.higgsfield.ai/oauth2/register
Content-Type: application/json
{"client_name":"GenFill Nuke probe","redirect_uris":["http://127.0.0.1:53682/callback"],
 "grant_types":["authorization_code","refresh_token"],"response_types":["code"],
 "token_endpoint_auth_method":"none","scope":"openid email offline_access"}

-> HTTP/2 201
{"client_id":"<redacted>","client_id_issued_at":1792023100481,"client_secret_expires_at":0,
 "client_name":"GenFill Nuke probe","redirect_uris":["http://127.0.0.1:53682/callback"],
 "grant_types":["authorization_code","refresh_token"],"response_types":["code"],
 "scope":"openid email offline_access","token_endpoint_auth_method":"none","application_type":"web"}
```

- The loopback redirect `http://127.0.0.1:<port>/callback` is accepted, and so is `http://localhost/callback`. The server returns no `client_secret`.
- Asking for the `urn:ietf:params:oauth:grant-type:device_code` grant at registration does nothing: the server drops it silently. A later `POST clerk.higgsfield.ai/oauth/device_authorization` then fails with `invalid_grant ... does not have the 'urn:ietf:params:oauth:grant-type:device_code' grant`. **Device flow is not an option for GenFill.**
- Redirect URI port matching: the authorize step answered 302 even with an unregistered port (9999), so validation happens later, at Clerk's consent or callback, or at the token step. UNVERIFIED whether Clerk allows a variable loopback port as RFC 8252 permits. **Safe approach:** pick the port first, register a client with that exact redirect URI, and store `client_id` + `redirect_uri` + port together. If the port is taken next time, re-register; DCR is cheap. Alternatively always try the same fixed port first.

### 1.5 Flow GenFill should implement (stdlib only)

1. Discovery (optional; you can hard-code the values): GET the two metadata URLs above.
2. If no `client_id` is stored, call DCR as in 1.4. Persist the `client_id`.
3. PKCE: `verifier = base64url(os.urandom(32))` without padding; `challenge = base64url(sha256(verifier))`.
4. Open the browser on
   `https://mcp.higgsfield.ai/oauth2/authorize?response_type=code&client_id=<id>&redirect_uri=http%3A%2F%2F127.0.0.1%3A<port>%2Fcallback&scope=openid%20email%20offline_access&state=<rand>&code_challenge=<challenge>&code_challenge_method=S256&resource=https%3A%2F%2Fmcp.higgsfield.ai%2Fmcp`
   with a one-shot `http.server` on 127.0.0.1:<port> that reads `code` and `state` from the query.
5. Token exchange: `POST https://mcp.higgsfield.ai/oauth2/token`, `Content-Type: application/x-www-form-urlencoded`, body
   `grant_type=authorization_code&code=..&redirect_uri=..&client_id=..&code_verifier=..&resource=https://mcp.higgsfield.ai/mcp`.
   A bad code returns `{"error":"invalid_grant","error_description":"..."}`; the endpoint works without client authentication. The success response shape was not captured (that needs an interactive login). Expect the standard fields `access_token`, `token_type`, `expires_in`, `refresh_token` (because of `offline_access`), possibly `id_token` and `scope`.
6. Refresh: `grant_type=refresh_token&refresh_token=..&client_id=..` to the same token endpoint. Refresh tokens may rotate, so always store the new one if the response contains it.
7. Every MCP request carries `Authorization: Bearer <access_token>`. On 401, refresh once and retry. If that fails, log in again.

### 1.6 MCP transport details: UNVERIFIED (needs a token)

Without a token I could not see whether the server answers `initialize` and `tools/call` with `application/json` or `text/event-stream`, or whether it returns `Mcp-Session-Id`. Build the client defensively, as the Streamable HTTP spec requires:

- Always send `Accept: application/json, text/event-stream` and `Content-Type: application/json`, plus `MCP-Protocol-Version: <negotiated>` after initialize.
- If the response `Content-Type` is `text/event-stream`, read lines, collect the `data:` payloads per event and pick the JSON-RPC message whose `id` matches. Otherwise parse the body as JSON.
- If `initialize` returns an `Mcp-Session-Id` header, echo it on every later request. If a request returns 404 with a session id, re-initialize.
- After `initialize`, send the notification `{"jsonrpc":"2.0","method":"notifications/initialized"}` (expect 202).
- Call tools with `{"jsonrpc":"2.0","id":N,"method":"tools/call","params":{"name":"generate_image_batch","arguments":{...}}}`. Tool names are the bare names without the `mcp__claude_ai_Higgsfield__` prefix that Claude adds.
- Tool results come back as `result.content[]`. In Claude every tool returned one `text` item containing JSON, so parse `content[i].text` with `json.loads` and also check `result.structuredContent` if present. `job_display` additionally returned a `resource_link` item (`[Resource link: <id>.png] <url>`). Errors come as `isError:true` with plain text such as `Invalid request: ...` or `Error estimating cost: ...`, so do not assume the text is JSON.
- Cloudflare is in front, so use a normal `User-Agent` and keep timeouts at 30 s or more.

---

## 2. Tools (exact schemas as exposed)

Notes on widgets and UI: many tool descriptions talk about a "generation widget" or "gallery widget" and say things like "call media_upload_widget as the only tool in that turn" or "display with show_generation_by_ids". These target chat UIs (MCP Apps). A headless client should ignore them. Use `generate_image_batch` + `jobs_wait` and skip `show_generation_by_ids` / `job_display` (they are display-only, although `job_display` does return useful JSON, see 2.6). The server instructions also mention calling `get_preferences` before the first generation (auto-project creation). This is optional: omitting `folder_id` worked fine.

### 2.1 `media_upload`

Input:
```json
{"filename":"plate.png","content_type":"image/png"}          // single
{"files":[{"filename":"plate.png","content_type":"image/png"}, ...]}  // 1-20
```
Optional: `method:"upload_url"` (the only value), `surface:"mcp"`. The media type is inferred from the extension (png/jpg/webp... means image).

Output (trimmed):
```json
{"uploads":[{
  "upload_url":"https://upload.higgsfield.ai/user_<uid>/<media_id>.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&...&X-Amz-Expires=86400&X-Amz-SignedHeaders=content-type%3Bhost%3Bif-none-match&...&X-Amz-Signature=…",
  "media_id":"e66702ad-032e-4a8d-8328-d6488f67c6d0",
  "url":"https://d2ol7oe51mr4n9.cloudfront.net/user_<uid>/<media_id>.png",
  "expires_in_seconds":86400,
  "method":"PUT",
  "content_type":"image/png",
  "instructions":"Upload the file using: curl -X PUT -H \"Content-Type: image/png\" --data-binary @plate.png '<upload_url>' ... then call media_confirm with type \"image\" and media_id ..."}]}
```

PUT details (tested):
- `PUT <upload_url>` with header `Content-Type: <content_type>` exactly as returned, body = raw bytes. No auth header. Returns 200 with an empty body.
- `if-none-match` appears in `SignedHeaders`, yet the PUT **without** an `If-None-Match` header also succeeded (200). With `If-None-Match: *` the first PUT returns 200, and a **second** PUT to the same URL returns `412 PreconditionFailed`. This is write-once protection. Sending `If-None-Match: *` is optional; recommended, with 412 treated as "already uploaded".
- `url` (CloudFront) is publicly readable after upload (GET returned 200 `image/png`).
- In Python: `urllib.request.Request(upload_url, data=bytes, method="PUT", headers={"Content-Type": ct})`. Do not let urllib re-encode the query string; pass the URL unchanged.

### 2.2 `media_confirm`

Input: `{"type":"image","media_id":"<id>"}` or `{"type":"image","media_ids":["<id>", ...]}` (1-20). `type` is required (`image|video|audio|file`).
Output: `{"results":[{"media_id":"e667...","status":"uploaded"},{"media_id":"91e0...","status":"uploaded"}]}`
Call it only after every PUT returned 200.

(`media_import_url` can turn a public HTTPS URL into a confirmed `media_id` in one call: `{"url":"https://...","type":"image"}`. That is irrelevant for GenFill because the plates are local.)

### 2.3 `generate_image`

Input: `{"params": {...}, "context": "<optional analytics sentence, max 500 chars>"}`. `params` can also be a string, but use the object.

`params` fields:
| field | notes |
|---|---|
| `model` | required, model id from `models_explore` |
| `prompt` | string |
| `medias` | `[{"role": "<role>", "value": "<media_id or prior job_id>"}]`. Never pass URLs here. |
| `aspect_ratio` | string from the model's `aspect_ratios` list |
| `count` | 1-4 variants of the same request in **one** call (one job per variant). Capped to 1 with `use_unlim:true`. |
| `get_cost` | `true` returns `{"cost":{"credits":X,"credits_exact":X}}` and submits nothing |
| `use_unlim` | **always send `false`**. If omitted and the user has an unlimited allowance, the server submits nothing and returns an `unlim_choice` question. |
| `folder_id` | optional UUID (project folder); omit it |
| `use_free_gens`, `preset_session_id` | not relevant |
| model params | **top level inside `params`** (e.g. `quality`, `resolution`, `seed`, `is_inpaint`, `variant`) |

Server behaviour seen:
- Role names are validated: `Invalid request: media role "masks" not supported by Nano Banana 2 Lite. Allowed: [image_references, mask]`.
- Roles may be auto-coerced. Example: role `mask_image` came back as `"adjustments":{"medias[1].role":{"requested":"mask_image","used":"image_references","reason":"Nano Banana 2 Lite backend expects schema-key media roles"}}`. That silently turns the mask into a second reference image, so **do not rely on coercion**.
- Check for an `adjustments` object in responses (count caps, role coercions).
- If a response contains `recovery_tool`, the description says to call it. Log this as an error in GenFill instead.
- On a transport timeout the description says **do not resubmit automatically**; the job may exist.

Live response (submit, gpt_image_2_5):
```json
{"results":[{"id":"54aea846-e5f2-47bf-a8b6-980efee18d38","type":"image","status":"pending","model":"gpt_image_2_5",
  "params":{"prompt":"Remove the red square completely; ...","aspect_ratio":"1:1",
            "reference_images":["e66702ad-032e-4a8d-8328-d6488f67c6d0"],"quality":"low","resolution":"1k"}}]}
```
With `count: N`, expect N entries in `results[]` (UNVERIFIED for N>1; budget limit). `results[].id` is the job_id.

### 2.4 `generate_image_batch` (preferred for headless use)

Input: `{"requests":[{"index":0,"params":{...same as above, count must be 1, no get_cost...}}, ...], "context":"..."}`. Each call takes 1-12 requests, one job per request.
Use it for N variations by sending N requests with identical params, or use `generate_image` with `count` up to 4.

Live success:
```json
{"jobs":[{"index":0,"job_id":"18b0bd65-c1f8-44f0-831f-62ea61b21d1d","status":"pending"}],"submitted_count":1,"failed_count":0}
```
Live failure (mask role) came back as an `isError` text result, not JSON:
```
Submitted 0/1 image generations.
- index 0: submission_failed — Unknown media role: mask
Wait for the complete requested set with jobs_wait, then show it together with show_generation_by_ids ...
```
A partial failure is possible. Keep the submitted job ids and do not blindly retry the whole batch.

### 2.5 `jobs_wait`

Input: `{"jobs":[{"index":0,"job_id":"<uuid>"}, ...], "timeout_seconds":15}`, with 1-12 jobs and a timeout of 0-15 s (max 15; 0 returns an immediate snapshot).
Output (live):
```json
{"jobs":[{"index":0,"job_id":"54aea846-e5f2-47bf-a8b6-980efee18d38","status":"completed","type":"image","model":"gpt_image_2_5",
          "result_url":"https://d8j0ntlcm91z4.cloudfront.net/user_<uid>/hf_20261015_001350_54aea846-e5f2-47bf-a8b6-980efee18d38.png"}],
 "summary":{"total":1,"completed":1,"failed":0,"active":0,"errors":0},
 "all_terminal":true}
```
Loop until `all_terminal == true`. When it is false, the description says to wait `poll_after_seconds` (a field then present in the response) before the next call. Both test jobs (gpt_image_2_5 low/1k) were already complete within the first 15 s wait, about 10-20 s after submit. Terminal statuses include at least `completed` and `failed`. Expect others such as `nsfw`/`canceled`; treat anything except `completed` as no result for that index.

### 2.6 `job_display` / `show_generation_by_ids`

Both are display tools for the chat UI. You do not need them: `jobs_wait.result_url` is sufficient. For reference, `job_display {"id":"<job_id>"}` returned a `resource_link` plus JSON with richer metadata:
```json
{"results":[{"id":"54aea846-...","type":"image","status":"completed","model":"gpt_image_2_5",
  "params":{"prompt":"...","aspect_ratio":"1:1",
            "medias":[{"role":"image","data":{"id":"e66702ad-...","type":"media_input","url":"https://d2ol7oe51mr4n9.cloudfront.net/.../e66702ad-....png"}}],
            "width":1024,"height":1024,"quality":"low","resolution":"1k","model":"flare","remove_bg":false,"reference_elements":[]},
  "results":{"rawUrl":"https://d8j0ntlcm91z4.cloudfront.net/.../hf_20261015_001350_54aea846-....png",
             "minUrl":"https://d8j0ntlcm91z4.cloudfront.net/.../hf_..._min.webp"},
  "createdAt":1792023230.824205,"liked":false}]}
```
`results.rawUrl` is the full-resolution PNG and equals `jobs_wait.result_url`; `minUrl` is a webp thumbnail. `show_generation_by_ids` takes `{"jobs":[{"index":i,"job_id":...}]}` (max 60) and only renders a widget.

**Result URLs are public**: a plain GET without auth returned `200`, `content-type: image/png`, `cache-control: public, max-age=31536000, immutable`. Anyone with the URL can read them; they are unguessable (user id + job uuid).

### 2.7 `balance`

`{}` returns `{"credits":<n>,"subscription_plan_type":"creator"}`. Useful for showing the user's credits in the GenFill panel.

### 2.8 `models_explore`

Input: `{"action":"list"|"search"|"get"|"recommend", "type":"image", "input":"image", "model_id":"...", "query":"...", "limit":100, "after":"<cursor>", "unlim":bool}`.
Output items have the shape `{id,name,provider_name,description,output_type,parameters:[{name,required,type,description,default,options|min|max}],medias:[{name,type,max?,roles:[...]}],aspect_ratios:[...],tags,supports_unlim?}` plus a top-level `has_more`, `next_page_token` and `unlim`.
`action:"search"` with query "inpaint mask object removal erase" returned **no** items. `apps_search` for "inpaint" and "remov" also returned no apps. **There is no dedicated inpaint tool or app.** Related tools that do not fit GenFill: `outpaint_image`, `remove_background`, `upscale_image`, `reframe`.

### 2.9 Image edit-capable models (`list`, type image, input image; full catalog has_more=false)

Costs are from `get_cost:true` with one reference image. **The cost estimate ignores `count`**: it returned the same number for count 1 and 4, so treat it as per-image and multiply by N yourself. None of these has a custom width/height output (exceptions noted); the output size is set by `aspect_ratio` + `resolution`.

| model id | roles | mask role? | key params | aspect ratios | seed | cost (credits) |
|---|---|---|---|---|---|---|
| **gpt_image_2_5** | image_references | no | variant flare/sunburst (default flare), quality low/medium/high/xhigh/max (default low), resolution 1k/2k/4k, background auto/opaque/transparent | auto,1:1,3:2,2:3,4:3,3:4,16:9,9:16,21:9,27:16,16:27,9:8,8:9,4:5,5:4 | no | **0.25** (low/1k), live-verified |
| nano_banana_2 | image_references, **mask** | listed but broken | resolution 1k/2k/4k, is_inpaint bool | auto,1:1,3:2,2:3,4:3,3:4,4:5,5:4,9:16,16:9,21:9 | no | 1.5 (1k) |
| nano_banana_2_1 | image_references, **mask**, video_references | listed but broken | resolution, is_inpaint, thinking_level minimal/medium/high, enable_web_search, enable_image_search, **seed** 0..2147483647 | + 4:1,1:4,8:1,1:8 | **yes** | 1.5 (1k) |
| nano_banana_2_lite | image_references, **mask** | listed but broken | resolution 1k only, thinking MINIMAL/HIGH, is_inpaint | auto,1:1,3:2,...,21:9 | no | 1.0 |
| ideogram_4_5 | image_references, **mask** | listed but broken | resolution 1k/2k ("Masked edits use source geometry"), quality very_low/low/medium/high (default high), magic_prompt auto/on/off, **seed**, **batch_size 1-4** | auto + many (1:3..3:1) | **yes** | 1.0 (low or very_low, 1k) |
| nano_banana / nano_banana_pro | image_references | no | pro: resolution 1k/2k/4k | 1:1..21:9 | no | not priced |
| seedream_v5_pro | image_references | no | resolution 1k/1.5k/2k, width/height ("stored with the generation"), is_inpaint, remove_bg | 1:1,4:3,3:4,16:9,9:16,3:2,2:3,21:9 | no | not priced |
| seedream_5_0_flash | image_references | no | resolution 1k/1.5k/2k, width/height "stored in metadata; not sent to provider" | auto,... | no | not priced |
| seedream_v4_5, seedream_v5_lite | image_references | no | quality basic/high | fixed list | no | not priced |
| flux_kontext | image_references | no | none | 1:1,4:3,3:4,16:9,9:16 | no | not priced |
| flux_2 | image_references | no | resolution 1k/2k, variant pro/flex/max | 1:1,4:3,3:4,16:9,9:16 | no | not priced |
| flux_3_image | image_references (up to 10) | no | resolution 768sq/1k/1.5k/2k/4k, batch_size 1-4 | auto,21:9..1:2 | no | not priced |
| gpt_image_2 | image_references | no | resolution 1k/2k/4k, quality low/medium/high | 1:1,4:3,... | no | not priced |
| openai_hazel | image_references | no | quality | 1:1,3:2,2:3,auto | no | not priced |
| kling_omni_image, grok_image, grok_image_2_0, image_auto | image_references | no | resolution / mode | various | no | not priced |

Not relevant: soul_*, cinematic_*, marketing_*, ms_image, autosprite, topaz_*, image_decompose, upscalers, outpaint, flux_2_pro_outpaint (outpaint only, per-side pixel expansion).

**Mask finding (important):** every attempt that used `{"role":"mask","value":<confirmed image media_id>}` failed, on all four mask-listed models, both for `get_cost` and for the real submit (`Error estimating cost: Unknown media role: mask`, `submission_failed — Unknown media role: mask`). `is_inpaint` was true or unset in these attempts. No credits were charged. This is a server-side bug or a role that is not wired up yet. GenFill could feature-detect it at runtime with a `get_cost:true` call using the mask role (free), and use the mask once that stops erroring. Until then, use the reference-only edit plus GenFill's own mask composite.

Prompting for removal (empty GenFill prompt): build an instruction prompt, e.g. "Remove the <object> inside the marked area; continue the surrounding background seamlessly. Keep everything else identical." The model never sees the mask, so the region must be described in text. One option is to tint or outline the masked area in the uploaded reference ("remove the object inside the red outline") and composite through the true mask afterwards. That is untested, but common practice with edit models.

---

## 3. Live end-to-end test

Balance: **<n>** before, **144.22** after test 1, **<n>** after test 2. That is two paid generations at 0.25 credits each. The failed mask submits charged nothing.

### Test 1: 512x512, 1:1
- Created `scratchpad/hf/test_plate.png` (512x512 RGB: sky (120,170,230), grass (90,140,60) below y=360, red square 192..320) and `test_mask.png` (white 176..336).
- `media_upload` with `files[2]`, then PUT (plate without If-None-Match, mask with `If-None-Match: *`; both 200), then `media_confirm` returned `status:"uploaded"` for both.
- `generate_image` `{"model":"gpt_image_2_5","prompt":"Remove the red square completely; continue the blue sky and green grass seamlessly where it was. Keep everything else identical.","medias":[{"role":"image_references","value":"e667..."}],"quality":"low","resolution":"1k","aspect_ratio":"1:1","count":1,"use_unlim":false}` returned job `54aea846-...`, status pending.
- `jobs_wait` (15 s) returned completed with `result_url`.
- Download: HTTP 200, `image/png`, 840,697 bytes, **PNG 1024x1024 8-bit RGB** (no alpha). The square was removed cleanly. Sky came back as (114,168,232) against the original (120,170,230), a slight colour drift, which the mask composite hides. Horizon at y=720/1024 = 0.703, the same relative position as the source 360/512, so the geometry is preserved under uniform scaling.

### Test 2: 1024x768, 4:3 (the GenFill-typical case)
- Uploaded `plate_1024x768.png` (horizon y=540, red square). A second PUT with `If-None-Match: *` to the same URL gave 412 (see 2.1).
- `generate_image_batch` with gpt_image_2_5 low/1k `aspect_ratio:"4:3"` returned job `18b0bd65-...`; `jobs_wait` reported completed on the first call.
- Result: **PNG 1168x880 RGB** (ratio 1.327, not exactly 4:3, and not 1024x768), 838,237 bytes. Horizon at 619/880 = 0.7034 against the source 541/768 = 0.7044. The content is aligned with a uniform scale, so **resizing to the crop size gives correct registration**.

### Implications for GenFill
1. Map the crop aspect to the nearest supported `aspect_ratio` string. For off-ratio crops, pad the crop to the chosen ratio before upload and crop the result back, or use `"auto"` where available (UNVERIFIED whether `auto` matches the input ratio exactly).
2. Always resample the result to the padded crop size, then un-pad, then composite through the mask.
3. Results are 8-bit sRGB PNG. Convert to linear for Nuke and treat the output as display-referred.
4. N variations: `generate_image_batch` with N identical requests (max 12 per call), or `generate_image` with `count:N` (max 4). `gpt_image_2_5` has no seed, so the variations come from sampling. If you need reproducibility, `ideogram_4_5` and `nano_banana_2_1` accept `seed`.
5. Cost per variation: 0.25 credits (gpt_image_2_5 low/1k); about 1 credit (ideogram low, nano lite); 1.5 (nano_banana_2 / 2_1 at 1k).

## 4. Minimal call sequence (copy-paste plan)

```
tools/call media_upload       {"files":[{"filename":"plate.png","content_type":"image/png"}]}
HTTP PUT upload_url           Content-Type: image/png [If-None-Match: *]  -> 200
tools/call media_confirm      {"type":"image","media_ids":["<media_id>"]}
tools/call generate_image_batch {"requests":[{"index":i,"params":{"model":"gpt_image_2_5","prompt":P,
                                 "medias":[{"role":"image_references","value":"<media_id>"}],
                                 "quality":"low","resolution":"1k","aspect_ratio":"4:3","use_unlim":false}} for i in range(N)]}
loop: tools/call jobs_wait    {"jobs":[{"index":i,"job_id":...}],"timeout_seconds":15}  until all_terminal
HTTP GET result_url           (public, no auth) -> PNG bytes
optional: tools/call balance  {}
```

## 5. Open items / unverified
- MCP response content type (JSON vs SSE) and whether `Mcp-Session-Id` is used: needs an authenticated request.
- Exact token response fields, access-token lifetime, and refresh-token rotation behaviour.
- Whether Clerk enforces the exact loopback port at the callback or token step.
- `count>1` response shape (expected `results[]` with N entries).
- When the `mask` role will work. Re-test with `get_cost:true`, which is free.
- Leftover test DCR clients (`wxUokeLYfZsld4mf`, `FoiqFl24aXymarV5`, `Na1kWgmHFGALIr41`) are unused public clients with no tokens; harmless.
