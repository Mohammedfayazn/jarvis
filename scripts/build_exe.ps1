<#
.SYNOPSIS
    Build Jarvis into a windowless app: dist\Jarvis\Jarvis.exe

.DESCRIPTION
    Installs PyInstaller into the project's .venv if needed, stops a running
    Jarvis.exe (it locks its own files), builds from jarvis.spec, then runs
    the new exe's --self-test and prints the result.

    Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#>
param([switch]$SkipSelfTest)

# "Continue", not "Stop": Windows PowerShell 5.1 turns anything a native
# program writes to stderr (PyInstaller logs there) into a fatal error.
# Native tools are judged by $LASTEXITCODE below instead.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No virtual environment at $python - create .venv and install requirements.txt first."
}

# 1. PyInstaller
& $python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..."
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv pip install --python $python pyinstaller
    } else {
        & $python -m pip install pyinstaller
    }
    if ($LASTEXITCODE -ne 0) { throw "Installing PyInstaller failed." }
}

# 2. A running Jarvis.exe keeps its files locked, so the build could not replace them
$running = Get-Process -Name Jarvis -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Stopping the running Jarvis.exe so it can be rebuilt..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# 3. Build
Write-Host "Building (this takes a few minutes - PyTorch is large)..."
& $python -m PyInstaller jarvis.spec --noconfirm --clean --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
$exe = Join-Path $root "dist\Jarvis\Jarvis.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing." }
$sizeMb = [math]::Round(((Get-ChildItem (Split-Path $exe) -Recurse | Measure-Object Length -Sum).Sum / 1MB))
Write-Host "Built $exe (folder: $sizeMb MB)"

# 4. Self-test. The exe has no console, so its output goes to the log file.
if (-not $SkipSelfTest) {
    $log = Join-Path $env:LOCALAPPDATA "Jarvis\jarvis.log"
    $before = 0
    if (Test-Path $log) { $before = (Get-Item $log).Length }
    Write-Host "Running the new exe's self-test..."
    $proc = Start-Process -FilePath $exe -ArgumentList "--self-test" -Wait -PassThru
    if (Test-Path $log) {
        $bytes = [System.IO.File]::ReadAllBytes($log)
        if ($bytes.Length -gt $before) {
            Write-Host ([System.Text.Encoding]::UTF8.GetString($bytes, $before, $bytes.Length - $before))
        }
    }
    if ($proc.ExitCode -ne 0) { throw "Self-test failed (exit code $($proc.ExitCode)) - see $log" }
}
Write-Host "Done."
