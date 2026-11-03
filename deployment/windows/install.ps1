# Installs or updates the Tapo energy collector on Windows 10/11 (secondary option; Linux is the primary target).
# Run in an elevated PowerShell from the repository folder:
#   powershell -ExecutionPolicy Bypass -File deployment\windows\install.ps1
# Creates a scheduled task "TapoEnergyCollector" that starts at boot as SYSTEM and restarts after crashes.
# Configuration, secrets and the local buffer live in C:\ProgramData\TapoCollector and are kept on updates.
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Root = Join-Path $env:ProgramData 'TapoCollector'
$Venv = Join-Path $Root 'venv'
$Config = Join-Path $Root 'config.yaml'
$EnvFile = Join-Path $Root 'collector.env'
$TaskName = 'TapoEnergyCollector'

Write-Host "==> Checking Python 3.11+"
$py = (Get-Command py -ErrorAction SilentlyContinue)
if (-not $py) { throw 'Python launcher (py) not found. Install Python 3.11+ from https://www.python.org/downloads/ (tick "Add python.exe to PATH").' }
& py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }

Write-Host "==> Installing the application in $Venv"
New-Item -ItemType Directory -Force -Path $Root | Out-Null
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) { & py -3 -m venv $Venv }
& (Join-Path $Venv 'Scripts\python.exe') -m pip install --quiet --upgrade pip
& (Join-Path $Venv 'Scripts\pip.exe') install --quiet -r (Join-Path $Repo 'requirements.lock')
& (Join-Path $Venv 'Scripts\pip.exe') install --quiet --no-deps --force-reinstall $Repo
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

Write-Host "==> Configuration in $Root"
if (-not (Test-Path $Config)) {
  # Relative paths in the example (data/, logs/) resolve next to this file: C:\ProgramData\TapoCollector\data and \logs.
  Copy-Item (Join-Path $Repo 'config.example.yaml') $Config
  Write-Host "Created $Config - EDIT IT (endpoint_url and devices)."
}
if (-not (Test-Path $EnvFile)) {
  Copy-Item (Join-Path $Repo '.env.example') $EnvFile
  Write-Host "Created $EnvFile - PUT THE SECRETS IN IT."
}
# Secrets: only SYSTEM and Administrators may read the folder's secret file.
icacls $EnvFile /inheritance:r /grant:r 'SYSTEM:(F)' 'Administrators:(F)' | Out-Null

Write-Host "==> Registering scheduled task $TaskName"
$exe = Join-Path $Venv 'Scripts\tapo-collector.exe'
$action = New-ScheduledTaskAction -Execute $exe -Argument "--config `"$Config`" --env-file `"$EnvFile`" run" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host @"

Installed. Next steps:
  1. notepad $EnvFile          (TAPO_USERNAME, TAPO_PASSWORD, ENERGY_API_TOKEN)
  2. notepad $Config           (upload.endpoint_url and your plugs)
  3. & '$exe' --config '$Config' --env-file '$EnvFile' check-config
  4. & '$exe' --config '$Config' --env-file '$EnvFile' test-device --device <id>
  5. Start-ScheduledTask $TaskName

Commands: Start-ScheduledTask / Stop-ScheduledTask / Get-ScheduledTaskInfo $TaskName
Status:   & '$exe' --config '$Config' --env-file '$EnvFile' status
Logs:     $Root\logs\collector.log
Note: Stop-ScheduledTask ends the process immediately; readings are committed to SQLite as they are taken, so nothing
already collected is lost, but the in-flight upload is repeated at the next start (the website ignores duplicates).
"@
