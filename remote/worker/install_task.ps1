# Auto-start the SPR worker at login.
# Primary: Task Scheduler (needs an elevated PowerShell).
# Fallback: Startup folder + VBS (no admin rights, hidden window).
# ASCII-only on purpose: Windows PowerShell 5.1 mis-tokenizes a BOM-less
# UTF-8 script as the legacy codepage, so non-ASCII here breaks parsing.
$bat = Join-Path $PSScriptRoot "run_worker.bat"
if (-not (Test-Path $bat)) { throw "not found: $bat" }

try {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c start `"SPR-Worker`" /min `"$bat`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    Register-ScheduledTask -TaskName "SPR-Worker" -Action $action `
        -Trigger $trigger -Settings $settings -Force -ErrorAction Stop | Out-Null
    Write-Host "Registered Task Scheduler task 'SPR-Worker'."
} catch {
    $vbs = Join-Path ([Environment]::GetFolderPath('Startup')) "SPR-Worker.vbs"
    "' Launch SPR worker at login (hidden window)`r`n" +
    "CreateObject(""Wscript.Shell"").Run """"""$bat"""""", 0, False" |
        Set-Content -Encoding ASCII $vbs
    Write-Host "No Task Scheduler rights - created Startup script: $vbs"
}
Write-Host "The worker will start automatically at next login."
