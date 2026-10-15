# Lessons

- Never commit with `git add -A` in a repo where Claude Code runs: it picks up `.claude/scheduled_tasks.lock`
  (session id, pid). Add only named files, and keep `.claude/*.lock` in .gitignore.
- Background OAuth sign-ins cannot be completed autonomously when the provider shows a login/consent page
  (Higgsfield/Clerk, Comfy): plan live checks so the user can finish the browser step, or use an API key.
- Unit tests must never read the user's real `~/.nuke/genfill` (config.json, models_cache.json): a live
  smoke run populated the discovery cache and broke a catalogue-count test. Every test module imports
  `tests/isolated_env.py`; new user-state files need an override env var and a guard test.
- When parallel subagents share one working tree, an implementer may leave its work uncommitted to avoid
  staging the other agent's edits: always tell implementers to commit only their files by explicit path.
- A provider's model schema can list inputs the server rejects (Higgsfield lists role "mask", the API
  answers "Unknown media role: mask"): verify with a free preflight (get_cost / simulate_cost) before
  enabling a request shape.
