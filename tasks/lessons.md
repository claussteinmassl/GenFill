# Lessons

- Never commit with `git add -A` in a repo where Claude Code runs: it picks up `.claude/scheduled_tasks.lock`
  (session id, pid). Add only named files, and keep `.claude/*.lock` in .gitignore.
- Background OAuth sign-ins cannot be completed autonomously when the provider shows a login/consent page
  (Higgsfield/Clerk, Comfy): plan live checks so the user can finish the browser step, or use an API key.
