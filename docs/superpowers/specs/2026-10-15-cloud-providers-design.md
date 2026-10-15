# Cloud providers: Magnific, Higgsfield, Comfy Cloud (MCP) and fal.ai (API)

Date: 2026-10-15. Status: approved (decisions taken with the user at the start of the session).

## Goal

Four new generation backends for GenFill, each exposing every model that is useful for
Generative Fill:

| Backend | Transport | Auth |
|---|---|---|
| Magnific | MCP (Streamable HTTP) at `https://mcp.magnific.com` | OAuth 2.1 (browser sign-in, PKCE, dynamic client registration) |
| Higgsfield | MCP at `https://mcp.higgsfield.ai/mcp` | OAuth 2.1 (same flow) |
| Comfy Cloud | MCP at `https://cloud.comfy.org/mcp` | `X-API-Key` (comfyui-...) if set, otherwise OAuth 2.1 |
| fal.ai | REST queue API (`queue.fal.run`) | `Authorization: Key <FAL_KEY>` |

"Relevant models" means (user decision): models with **true mask inpainting** plus strong
**reference-edit models** (they re-render the crop; GenFill composites them back through the
feathered selection, as it already does for OpenAI). Upscalers, restyle and similar are out of
scope.

## Decisions

- GenFill is its **own MCP client**, stdlib only (urllib, http.server, hashlib, secrets,
  webbrowser). No dependency on Claude, no third-party packages: it must run in Nuke's Python
  3.7+ on Windows, macOS and Linux.
- OAuth tokens are stored with the other secrets in `~/.nuke/genfill/config.json` (0600),
  never in .nk scripts. Refresh tokens are used silently; a browser sign-in only happens the
  first time, or when a refresh fails.
- Each backend keeps the existing `Backend` interface (`model_size`, `generate`), so the
  engine, state, colour pipeline and node graph stay unchanged.
- Model choice: the existing `gf_model` ("model version") knob holds the model id. The panel
  gets an editable model-version combo filled from each backend's curated catalogue; typing any
  other id still works (free-text override).

## Components

### `backends/mcp.py` — MCP client
- `McpClient(url, auth)`: JSON-RPC 2.0 over Streamable HTTP. `initialize` (protocol version
  2025-06-18, falls back to what the server answers), `notifications/initialized`,
  `tools/call`, `tools/list`.
- Accepts both `application/json` and `text/event-stream` responses (parses SSE `data:` lines,
  picks the response whose `id` matches). Keeps `Mcp-Session-Id` and `MCP-Protocol-Version`.
- `call_tool(name, args)` returns the tool's `structuredContent` if present, else parses the
  first JSON text content, else the text. `isError` → `BackendError` with the server's message.
- On HTTP 401 it asks the auth object to refresh (once), then re-authorise interactively if
  allowed; on 404 with a session id it re-initialises (session expired).
- Auth objects: `ApiKeyAuth(header, key)`, `OAuthAuth(provider_id, server_url)`.

### `backends/oauth.py` — OAuth 2.1 for MCP
- Discovery: the 401's `WWW-Authenticate: Bearer resource_metadata=...`, else
  `/.well-known/oauth-protected-resource[/path]` (RFC 9728) → authorization server →
  `/.well-known/oauth-authorization-server` or `openid-configuration` (RFC 8414).
- Dynamic client registration (RFC 7591) as a public client (`token_endpoint_auth_method:
  none`) with a loopback redirect `http://127.0.0.1:<port>/callback`. The registration is cached
  per provider and reused; the port is fixed per provider registration (first free port from a
  small list) because some servers match redirect URIs exactly.
- Authorization code + PKCE S256, `state`, `resource` parameter (RFC 8707). The browser is
  opened with `webbrowser.open`; a one-shot `http.server` on 127.0.0.1 receives the code and
  shows a "you can close this tab" page. Waiting is cancellable (the generate progress Cancel)
  and times out after 5 minutes.
- Token refresh with `refresh_token`; tokens + expiry stored in config under `oauth.<id>`.
- `sign_out(provider)` deletes tokens and registration.

### `backends/<provider>.py`
Each backend has a curated model catalogue (`ModelSpec`: id, label, kind = `inpaint` | `edit`
| `erase`, size rules, max count) and maps a `GenerationRequest` to the provider's calls:
upload image (+ mask), submit, poll/wait, download results. Empty prompt → the provider's
removal route (an `erase` model, or the inpaint model with a clean-plate prompt).
Exact tool names, fields and model ids come from the research notes in the plan.

### Result images
Providers may return JPEG or WebP. The engine already writes whatever bytes the backend returns;
backends request PNG where the provider allows it. A shared helper `base.ensure_supported(data)`
detects PNG/JPEG by magic bytes; the engine names the file `.png`/`.jpg` accordingly (Nuke reads
both). WebP is avoided by requesting PNG/JPEG; if a provider can only return WebP the backend
reports a clear error.

### UI
- API Keys dialog → "API Keys & Accounts": key fields as before (fal, Comfy optional) plus a
  row per OAuth provider with status ("Signed in" / "Not signed in") and Sign in / Sign out
  buttons. Sign in runs in a background thread with the browser.
- Generate with an OAuth backend and no token: the worker thread runs the sign-in itself
  (progress text "Waiting for sign-in in your browser..."), so Nuke's UI never blocks.
- Panel: an editable "version" combo next to the backend combo (curated models of the current
  backend; empty = default).

## Error handling
- Every provider error becomes a `BackendError` with an artist-readable message (auth,
  credits, content filter, timeouts, server errors), as in the existing backends.
- Spend gates in Comfy tools (`confirm`): the artist clicking Generate is the consent, so GenFill
  passes `confirm: true`.
- Polling honours `cancelled()` between requests; a cancelled job is cancelled server-side where
  the provider offers it.

## Testing
- Unit tests (stdlib `unittest`, mocked HTTP, like `tests/test_core.py`): MCP JSON and SSE
  parsing, session handling, 401 refresh, OAuth discovery/DCR/PKCE/token exchange/refresh with a
  fake server, loopback callback, and each provider's request mapping and result decoding.
- Live verification: a few cheap real generations per provider (user approved), fal only if a
  `FAL_KEY` is available.

## Out of scope
- Video, upscaling, style transfer.
- Float/HDR transport for cloud providers (8-bit PNG as today).
