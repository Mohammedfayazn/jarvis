<#
.SYNOPSIS
    Stop the background Jarvis.exe (it has no window to close).

    powershell -ExecutionPolicy Bypass -File scripts\stop_jarvis.ps1
#>
$running = Get-Process -Name Jarvis -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    Write-Host "Jarvis stopped."
} else {
    Write-Host "Jarvis is not running."
}
