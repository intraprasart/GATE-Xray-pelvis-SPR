# Auto-start SPR Monitor at login (24/7 queue watch board).
# ASCII-only on purpose: Windows PowerShell 5.1 mis-tokenizes a BOM-less
# UTF-8 script as the legacy codepage, so non-ASCII here breaks parsing.
$bat = Join-Path $PSScriptRoot "run_monitor.bat"
if (-not (Test-Path $bat)) { throw "not found: $bat" }
$vbs = Join-Path ([Environment]::GetFolderPath('Startup')) "SPR-Monitor.vbs"
"' Launch SPR Monitor at login (hidden cmd window; browser opens itself)`r`n" +
"CreateObject(""Wscript.Shell"").Run """"""$bat"""""", 0, False" |
    Set-Content -Encoding ASCII $vbs
Write-Host "Installed SPR Monitor auto-start: $vbs"
Write-Host "Start now:  Start-Process '$bat'"
