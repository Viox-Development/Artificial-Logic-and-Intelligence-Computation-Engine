# ALICE Windows setup. Run it in the repo folder:
#   powershell -ExecutionPolicy Bypass -File install.ps1          # ALICE and the app; pick the rest in the app
#   powershell -ExecutionPolicy Bypass -File install.ps1 -All     # every optional feature, no wizard
# The installer (ALICE-Setup.exe, installer/alice.iss) runs it with -FromInstaller.
param([switch]$FromInstaller, [switch]$All, [switch]$Autostart)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot  # the installer may start it from another folder

trap {
    Write-Host ""
    Write-Host "Setup could not finish: $_" -ForegroundColor Red
    if ($FromInstaller) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

# Find Python. "python" is often the Microsoft Store stub, or PATH is stale right after winget, so look in the
# known install folders and the py launcher first.
function Test-Py([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre -c "import sys; print(sys.executable if sys.version_info[:2] >= (3, 10) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ([string]($out | Select-Object -Last 1)).Trim()
            if ($path -and (Test-Path $path)) { return $path }
        }
    } catch {}
    return $null
}

function Find-Python {
    $known = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    foreach ($k in $known) {
        if (Test-Path $k) { $r = Test-Py $k @(); if ($r) { return $r } }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("-3.11", "-3.12", "-3.10")) { $r = Test-Py "py" @($v); if ($r) { return $r } }
    }
    return (Test-Py "python" @())
}

$py = Find-Python
if (-not $py -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Python not found, installing Python 3.11 (winget)..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
    $py = Find-Python
}
if (-not $py) {
    throw "Python not found. Install Python 3.11 from python.org (tick 'Add python.exe to PATH'), reopen PowerShell and try again."
}
Write-Host "Python: $py"

# Auto-update uses git (ALICE pulls its own repo). Install it if missing; ALICE still runs without it.
if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Git (for auto-update)..." -ForegroundColor Yellow
    winget install -e --id Git.Git --scope user --accept-package-agreements --accept-source-agreements | Out-Null
}

if (-not (Test-Path ".venv")) { & $py -m venv .venv }
if (-not (Test-Path ".\.venv\Scripts\python.exe")) { throw "Could not create the virtual environment (.venv)." }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null

# ALICE and the app window (pywebview). Without pywebview the app opens in an Edge app window.
& .\.venv\Scripts\python.exe -m pip install -e ".[app]"
if ($LASTEXITCODE -ne 0) {
    & .\.venv\Scripts\python.exe -m pip install -e "."
    if ($LASTEXITCODE -ne 0) { throw "Package install failed, see the error above." }
    Write-Host "pywebview could not be installed; the app will open in an Edge window." -ForegroundColor Yellow
}

& .\.venv\Scriptslice.exe init
& .\.venv\Scriptslice.exe shortcut

if ($All) {
    # Everything at once (scripts, developers). Normally the app's setup wizard installs only what you pick.
    & .\.venv\Scripts\python.exe -m pip install -e ".[computer,browser,core,voice,home,voice-local]"
    if ($LASTEXITCODE -ne 0) { Write-Host "Some optional packages failed; ALICE still runs without them." -ForegroundColor Yellow }
    $chrome = Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
    $edge = Test-Path "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    if (-not ($chrome -or $edge)) { & .\.venv\Scripts\python.exe -m playwright install chromium }
    if ($Autostart) { & .\.venv\Scriptslice.exe core install }
} else {
    # The app opens the setup wizard on first start: language, a model, and the features you want.
    & .\.venv\Scripts\python.exe -c "from alice.features import set_wizard_state; set_wizard_state(wizard='pending')"
}

Write-Host ""
Write-Host "ALICE is installed." -ForegroundColor Green
if ($FromInstaller) {
    Write-Host "ALICE opens next and walks you through the rest."
    Start-Sleep -Seconds 2
} else {
    Write-Host "Start the app from the ALICE shortcut on your desktop, or in a terminal: .\.venv\Scriptslice.exe"
}
