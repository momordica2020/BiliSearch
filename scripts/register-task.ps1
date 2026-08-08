<#
.SYNOPSIS
注册 Windows 计划任务：定期爬取 B 站元数据并构建索引。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -IntervalHours 6 -UseVenv
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -IntervalHours 6 -UseVenv -Deploy
#>
param(
    [string]$TaskName = "BiliSearch-Crawl",
    [int]$IntervalHours = 6,
    [int]$Limit = 300,
    [string]$DataDir = "data",
    [switch]$UseVenv,
    [switch]$Deploy
)
$ErrorActionPreference = "Stop"
$root = (Get-Location).Path

if ($UseVenv) {
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { throw "未找到虚拟环境: $py（先执行 python -m venv .venv）" }
} else {
    $py = (Get-Command python).Source
}

$crawlArgs = "-m crawler --mode crawl --build --data-dir `"$DataDir`" --limit $Limit"
$actions = @(
    (New-ScheduledTaskAction -Execute $py -Argument $crawlArgs -WorkingDirectory $root)
)
if ($Deploy) {
    $deployArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\deploy.ps1`" -Push"
    $actions += New-ScheduledTaskAction -Execute "powershell.exe" -Argument $deployArgs -WorkingDirectory $root
}
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $TaskName -Action $actions -Trigger $trigger `
    -Settings $settings -Description "BiliSearch: 定时爬取 B 站元数据并构建离线索引" -Force

Write-Host "已注册计划任务 $TaskName"
Write-Host "  命令: $py $crawlArgs"
if ($Deploy) { Write-Host "  附加动作: 发布 site/ 到 gh-pages 分支" }
Write-Host "  间隔: 每 $IntervalHours 小时（可在任务计划程序中调整）"
