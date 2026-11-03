# Removes the scheduled task and the virtual environment. Keeps configuration, secrets and the buffer
# in C:\ProgramData\TapoCollector unless -Purge is given.
#Requires -RunAsAdministrator
param([switch]$Purge)
$Root = Join-Path $env:ProgramData 'TapoCollector'
Stop-ScheduledTask -TaskName 'TapoEnergyCollector' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'TapoEnergyCollector' -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root 'venv') -ErrorAction SilentlyContinue
if ($Purge) { Remove-Item -Recurse -Force $Root; Write-Host 'Removed everything, including undelivered readings.' }
else { Write-Host "Removed the task and application; kept $Root (use -Purge to delete it)." }
