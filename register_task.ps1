# --------------------------------------------------------------------
#  Register a Windows Scheduled Task that runs mailon sync every hour.
#
#  Usage (from an elevated PowerShell in the project folder):
#      powershell -ExecutionPolicy Bypass -File register_task.ps1
#
#  To remove later:
#      schtasks /Delete /TN "MailonSync" /F
# --------------------------------------------------------------------

$TaskName = "MailonSync"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatPath = Join-Path $ProjectDir "run_sync.bat"
$LogsDir = Join-Path $ProjectDir "logs"

if (-not (Test-Path $BatPath)) {
    Write-Error "run_sync.bat not found at $BatPath"
    exit 1
}

if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir | Out-Null
}

$Action = New-ScheduledTaskAction `
    -Execute $BatPath `
    -WorkingDirectory $ProjectDir

# Every hour, forever, starting now.
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))  # 10 years

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Run as the current interactive user (NOT SYSTEM) so that:
#   - agent-browser can launch Chrome in that user's profile
#   - DPAPI-protected secrets (if used) decrypt
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Updating existing task '$TaskName'..."
    Set-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
                      -Settings $Settings -Principal $Principal | Out-Null
} else {
    Write-Host "Creating task '$TaskName'..."
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
                           -Settings $Settings -Principal $Principal `
                           -Description "Hourly mailon.kr inbox backup" | Out-Null
}

Write-Host ""
Write-Host "Done. Task '$TaskName' is scheduled."
Write-Host "Next run: $(Get-Date).AddMinutes(2)"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  schtasks /Query /TN $TaskName /V /FO LIST   (show details)"
Write-Host "  schtasks /Run   /TN $TaskName               (run now)"
Write-Host "  schtasks /End   /TN $TaskName               (kill a running one)"
Write-Host "  schtasks /Delete /TN $TaskName /F           (remove task)"
Write-Host ""
Write-Host "Logs: $LogsDir\sync-YYYY-MM-DD.log"
