<#
.SYNOPSIS
    Put a "Jarvis" shortcut on the Desktop that starts dist\Jarvis\Jarvis.exe.

.DESCRIPTION
    Double-clicking it starts Jarvis awake and opens the HUD. If Jarvis is
    already running (e.g. from Startup), it just opens the HUD instead of
    starting a second copy.

    powershell -ExecutionPolicy Bypass -File scripts\create_desktop_shortcut.ps1
#>
param([string]$Exe)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $root "dist\Jarvis\Jarvis.exe" }
$Exe = [System.IO.Path]::GetFullPath($Exe)
if (-not (Test-Path $Exe)) { throw "$Exe not found - run scripts\build_exe.ps1 first." }

# GetFolderPath follows OneDrive-redirected Desktops, unlike $HOME\Desktop
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "Jarvis.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $Exe
$shortcut.WorkingDirectory = Split-Path $Exe
$shortcut.Description = "Jarvis voice assistant"
$shortcut.IconLocation = "$Exe,0"
$shortcut.Save()

Write-Host "Desktop shortcut: $link -> $Exe"
