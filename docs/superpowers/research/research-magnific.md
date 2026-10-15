# Magnific remote MCP server: research for GenFill (own MCP client)

Probed live on 2026-10-15. Everything marked **[verified]** was observed directly (curl or a real tool call).
Everything marked **[inferred]** is a reasonable assumption the implementer should handle defensively.

---

## 1. Transport

- Endpoint: `https://mcp.magnific.com` (path `/`). `/mcp` also answers identically (same 401), but the
  protected-resource metadata says `"resource": "https://mcp.magnific.com"`, so use `/`. **[verified]**
- Streamable HTTP transport (per docs.magnific.com/modelcontextprotocol). `POST` only:
  `GET /` and `GET /mcp` (even with `Accept: text/event-stream`) return `405 allow: POST`, so there is
  no standalone SSE stream for server-initiated messages. You don't need one. **[verified]**
- Send headers on every POST: `Content-Type: application/json`,
  `Accept: application/json, text/event-stream`, `Authorization: Bearer <access_token>`.
  The response may be plain JSON or an SSE body (`data: {...}` lines). Handle both. **[inferred: standard streamable HTTP]**
- Standard handshake: `initialize` → read `Mcp-Session-Id` response header if present and echo it on all later
  requests → POST `notifications/initialized` → `tools/call`. (Couldn't observe the authenticated envelope
  myself, because this session's MCP connector hides raw JSON-RPC.) **[inferred]**
- CORS: `access-control-allow-headers: Authorization, Content-Type, X-Pikaso-Client`. **[verified]**
- Unauthenticated response (POST `/` without a token):
  ```
  HTTP/2 401
  www-authenticate: Bearer error="invalid_token", resource_metadata="https://mcp.magnific.com/.well-known/oauth-protected-resource", scope="openid profile email mcp:custom-audience"
  {"message":"Unauthenticated."}
  ```

## 2. Auth (OAuth 2.1, Keycloak). No API key.

- **API-key headers are NOT accepted.** `x-magnific-api-key: …` and `x-freepik-api-key: …` both give the same
  401 Bearer challenge. The docs say: "You don't need to manage an API key… uses OAuth 2.0 with your Magnific
  account." You have to use OAuth. **[verified]**
- Protected resource metadata `GET https://mcp.magnific.com/.well-known/oauth-protected-resource` **[verified]**:
  ```json
  {"resource":"https://mcp.magnific.com",
   "authorization_servers":["https://auth.magnific.com/realms/mcp"],
   "jwks_uri":"https://auth.magnific.com/realms/mcp/protocol/openid-connect/certs",
   "scopes_supported":["openid","profile","email","mcp:custom-audience"],
   "bearer_methods_supported":["header"]}
  ```
  (`/.well-known/oauth-protected-resource/mcp` → 404.)
- AS metadata: `https://auth.magnific.com/realms/mcp/.well-known/openid-configuration` (200), also
  `…/realms/mcp/.well-known/oauth-authorization-server` (200),
  `https://auth.magnific.com/.well-known/oauth-authorization-server/realms/mcp` (200), and
  `https://mcp.magnific.com/.well-known/oauth-authorization-server` (200, same issuer). **[verified]**

| Field | Value |
|---|---|
| issuer | `https://auth.magnific.com/realms/mcp` |
| authorization_endpoint | `https://auth.magnific.com/realms/mcp/protocol/openid-connect/auth` |
| token_endpoint | `https://auth.magnific.com/realms/mcp/protocol/openid-connect/token` |
| registration_endpoint (DCR) | `https://auth.magnific.com/realms/mcp/clients-registrations/openid-connect` |
| revocation_endpoint | `https://auth.magnific.com/realms/mcp/protocol/openid-connect/revoke` |
| device_authorization_endpoint | `…/protocol/openid-connect/auth/device` (but disabled for DCR clients, see below) |
| scopes_supported | `profile openid mcp:custom-audience email basic offline_access` |
| code_challenge_methods_supported | `plain`, `S256` → use **S256** |
| token_endpoint_auth_methods_supported | includes `none` (public client) |
| grant_types | includes `authorization_code`, `refresh_token` |

### Dynamic Client Registration: works, anonymous, loopback OK **[verified]**
Request:
```http
POST https://auth.magnific.com/realms/mcp/clients-registrations/openid-connect
Content-Type: application/json

{"client_name":"GenFill","redirect_uris":["http://127.0.0.1:53682/callback"],
 "token_endpoint_auth_method":"none","grant_types":["authorization_code","refresh_token"],
 "response_types":["code"],"scope":"openid profile email mcp:custom-audience offline_access"}
```
Response `201 Created` (trimmed):
```json
{"redirect_uris":["http://127.0.0.1/callback"],
 "token_endpoint_auth_method":"none",
 "grant_types":["authorization_code","refresh_token"],"response_types":["code"],
 "client_id":"<redacted>","client_name":"GenFill probe",
 "scope":"mcp:custom-audience openid offline_access profile email",
 "registration_client_uri":"https://auth.magnific.com/realms/mcp/clients-registrations/openid-connect/<client_id>",
 "registration_access_token":"eyJ…", "client_id_issued_at":1792023095, ...}
```
Notes:
- **Keycloak strips the port from the stored redirect URI** (`http://127.0.0.1/callback`). It still applies
  RFC 8252 loopback matching: an auth request with `redirect_uri=http://127.0.0.1:53682/callback` got a **302**
  to the Magnific login page, so you can bind any free port at runtime and register once. **[verified]**
- `http://localhost:53682/callback` was rejected with **400 "Invalid parameter: redirect_uri"**. Use the literal
  `127.0.0.1`. A different path (`/other`) is also rejected, so the path must match exactly. **[verified]**
- The device flow is disabled for DCR clients:
  `{"error":"unauthorized_client","error_description":"Client is not allowed to initiate OAuth 2.0 Device Authorization Grant…"}`.
  **[verified]** That leaves loopback auth-code + PKCE.
- Register once and persist `client_id` (plus `registration_access_token`, if you want to delete it later with
  `DELETE registration_client_uri` and `Authorization: Bearer <registration_access_token>` → 204, verified).
  The test client I created was deleted.

### Authorization request
```
GET https://auth.magnific.com/realms/mcp/protocol/openid-connect/auth
  ?client_id=<id>&response_type=code
  &redirect_uri=http://127.0.0.1:<port>/callback
  &scope=openid profile email mcp:custom-audience offline_access
  &state=<rand>&code_challenge=<S256>&code_challenge_method=S256
  &resource=https://mcp.magnific.com
```
It 302s to `https://www.magnific.com/log-in?client_id=magnific&…&cb_url=…`, which is the normal Magnific web login
(open it in the system browser). **[verified]** Then exchange the code at token_endpoint with form fields
`grant_type=authorization_code, code, redirect_uri (same, with port), client_id, code_verifier`
(no secret). Refresh with `grant_type=refresh_token, refresh_token, client_id`. Request `offline_access`
so the refresh token lives long. Pass `resource` too. **[inferred: standard Keycloak; token lifetimes not
measured]** The scope `mcp:custom-audience` is what puts the MCP audience into the token, so don't drop it.

## 3. Tool output format (important for parsing)

Tool results come back as **text content**, and the format **varies per tool**. **[verified]**
- JSON text: `creations_request_upload`, `creations_finalize_upload`, `images_retouch`, `images_generate`,
  `creations_wait`, `account_balance`, `images_models_list`, `simulate_cost`.
- YAML-like "TOON" text (not JSON!): `creations_get`, `retouch_models_list`
  (e.g. `models[21]:\n  - slug: retouch-auto …`, `url: "https://…"`).

Parser strategy: prefer `result.structuredContent` if the server sends it **[unknown, check]**; else try
`json.loads(content[0].text)`; else regex `^\s*url:\s*"([^"]+)"`. GenFill only needs `creations_wait`
(JSON), so it can avoid `creations_get` entirely.

## 4. Tools for the GenFill flow (exact schemas)

### 4.1 `creations_request_upload`: presigned PUT target(s)
Input: `{"mimeType": "image/png", "count": 2}`. `mimeType` is required; enum includes `image/jpeg`, `image/png`,
`image/webp`, `image/svg+xml` (plus video/audio/glb). `count` is 1–100 and optional.
Raw response **[verified]**:
```json
{"uploads":[
  {"proxyUploadUrl":"https://ak-data.magnific.com/app/api/mcp/uploads/proxy/14053e6e-….png?path=temp-files%2F14053e6e-….png&mime=image%2Fpng&user_id=…&expires=1792026740&v=1&sig=…",
   "path":"temp-files/14053e6e-9349-4df4-8fee-14b968af1e42.png","mimeType":"image/png",
   "expiresAt":"2026-10-15T01:12:20+00:00"},
  {"proxyUploadUrl":"…bb169b62….png?…","path":"temp-files/bb169b62-….png", …}],
 "count":2,"mimeType":"image/png","expiresAt":"2026-10-15T01:12:20+00:00",
 "instructions":"… HTTP PUT the raw binary to `proxyUploadUrl` … Set header `Content-Type: image/png` … If a PUT fails, do not retry the same URL — re-PUTs of one target rate-limit (429); request fresh target(s) here."}
```
- The URL expires after **1 hour**.
- The PUT needs **no Authorization header** (the signature is in the URL). The only header is
  `Content-Type: <mimeType>`. The body is raw bytes. **[verified]**
- PUT response: `200 {"success":true,"path":"temp-files/….png"}`, headers `x-ratelimit-limit: 60`,
  `x-ratelimit-remaining: 59`. **[verified]**
- Don't re-PUT the same URL after a failure (429). Request a new target instead.

### 4.2 `creations_finalize_upload`: temp path(s) to creation id(s)
Input (batch form used):
```json
{"fileName":"genfill_test_plate.png",
 "uploads":[{"path":"temp-files/14053e6e-….png","fileName":"genfill_test_plate.png"},
            {"path":"temp-files/bb169b62-….png","fileName":"genfill_test_mask.png"}],
 "visible": false}
```
Fields: `fileName` (string, **required at top level even in batch mode**), `path` (single), or
`uploads[]` (`{path, fileName}` both required), `visible` (bool, default true; use `false` so throwaway
plates/masks stay out of the user's library), `folderReference` (optional).
Size limits: raster ≤25 MB.
Response **[verified]**:
```json
{"results":[{"identifier":"JNdBG4xOq4","status":"completed"},
            {"identifier":"p8xLCodehw","status":"completed"}],
 "errors":[],"count":2,"errorCount":0}
```
This is synchronous: the ids are usable right away.

### 4.3 `images_retouch`: masked inpaint / erase (the primary GenFill path)
Input schema:
| field | type | notes |
|---|---|---|
| `creationIdentifier` | string, **required** | uploaded plate |
| `maskCreationIdentifier` | string, **required** | "Same pixel size as the image; white = change, black = keep." |
| `mode` | `"replace"` \| `"erase"` | default `replace` |
| `prompt` | string | required for `replace` |
| `model` | string | slug from `retouch_models_list` whose `retouchModes` contains `mode`; omit / `auto` = Auto |
| `resolution` | string | only for models that list `resolutions` |
| `folderReference` | string | optional |

There is **no `count` and no `seed`**. For N variations, make N calls (each returns its own creation id).
Raw response **[verified]**:
```json
{"creation":{"identifier":"YMHb8eKWeC","status":"processing","expectTime":25,"tool":"retouch","credits":30,
  "webUrl":"https://www.magnific.com/app/creation/YMHb8eKWeC?utm_source=mcp&utm_medium=ai_connector"},
 "instruction":"Queued. … use `creations_wait` …"}
```
GenFill mapping: empty prompt → `mode:"erase"`; non-empty → `mode:"replace", prompt`.

### 4.4 `retouch_models_list`: all 21 models **[verified]** (all `requiresPremium: true`)
| slug | name | modes | type | resolutions (default) | ETA s | beta/private |
|---|---|---|---|---|---|---|
| `retouch-auto` | Auto | replace, erase | precise, creative | – | 10 | |
| `retouch-classic` | Classic | replace | precise | – | 10 | |
| `retouch-erase` | Erase | erase | precise | – | 26 | |
| `retouch-pro` | Pro | replace, erase | precise | – | 15 | |
| `retouch-reference` | Ideogram Character | replace | precise | – | 31 | |
| `retouch-lora` | Lora | replace | precise | – | 23 | |
| `retouch-imagen-nano-banana-2-flash` | Nano Banana 2 | replace | creative | 1k,2k,4k (2k) | 35 | |
| `retouch-inpaint-lab-nano-banana-2-flash` | Nano Banana 2 | replace, erase | creative | 1k,2k,4k (2k) | 50 | beta |
| `retouch-imagen-nano-banana-2` | Nano Banana Pro | replace | creative | 2k,4k (2k) | 56 | |
| `retouch-inpaint-lab-nano-banana-pro` | Nano Banana Pro | replace, erase | creative | 2k,4k (2k) | 39 | beta |
| `retouch-inpaint-lab-seedream-5-pro` | Seedream 5 Pro | replace, erase | creative | 1.5k,2k (2k) | 24 | beta |
| `retouch-inpaint-lab-gpt-2` | GPT 2 | replace, erase | creative | 1k,2k,4k (2k) | 102 | beta, private |
| `retouch-precise-gpt-2` | GPT 2 | replace, erase | precise | – | 116 | beta, private |
| `retouch-inpaint-lab-gpt-2-mini` | GPT 2.5 | replace, erase | creative | 1k,2k,4k (2k) | 36 | beta, private |
| `retouch-precise-gpt-2-mini` | GPT 2.5 | replace, erase | precise | – | 152 | beta, private |
| `retouch-precise-nano-banana-2-flash` | Nano Banana 2 | replace, erase | precise | – | 156 | beta, private |
| `retouch-inpaint-lab-nano-banana-2-1` | Nano Banana 2.1 | replace, erase | creative | 1k,2k,4k (2k) | 48 | beta, private |
| `retouch-precise-nano-banana-2-1` | Nano Banana 2.1 | replace, erase | precise | – | 128 | beta, private |
| `retouch-precise-nano-banana-pro` | Nano Banana Pro | replace, erase | precise | – | 111 | beta, private |
| `retouch-ideogram-4-5` | Ideogram 4.5 | replace, erase | creative | – | 49 | beta, private |
| `retouch-precise-ideogram-4-5` | Ideogram 4.5 | replace, erase | precise | – | 37 | beta, private |

`private: true` = "internal or feature-flagged, not generally available". Hide those in the UI, or fetch the list at
runtime and filter. Response format is TOON text (see §3).

Costs via `simulate_cost` (exact, credits) **[verified]**: erase auto **30**; replace auto **10**;
replace `retouch-pro` **50**; replace `retouch-inpaint-lab-nano-banana-2-flash` @1k **75**;
replace `retouch-inpaint-lab-seedream-5-pro` **100**.

### 4.5 `images_generate`: whole-image edit via reference (alternative path, supports count + seed)
Key fields: `prompt` (**required**), `mode` (model slug, default `auto`), `references`
`[{"type":"image","identifier":"<creationId>"}]` (max 12), `count` 1–8 (one call, N results),
`seed` 0–4294967295 (with count>1 each variant uses seed+index, verified 1234/1235),
`width`+`height` (exact px, min 50, only models with `customSize`; replaces aspectRatio),
`aspectRatio` (enum `1:1 21:9 16:9 9:16 2:3 3:4 1:2 2:1 5:4 4:5 3:2 4:3`; "edits may snap"),
`resolution`, `quality`, `variant`, `tier`, `extraSettings`, `preciseEdit` (ideogram-4-5 only: edit first
image ref in place), `transparentBackground`, `folderReference`. Brand-kit fields are irrelevant here.

Edit-capable models (`referenceTypes` includes `image`), none private unless noted:
| slug | name | customSize | resolutions | notes |
|---|---|---|---|---|
| `seedream-5-pro` | Seedream 5 Pro | yes (max 16:1) | 1.5k, 2k | server's default "sota" pick for edits |
| `imagen-nano-banana-2-flash` | Nano Banana 2 | yes (8:1) | 1k,2k,4k | **tested**, 75 cr/img @1k |
| `imagen-nano-banana-2` | Nano Banana **Pro** (naming trap) | yes (8:1) | 1k,2k,4k | |
| `imagen-nano-banana-2-lite` | Nano Banana 2 Lite | no | – | cheap/fast, 60 cr/img |
| `imagen-nano-banana` | Nano Banana | no | – | |
| `gpt-2` | GPT 2 | yes (8:1) | 1k,2k,4k | qualities low/medium/high |
| `gpt-2-mini` | GPT 2.5 (beta) | yes (8:1) | 1k,2k,4k | variants flare/sunburst |
| `flux-kontext` / `flux-kontext-high` | Flux.1 Kontext Pro / Max | no | – | 100 cr/img (Pro) |
| `flux-2`, `flux-2-max`, `flux-2-flex`, `flux-2-klein` | Flux.2 | no | 1k, 2k | |
| `seedream-4`, `seedream-4-4k`, `seedream-4-5`, `seedream-5-flash`, `seedream-5-lite` | Seedream | no | various | |
| `magnific-one` | Magnific One | yes | 1k,2k,4k | private |
| `ideogram-4-5` | Ideogram 4.5 | no | – | private; `preciseEdit` |
| `flux-3-image` | Flux.3 | no | 768p…4k | private |
Ideogram (`ideogram`, `ideogram-4`, `p-image-ideogram`) and older Flux models do NOT accept `image` refs.

Raw response (count=2) **[verified]**:
```json
{"creations":[
  {"identifier":"tCwJ54ZmZJ","status":"queued","expectTime":30,"tool":"text-to-image","credits":75,"webUrl":"…","mode":"imagen-nano-banana-2-flash"},
  {"identifier":"lJTMidugv9","status":"queued","expectTime":30,"tool":"text-to-image","credits":75,"webUrl":"…","mode":"imagen-nano-banana-2-flash"}],
 "instruction":"Queued. …"}
```
Note: the key is `creations` (array) here, but `creation` (object) for `images_retouch`.

### 4.6 `creations_wait`: long-poll
Input: `{"identifiers":[…1..8], "timeoutSeconds": 0..25}` (default 25, max 25). Loop until `allTerminal`.
In-progress entries carry `poll_after_seconds`. Raw completed response **[verified]**:
```json
{"results":[{"identifier":"YMHb8eKWeC","status":"completed",
   "results":{"url":"https://pikaso.cdnpk.net/private/production/<id>/render.png?token=exp=…",
              "thumbnailUrl":"https://pikaso.cdnpk.net/private/production/<id>/conversions/render-preview.png?token=exp=…"}}],
 "allTerminal":true}
```
**The final image URL is `results[i].results.url`.** Both test jobs finished within the first 25 s wait. Failure
status strings were not observed. Treat any status other than `completed`, `processing`, or `queued` as an error **[inferred]**.

### 4.7 `creations_get`: metadata (TOON text)
Input `{"creationIdentifier": "…"}` or `{"identifiers": [...≤50]}`. Returns `url`, `previewUrl`
(`url` + `&w=1024&preview=1`, which serves **JPEG**), `thumbnailUrl`, `metadata.{mode,slug,seed,width,height,credits,…}`.
**`metadata.width/height` are unreliable**: the retouch result said 864x864 and the generate result said 1024x768,
but both delivered files are 640x480. Read the size from the downloaded file.

### 4.8 Download
- `url` is a token-signed CDN URL (`pikaso.cdnpk.net/private/…?token=exp=…`). **A plain GET with no auth
  header works (200).** Without `?token` you get 403. **[verified]**
- The token `exp=1792368000` (2026-10-19 00:00 UTC) was the same for all URLs, so it looks like a validity of a few
  days, rounded to a day boundary. Download right away. Don't cache URLs.
- `content-type: image/png`. The file name is always `render.png`.

## 5. Live end-to-end test **[verified]**
Inputs (scratchpad `mtest/`): `plate.png` 640x480 RGB (sky/ground plus a red disk r=70 at 320,260), `mask.png`
640x480 RGB, white square x 230–409, y 170–349.

1. `account_balance` before: `{"plan":{"tier":"magnific","productName":"Premium+","isPaidPlan":true,"isUnlimitedMode":false,"unlimitedAppliesHere":false},"credits":{"available": <n>,"totalPlan": <n>,"spent": <n>,"hasExtraCredits":true,"scope":"personal"}}`
2. request_upload (count 2) → PUT ×2 (200) → finalize (`visible:false`) → `JNdBG4xOq4` (plate), `p8xLCodehw` (mask).
3. `images_retouch {creationIdentifier, maskCreationIdentifier, mode:"erase"}` (no model) → Auto routed to
   `retouch-erase` (metadata `slug: retouch-erase`, `seed: 1909226528`, prompt `" "`), 30 credits.
4. `creations_wait` → completed in under 25 s.
5. Download: **PNG 640x480, 8-bit RGBA (alpha all 255)**, 19.5 KB, same size as the input.
   The red object was fully removed. Mean abs RGB diff vs input: **0.03 outside the mask** (so the server
   effectively composites and keeps the unmasked pixels) and 168 inside.
6. Second paid test: `images_generate` with `mode: imagen-nano-banana-2-flash`, ref = plate, `count: 2`,
   `width: 640, height: 480, resolution: "1k", seed: 1234`, prompt "Remove the red ball…" → 2×75 credits.
   Results: **PNG 640x480 RGB** (exact custom size honoured, although metadata says 1024x768), ball removed,
   global drift outside the mask with mean abs diff 5–8. This is not pixel-preserving, so GenFill's own mask
   composite is required here.
7. `account_balance` after: available **<n>**, so a total of 180 credits spent (30 + 150).

## 6. Recommendations / gotchas for the implementer
- **Primary path: `images_retouch`.** It is pixel-preserving and returns the same size as the input. Erase costs 30
  and replace auto costs 10. For N variations, send N parallel calls, then `creations_wait` with up to 8 ids in one call.
- Plate and mask must have the **same pixel size**. Send the mask as a black/white PNG (RGB worked; grayscale
  probably works too, but I didn't test it).
- Upload both files with one `creations_request_upload(count=2)` and one batch `creations_finalize_upload(visible=false)`.
  Uploads are reusable across variations (upload once, retouch N times).
- Result decoding: the PNG can be RGBA (retouch) or RGB (generate). `previewUrl` is JPEG, so always use `url`.
- Rate limits seen: upload proxy `x-ratelimit-limit: 60` (per window, likely per minute). Re-PUT to the same URL → 429.
  I didn't hit any MCP-side limits.
- Everything lands in the user's "Personal" project (library). `visible:false` applies only to uploads. Generated
  results are visible in the user's library.
- `simulate_cost` (free) takes the same args and returns `{"credits":N,"isUnlimited":false,"certainty":"exact"}`.
  It is useful for a cost preview in the UI. `account_balance` gives `credits.available`.
- Retouch models with `resolutions` (e.g. 2k default) may upscale the output. I only tested Auto/erase, which
  returned the input size. If you use resolution models, resize the result back to the crop size before compositing.
