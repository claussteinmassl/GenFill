<#
.SYNOPSIS
    Install the current GenFill build from this repository into Nuke.

.DESCRIPTION
    Every run:
      1. runs the unit tests (with Nuke's own python.exe if it can find one)
         and stops if they fail,
      2. copies the repo's nuke\ folder to a clean install folder
         (default: %USERPROFILE%\.nuke\plugins\GenFill) and stamps it with
         build_info.json (version, git commit, time) so Nuke shows exactly which
         build is loaded,
      3. makes sure %USERPROFILE%\.nuke\init.py loads it, using a clearly
         marked block it manages (init.py is backed up first).

    -Mode dev skips the copy and points Nuke straight at the repo, so every
    save is live after a restart or GenFill > Developer > Reload GenFill.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Mode dev
    .\install.ps1 -Launch
    .\install.ps1 -SkipTests -Target D:\pipeline\nuke\GenFill
#>
[CmdletBinding()]
param(
    [ValidateSet('copy', 'dev')]
    [string]$Mode = 'copy',
    [string]$Target = (Join-Path $env:USERPROFILE '.nuke\plugins\GenFill'),
    [switch]$SkipTests,
    [switch]$Force,
    [switch]$Launch,
    [string]$NukeArgs = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

. (Join-Path $PSScriptRoot 'common.ps1')

$Repo = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $Repo 'nuke'
$PkgInit = Join-Path $Src 'python\genfill\__init__.py'
if (-not (Test-Path $PkgInit)) {
    throw "Can't find the GenFill source at $Src. Run this from the repo's installers folder."
}

$version = '?'
if ((Get-Content $PkgInit -Raw) -match '__version__\s*=\s*"([^"]+)"') { $version = $Matches[1] }

Write-Host ''
Write-Host "GenFill installer  -  version $version  -  mode: $Mode" -ForegroundColor White
Write-Host "Repo: $Repo"

# ---------------------------------------------------------------- Nuke installs
Write-Step 'Looking for Nuke'
$nukes = @(Get-NukeInstalls)
if ($nukes.Count -eq 0) {
    Write-Warn 'No Nuke found under Program Files (that is fine if it lives elsewhere).'
} else {
    foreach ($n in $nukes) { Write-Ok ("{0}  ({1})" -f $n.Name, $n.Exe) }
}

# ---------------------------------------------------------------- obsolete files
# Files from older GenFill versions that no longer exist in the repo.
$Obsolete = @(
    'installers\install_local_ai.ps1',
    'installers\install_local_ai.bat',
    'installers\start_local_ai.ps1',
    'installers\start_local_ai.bat',
    'nuke\python\genfill\backends\local.py',
    'nuke\python\genfill\localai\__init__.py',
    'nuke\python\genfill\localai\client.py',
    'nuke\python\genfill\localai\installer.py',
    'nuke\python\genfill\localai\worker.py',
    'nuke\python\genfill\ui\localai_dialog.py',
    'tests\test_localai.py',
    'tests\test_installer.py'
)
foreach ($rel in $Obsolete) {
    $p = Join-Path $Repo $rel
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Force
        Write-Ok "Removed obsolete file $rel"
    }
}

# ---------------------------------------------------------------- tests
if ($SkipTests) {
    Write-Warn 'Skipping tests (-SkipTests).'
} else {
    Write-Step 'Running unit tests'
    $py = Find-Python $nukes
    if (-not $py) {
        Write-Warn 'No Python found (Nuke python.exe or python on PATH). Skipping tests.'
    } else {
        Write-Ok "Using $py"
        $code = Invoke-Tests $py $Repo
        if ($code -ne 0) {
            if ($Force) {
                Write-Warn "Tests FAILED (exit $code) but -Force was given. Installing anyway."
            } else {
                Write-Fail "Tests FAILED (exit $code). Nothing was installed. Use -Force to install anyway."
                exit 1
            }
        } else {
            Write-Ok 'All tests passed.'
        }
    }
}

# ---------------------------------------------------------------- build info
$git = Get-GitInfo $Repo
$built = Get-Date -Format 'yyyy-MM-dd HH:mm'

# ---------------------------------------------------------------- install
if ($Mode -eq 'copy') {
    Assert-SafeTarget $Target
    Write-Step "Installing build to $Target"
    if (Test-Path $Target) {
        try {
            Remove-Item -LiteralPath $Target -Recurse -Force
        } catch {
            Write-Fail "Could not remove the previous build ($($_.Exception.Message)). Close Nuke and try again."
            exit 1
        }
    }
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    Copy-Item -Path (Join-Path $Src '*') -Destination $Target -Recurse -Force
    Get-ChildItem -LiteralPath $Target -Recurse -Directory -Filter '__pycache__' |
        Remove-Item -Recurse -Force
    $info = [ordered]@{
        version = $version
        commit  = $git.Commit
        dirty   = $git.Dirty
        built   = $built
        mode    = 'copy'
        source  = $Repo
    }
    Write-Utf8NoBom (Join-Path $Target 'python\genfill\build_info.json') ($info | ConvertTo-Json)
    $pluginPath = $Target
    $label = $git.Commit
    if (-not $label) { $label = 'no-git' }
    if ($git.Dirty) { $label = "$label (+ uncommitted changes)" }
    Write-Ok "Build $version  $label  $built"
} else {
    Write-Step 'Dev mode: Nuke will load GenFill straight from the repo'
    $stale = Join-Path $Src 'python\genfill\build_info.json'
    if (Test-Path $stale) { Remove-Item -LiteralPath $stale -Force }
    $pluginPath = $Src
    Write-Ok $pluginPath
}

# ---------------------------------------------------------------- init.py
Write-Step 'Updating your Nuke init.py'
$result = Set-GenFillInitBlock $pluginPath
Write-Ok "init.py: $($result.InitPy)"
if ($result.Backup) { Write-Ok "backup:  $($result.Backup)" }
foreach ($line in $result.Disabled) {
    Write-Warn "Disabled an older GenFill line so it doesn't load twice:  $line"
}

# ---------------------------------------------------------------- done
$running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^Nuke\d' })
Write-Host ''
Write-Host "Installed GenFill $version." -ForegroundColor Green
if ($running.Count -gt 0) {
    Write-Warn 'Nuke is open. Either restart it, or use GenFill > Developer > Reload GenFill'
    Write-Warn 'and reopen any GenerativeFill panels. (Reload only works if this install'
    Write-Warn 'location was already loaded in that Nuke session.)'
} else {
    Write-Ok 'Start Nuke and look for GenFill in the Nodes toolbar, or Tab-search "Generative Fill".'
}
Write-Ok 'To check the build: GenFill > Developer > Run Smoke Test'

if ($Launch) {
    if ($nukes.Count -eq 0) {
        Write-Warn 'Cannot launch: no Nuke found.'
    } else {
        $exe = $nukes[0].Exe
        Write-Step "Launching $($nukes[0].Name)"
        if ($NukeArgs) { Start-Process -FilePath $exe -ArgumentList $NukeArgs }
        else { Start-Process -FilePath $exe }
    }
}
exit 0
