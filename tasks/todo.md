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
