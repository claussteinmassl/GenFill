# Cloud Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Magnific, Higgsfield and Comfy Cloud (via their remote MCP servers) and fal.ai (REST)
as GenFill backends, with every inpaint/edit model that is useful for Generative Fill.

**Architecture:** GenFill is its own stdlib MCP client (`backends/mcp.py`) with OAuth 2.1
sign-in (`backends/oauth.py`); `backends/cloud.py` holds the shared `McpBackend` base and
helpers. Each provider is one module implementing the existing `Backend` interface
(`model_size`, `generate`) plus a curated `catalogue` of `ModelSpec`s. The engine, node graph,
state and colour pipeline are unchanged.

**Tech Stack:** Python 3.7+ stdlib only (urllib, http.server, json, hashlib, secrets,
threading), `unittest`, Qt (PySide2/6) for the dialog.

**Spec:** `docs/superpowers/specs/2026-10-15-cloud-providers-design.md`
**Protocol notes (verified live):** `docs/superpowers/research/research-{magnific,higgsfield,comfy,fal}.md`

## Global Constraints

- Stdlib only; must import under Python 3.7 (Nuke 13) — no f-strings with `=`, no walrus, no
  `dict | dict`, keep `from __future__ import annotations`.
- Backends never import `nuke`. Every error the artist can see is a `BackendError` with a
  readable message; cancellation raises `Cancelled` (check `cancelled()` between every network
  step and inside polling).
- `generate(request, api_key, progress=None, cancelled=None)` returns a list of image bytes
  (PNG or JPEG, validated with `base.check_image`), one per variation, exactly
  `request.count` items unless the provider returned fewer (then raise if zero).
- Inputs: `request.image_path` (8-bit sRGB PNG, size `request.width` x `request.height`),
  `request.mask_path` (RGB PNG, white = fill), `request.mask_alpha_path` (RGBA, alpha 0 = fill),
  `request.prompt` (empty = remove), `request.seed` (0 = random), `request.count`,
  `request.model` (empty = backend default), `request.negative_prompt`, `request.quality`.
- Results may be any size; the node reformats them to the crop. Aspect should match the
  request, so `model_size` must return a size whose aspect the model will honour.
- Paid generation tools: the Generate click is the artist's consent (pass `confirm: true`,
  `use_unlim: false` etc. as the provider requires).
- Tests: `python3 -m unittest discover tests` must pass; network is faked with
  `tests/fake_servers.py` (`FakeMcpServer`, `text_result`) or `unittest.mock`.
- Code style: match `backends/openai_images.py` / `stability.py` (module docstring explaining
  the provider, Google-style docstrings where a function needs one, English only).
- Commits end with `Claude-Session: https://claude.ai/code/session_0145ayo4GhtZYb892kjAnJFw`.

**User decisions (already made):**
- "Real MCP + OAuth": GenFill is its own MCP client; Magnific/Higgsfield via OAuth sign-in,
  Comfy via X-API-Key (or OAuth).
- "Inpaint + edit models": true mask inpainting plus strong reference-edit models, comped
  through the mask; curated list plus free-text override.
- "Yes, a few cheap ones": live tests with real accounts allowed (1–2 cheap generations per
  provider).
- "Feature branch + PR": branch `feat/cloud-providers`, PR against `claussteinmassl/GenFill` main.
- The user asked not to be asked anything after the start: decide autonomously.

---

## Done before this plan (commits on the branch)

- `backends/base.py`: `http_request`, `http_json`, `download`, `raise_for_status`, `header`,
  `sniff_image`, `check_image`, `ModelSpec(id, label, kind, erase, max_count, note, **extra)`,
  `Backend.catalogue / model_ids() / model_spec(model) / auth`, prompt constants
  `EDIT_EMPTY_PROMPT`, `EDIT_PROMPT_SUFFIX`, `REF_EMPTY_PROMPT`, `REF_PROMPT_SUFFIX`.
- `backends/mcp.py`: `McpClient(url, auth, label, interactive, progress, cancelled, timeout)`
  with `connect()`, `list_tools()`, `call_tool(name, args, timeout=None)` → decoded result
  (structuredContent, else JSON text, else text); `ApiKeyAuth`, `OAuthAuth`.
- `backends/oauth.py`: `OAuthProvider(id, server_url, label, scopes)` with `access_token()`,
  `login()`, `signed_in()`, `sign_out()`, `invalidate()`, `discover()`.
- `backends/cloud.py`: `McpBackend` (`server_url`, `api_key_header`, `client(api_key, progress,
  cancelled)`, `provider()`, `signed_in()`), `ref_prompt(prompt)`, `put_bytes(url, data, ctype,
  headers)`, `poll(fn, cancelled, timeout, interval, what)`.
- `engine.py`: passes optional keys; names results `.png`/`.jpg` by content.
- `config.py`: `update(fn)`, `get_oauth(id)`, `set_oauth(id, value)`.

### Task 1: Magnific backend

**Goal:** `backends/magnific.py` with `MagnificBackend(McpBackend)` (id `magnific`, label
`Magnific (MCP)`), retouch inpainting + reference-edit models.

**Files:**
- Create: `nuke/python/genfill/backends/magnific.py`
- Test: `tests/test_magnific.py`

**Acceptance Criteria:**
- [ ] `server_url = "https://mcp.magnific.com"`, `auth = "oauth"`.
- [ ] Catalogue: retouch models (kind `inpaint`): `retouch-auto` (default), `retouch-pro`,
      `retouch-classic`, `retouch-imagen-nano-banana-2-flash`, `retouch-imagen-nano-banana-2`,
      `retouch-inpaint-lab-nano-banana-2-flash`, `retouch-inpaint-lab-nano-banana-pro`,
      `retouch-inpaint-lab-seedream-5-pro`, `retouch-erase` (erase only); edit models (kind
      `edit`, via `images_generate` with an image reference): `seedream-5-pro`,
      `imagen-nano-banana-2-flash`, `imagen-nano-banana-2`, `imagen-nano-banana-2-lite`,
      `gpt-2`, `flux-kontext`, `flux-kontext-high`, `flux-2`, `seedream-4-5`. No private models.
- [ ] Upload: one `creations_request_upload {mimeType:"image/png", count:2}`, PUT both files
      to `proxyUploadUrl` (only `Content-Type`), one `creations_finalize_upload` with
      top-level `fileName`, `uploads[{path,fileName}]`, `visible:false`.
- [ ] Retouch: N `images_retouch` calls (`mode` `replace`+prompt, or `erase` for an empty
      prompt; a model without erase falls back to `retouch-auto` for erase); edit models: one
      `images_generate` with `references:[{type:"image",identifier}]`, `count` (≤ 8),
      `seed` if non-zero, `width/height` for custom-size models else the nearest
      `aspectRatio`, prompt via `ref_prompt`.
- [ ] Wait: `creations_wait {identifiers (≤8), timeoutSeconds: 25}` until `allTerminal`;
      result URL = `results[i].results.url`; download without auth; failed status → error.
- [ ] Unknown model ids starting with `retouch-` are treated as retouch, others as edit.
- [ ] Tests cover: upload sequence, retouch replace/erase, erase fallback, edit-model request
      (count, seed, size), wait loop with a pending round, failed job, cancel.

**Verify:** `python3 -m unittest tests.test_magnific -v` → all pass

### Task 2: Higgsfield backend

**Goal:** `backends/higgsfield.py` with `HiggsfieldBackend(McpBackend)` (id `higgsfield`,
label `Higgsfield (MCP)`), reference-edit models.

**Files:**
- Create: `nuke/python/genfill/backends/higgsfield.py`
- Test: `tests/test_higgsfield.py`

**Acceptance Criteria:**
- [ ] `server_url = "https://mcp.higgsfield.ai/mcp"`.
- [ ] Catalogue from `research-higgsfield.md` (default `gpt_image_2_5`; plus the nano banana,
      seedream, flux kontext, ideogram and other image-reference edit models listed there).
- [ ] Upload: `media_upload` → PUT to `upload_url` with the returned content type →
      `media_confirm {type:"image", media_ids:[...]}`.
- [ ] Generate with `generate_image_batch` (or `generate_image`, as the research recommends)
      using `params` with top-level `model`, `prompt` (`ref_prompt`), `medias` with the
      image-reference role, `aspect_ratio` nearest to the crop, `use_unlim: false`, `seed`
      where supported; the mask role is not used (server rejects it).
- [ ] Wait: `jobs_wait` (timeout ≤ 15) until `all_terminal`; download each `result_url`.
- [ ] `model_size` returns a size with an aspect the model supports.
- [ ] Tests cover upload, generate request shape, wait loop, failure, cancel.

**Verify:** `python3 -m unittest tests.test_higgsfield -v` → all pass

### Task 3: Comfy Cloud backend

**Goal:** `backends/comfy_cloud.py` with `ComfyCloudBackend(McpBackend)` (id `comfy`, label
`Comfy Cloud (MCP)`), true mask inpainting via workflow graphs plus partner edit models.

**Files:**
- Create: `nuke/python/genfill/backends/comfy_cloud.py`
- Test: `tests/test_comfy_cloud.py`

**Acceptance Criteria:**
- [ ] `server_url = "https://cloud.comfy.org/mcp"`, `api_key_header = "X-API-Key"`,
      `env_var = "COMFY_API_KEY"`, `auth = "key_or_oauth"`.
- [ ] Upload: `upload_file {file_path, client_os}` → parse the single-use PUT URL from the
      returned text/structure → PUT the bytes (no auth) → `name`.
- [ ] Mask models run as one `submit_workflow` graph (`confirm: true`) with N fill nodes
      (seed, seed+1, …) and N `SaveImage` nodes: `FluxProFillNode` (default `flux-pro-fill`),
      `FluxEraseNode` for empty prompts, and the other mask nodes from the research
      (Bria GenFill/Eraser, Recraft inpaint, Ideogram edit) where the node spec is known.
- [ ] Partner edit models (no mask) via `partner_generate` with `medias:[{role:"image",
      name}]` or the equivalent workflow, as the research shows.
- [ ] Wait with `wait_for_job`/`get_job_status`, then `get_output`; download every output URL
      immediately (follow redirects).
- [ ] Tests cover upload parsing, graph construction (N variations), erase route, partner
      route, wait/output parsing, failure, cancel.

**Verify:** `python3 -m unittest tests.test_comfy_cloud -v` → all pass

### Task 4: fal.ai backend

**Goal:** `backends/fal.py` with `FalBackend(Backend)` (id `fal`, label `fal.ai`,
`env_var = "FAL_KEY"`), queue API, inpaint + edit endpoints.

**Files:**
- Create: `nuke/python/genfill/backends/fal.py`
- Test: `tests/test_fal.py`

**Acceptance Criteria:**
- [ ] `Authorization: Key <key>`; submit `POST https://queue.fal.run/<endpoint>`; poll the
      returned `status_url` until `COMPLETED` (honour cancel; `PUT cancel_url` on cancel);
      fetch `response_url`; download `images[].url`.
- [ ] Images sent as data URIs (or fal storage upload if the research says data URIs are
      not accepted / too large).
- [ ] Catalogue from `research-fal.md` (mask inpainting endpoints + reference-edit
      endpoints, removal default for empty prompts), field mapping per endpoint.
- [ ] Tests with mocked `http_request` cover submit/poll/result, data-URI inputs, per-endpoint
      payloads, errors (401/422/content filter), cancel.

**Verify:** `python3 -m unittest tests.test_fal -v` → all pass

### Task 5: Registry, accounts dialog and model-version combo

**Goal:** The four backends appear in the model menu; artists can sign in/out and pick a model.

**Files:**
- Modify: `nuke/python/genfill/backends/__init__.py` (register, order: Local, Stability,
  OpenAI, fal.ai, Magnific, Higgsfield, Comfy Cloud, Offline Preview)
- Modify: `nuke/python/genfill/ui/settings.py` ("API Keys & Accounts": key rows for key and
  key_or_oauth backends; per OAuth backend a status label + Sign in / Sign out, sign-in in a
  thread, UI updated via a Qt signal/timer)
- Modify: `nuke/python/genfill/ui/panel.py` (editable "version" combo bound to `gf_model`,
  items = `backend.model_ids()`, empty entry = default)
- Modify: `nuke/python/genfill/node.py` (knob tooltips, button label)
- Modify: `nuke/python/genfill/engine.py` (OAuth backend not signed in: proceed — the worker
  signs in; status message)
- Test: `tests/test_core.py` (registry ids), `tests/test_pipeline.py` if it lists backends

**Acceptance Criteria:**
- [ ] `backends.ids() == ["local", "stability", "openai", "fal", "magnific", "higgsfield", "comfy", "mock"]`.
- [ ] Settings dialog builds under the fake/no-Qt test environment is not required; the
      module must still import without Qt errors in Nuke (manual).
- [ ] Full test suite passes.

**Verify:** `python3 -m unittest discover tests` → OK

### Task 6: Docs and version

**Goal:** README (Models table, Install → cloud accounts, Privacy), ARCHITECTURE (MCP/OAuth
section), CHANGELOG 0.8.0, `__version__ = "0.8.0"`, `USER_AGENT`.

**Files:** `README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `nuke/python/genfill/__init__.py`,
`nuke/python/genfill/backends/base.py`

**Acceptance Criteria:**
- [ ] Every new backend documented with auth, models, notes; privacy section names where
      tokens live.

**Verify:** `grep -n "0.8.0" CHANGELOG.md nuke/python/genfill/__init__.py`

### Task 7: Live verification

**Goal:** Prove each backend against the real service with 1–2 cheap generations through
GenFill's own code (a script calling `backend.generate` on a test crop).

**Acceptance Criteria:**
- [ ] Comfy, Magnific, Higgsfield: OAuth sign-in or key, one generation each, result image
      written and inspected; fal only if `FAL_KEY` is available.
- [ ] Anything that could not be verified is reported as such.

**Verify:** `python3 scripts/live_check.py <backend>` → writes results to the scratchpad

### Task 8: Review and PR

**Goal:** Code review of the branch, fixes, PR against main.
