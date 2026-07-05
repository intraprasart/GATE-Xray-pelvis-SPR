# ให้ SPR worker รันอัตโนมัติตอน login
# วิธีหลัก: Task Scheduler (ต้องรัน PowerShell แบบ Administrator)
# วิธีสำรอง: Startup folder + VBS (ไม่ต้องใช้สิทธิ์ admin, หน้าต่างซ่อน)
$bat = Join-Path $PSScriptRoot "run_worker.bat"
if (-not (Test-Path $bat)) { throw "ไม่พบ $bat" }

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
    Write-Host "ลงทะเบียน Task Scheduler 'SPR-Worker' แล้ว"
} catch {
    $vbs = Join-Path ([Environment]::GetFolderPath('Startup')) "SPR-Worker.vbs"
    "' Auto-start SPR worker (hidden window) at login`r`n" +
    "CreateObject(""Wscript.Shell"").Run """"""$bat"""""", 0, False" |
        Set-Content -Encoding ASCII $vbs
    Write-Host "ไม่มีสิทธิ์ Task Scheduler — สร้าง Startup script แทน: $vbs"
}
Write-Host "worker จะเริ่มเองในการ login ครั้งถัดไป"
