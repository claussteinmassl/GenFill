# Installers

Double-click these from Explorer (Windows). Each one opens a console, says what it's doing,
and waits for a key press at the end.

| Script | What it does |
|---|---|
| **install.bat** | Runs the unit tests, then installs a fresh copy of the current build to `%USERPROFILE%\.nuke\plugins\GenFill` and makes your Nuke `init.py` load it. Run it every time you want to test the latest code. |
| **install_and_launch.bat** | Same, then starts the newest Nuke it finds. |
| **install_dev.bat** | Points Nuke straight at this repo instead of copying. Edits are live after a restart or **GenFill > Developer > Reload GenFill**. |
| **uninstall.bat** | Removes the installed build and the init.py block. Keeps your API keys and generated images. |
| **run_smoke_test.bat** | Runs the smoke test in Nuke's terminal mode (needs a license that allows `nuke -t`). |

## What install does, exactly

1. Finds your Nuke installs under Program Files.
2. Runs `python -m unittest discover -s tests` using **Nuke's own python.exe**, so the tests run
   on the same Python version Nuke uses. If they fail, nothing is installed (`-Force` overrides).
3. Deletes the previous build and copies `nuke\` to the install folder.
4. Writes `build_info.json` into the build: version, git commit (with `*` if you have
   uncommitted changes) and time. Nuke prints this at startup and shows it under
   **GenFill > Developer > Build Info**, so you always know which build you're testing.
5. Backs up `%USERPROFILE%\.nuke\init.py` to `init.py.genfill-backup`, then adds or updates one
   clearly marked block:

   ```python
   # >>> GenFill (managed by GenFill installers\install.ps1 - edits here are overwritten) >>>
   import nuke
   nuke.pluginAddPath(r"C:\Users\you\.nuke\plugins\GenFill")
   # <<< GenFill <<<
   ```

   Any hand-written GenFill `pluginAddPath` line elsewhere in init.py gets commented out so the
   plugin doesn't load twice. Everything else in your init.py is left alone.
6. Warns you if Nuke is already open.

## Options (PowerShell)

```powershell
.\install.ps1 -Mode dev                     # live from repo
.\install.ps1 -SkipTests                    # don't run tests
.\install.ps1 -Force                        # install even if tests fail
.\install.ps1 -Target D:\nuke\GenFill       # different install folder (name must contain GenFill)
.\install.ps1 -Launch -NukeArgs "--nukex"   # start NukeX after installing
```

## Testing a build in Nuke

After installing, in Nuke: **GenFill > Developer > Run Smoke Test**. It builds a throwaway
setup inside a Group called `GenFill_SmokeTest`, generates with the offline backend (no key or
network needed), renders the results and writes everything to `test_output\` in this repo:

- `report.json`: pass/fail per step with tracebacks, Nuke and Python versions, and which knobs
  exist on this Nuke version
- `comp.png`, `fill.png`: what the node outputs
- `source.png`, `mask.png`, `var_01.png`: what went to and came back from the model
- `panel.png`: a screenshot of the GenFill panel

Claude can read `test_output\` directly, so after a run you can just say "check the smoke test".

## ComfyUI

GenFill's free local model runs on a ComfyUI server you install yourself; see the main README
(Install > ComfyUI). There is nothing to install from this folder.

## Checking a backend against the real service

`live_check.py` generates once with a backend outside Nuke, like GenFill does, and writes the
results and a report to `test_output\live_<backend>`:

```
python installers/live_check.py <backend id> [model] [--prompt "..."] [--count N] [--out DIR]
```

For ComfyUI the model is a workflow id, and `--workflows DIR` adds a workflow folder (it sets
`GENFILL_COMFY_WORKFLOWS`). The server comes from `GENFILL_COMFY_URL` or the saved URL (default
`http://127.0.0.1:8188`). The test fixtures use SDXL, which needs
`sd_xl_base_1.0_inpainting_0.1.safetensors` in the server's `models/checkpoints` folder:

```
python installers/live_check.py comfyui sdxl_inpaint --workflows tests/fixtures/comfyui
```

An empty `--prompt ""` runs the erase partner (`sdxl_erase`). Cloud backends spend real credits.

`--resolution 1K|2K|4K` checks a processing resolution: the synthetic crop is made as large as
the tier, and the line printed first shows the size sent ("sends 2048 x 1536 (2K)" or the
model's maximum).
