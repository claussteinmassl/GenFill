# Cloud providers (feat/cloud-providers)

Plan: docs/superpowers/plans/2026-10-15-cloud-providers.md

- [x] Research (Magnific, Higgsfield, Comfy verified live; fal docs)
- [x] MCP client + OAuth + shared base (tests/test_mcp.py)
- [x] Task 1 Magnific backend
- [x] Task 2 Higgsfield backend
- [x] Task 3 Comfy Cloud backend
- [x] Task 4 fal.ai backend
- [x] Task 5 Registry + accounts dialog + version combo
- [x] Task 6 Docs + version 0.8.0
- [x] Task 7 Live verification
- [x] Task 8 Review + PR

## Review
- 198 unit tests green (macOS py3.9; Windows py3.12 and py3.7).
- Live: Magnific end to end through GenFill (OAuth sign-in auto, retouch erase). Higgsfield and Comfy:
  sign-in needs a browser step by the user (timed out unattended); catalogues verified with free preflights / dry runs.
  fal.ai: no key available, mocked only.

# Pipeline environment variables (same branch)

- [x] Keys: already from env (STABILITY_API_KEY, OPENAI_API_KEY, FAL_KEY, COMFY_API_KEY); Magnific/Higgsfield OAuth only
- [x] naming.py: patterns with $VAR/${VAR}/%VAR% + {token[:fmt]}, safe names, number always present
- [x] GENFILL_CACHE for every script (knob > env > script dir > config > temp), GENFILL_NAME, GENFILL_VARIATION_NAME
- [x] Numbers never reused (state "number"), folder collisions bump the number, bad patterns stop before rendering
- [x] Tests (test_naming.py, pipeline env test), README/ARCHITECTURE/CHANGELOG

## Review
- 218 unit tests green (macOS py3.9). Knob value with `{tokens}` goes through Nuke's TCL evaluate first: unverified in Nuke.

# ComfyUI workflows + model/resolution/cost features (feat/comfyui-workflows)

Plan: docs/superpowers/plans/2026-10-15-comfyui-workflows.md

- [x] T1 workflow library, T2 ComfyUI backend, T3 shipped workflows, T4 Local AI removed + settings, T5 docs 0.9.0
- [x] T7 current catalogues, T8 live model discovery, T9 resolution 1K/2K/4K, T10 multi-line prompt, T11 cost + balance
- [x] T12 live verification, T13 final review

## Review
- 325 unit tests green: macOS py3.9, Windows VM py3.12 and py3.7.
- Live: ComfyUI SDXL inpaint 1K (319 s) and 2K (2048x1536, 857 s) on the local MPS ComfyUI; shipped FLUX/API workflows
  validate (only missing models / missing login); Magnific GPT 2.5 generation OK (27.8 s); Magnific cost quotes and
  balance live in the panel (offscreen PySide6 + fake nuke); discovery found 16 new Magnific models.
- Unverified (no login/key here): Comfy Cloud GPT 2.5 / Ideogram V3 mask polarity and resolution passthrough,
  fal Ideogram 4.5 mask inversion, fal pricing API, Stability balance. Higgsfield mask inpainting stays off (server
  rejects role "mask"). Nuke-only: multi-line knob save/reload with [ ] $ { }, legacy String_Knob newlines.
