<#
.SYNOPSIS
    Start Jarvis automatically when you log on to Windows (or undo it).

.DESCRIPTION
    Adds a shortcut to your Startup folder (shell:startup) that runs
    dist\Jarvis\Jarvis.exe --start-asleep --no-browser:
      * asleep: only the offline "Hey Jarvis" detector listens - nothing is
        sent to Google until you say the wake word;
      * no browser tab pops up at logon - open http://127.0.0.1:8765/ (or
        double-click the Desktop shortcut) when you want the HUD.
    Needs no administrator rights. Also makes sure the API key file exists at
    %LOCALAPPDATA%\Jarvis\.env, copying the project's .env there if needed.

    powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove
#>
param([switch]$Remove, [string]$Exe)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath("Startup")
$link = Join-Path $startup "Jarvis.lnk"

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed $link - Jarvis won't start at logon." }
    else { Write-Host "Jarvis was not in the Startup folder." }
    return
}

if (-not $Exe) { $Exe = Join-Path $root "dist\Jarvis\Jarvis.exe" }
$Exe = [System.IO.Path]::GetFullPath($Exe)
if (-not (Test-Path $Exe)) { throw "$Exe not found - run scripts\build_exe.ps1 first." }

# The packaged app reads its API key from the data folder, never from the build.
$dataDir = Join-Path $env:LOCALAPPDATA "Jarvis"
$envTarget = Join-Path $dataDir ".env"
if (-not (Test-Path $envTarget)) {
    $envSource = Join-Path $root ".env"
    if (-not (Test-Path $envSource)) {
        throw "No API key: create $envTarget with GOOGLE_API_KEY=... (no project .env to copy)."
    }
    New-Item -ItemType Directory -Force $dataDir | Out-Null
    Copy-Item $envSource $envTarget
    Write-Host "Copied .env to $envTarget"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $Exe
$shortcut.Arguments = "--start-asleep --no-browser"
$shortcut.WorkingDirectory = Split-Path $Exe
$shortcut.Description = "Jarvis voice assistant (starts asleep - say 'Hey Jarvis')"
$shortcut.IconLocation = "$Exe,0"
$shortcut.WindowStyle = 7          # minimized - there is no window, but just in case
$shortcut.Save()

Write-Host "Startup shortcut: $link"
Write-Host "  -> $Exe --start-asleep --no-browser"
Write-Host "Jarvis will start asleep at your next logon. Undo with: install_startup.ps1 -Remove"
