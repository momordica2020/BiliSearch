<#
.SYNOPSIS
注册 Windows 计划任务：定期爬取 B 站元数据并构建索引。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -IntervalHours 6 -UseVenv
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -IntervalHours 6 -UseVenv -Deploy
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Mode burst -Popular 200 -Ranking 0,1,4,36 -Workers 6 -UseVenv -Deploy
#>
param(
    [string]$TaskName = "BiliSearch-Crawl",
    [int]$IntervalHours = 6,
    [int]$Limit = 300,
    [ValidateSet("crawl", "burst", "roam")]
    [string]$Mode = "crawl",
    [int]$Popular = 0,
    [string]$Ranking = "",
    [int]$Workers = 4,
    [double]$IntervalSeconds = 1.2,
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

if ($Mode -eq "burst") {
    $crawlArgs = "-m crawler --mode burst --build --data-dir `"$DataDir`" --workers $Workers --interval $IntervalSeconds"
    if ($Popular -gt 0) { $crawlArgs += " --popular $Popular" }
    if ($Ranking) { $crawlArgs += " --ranking `"$Ranking`"" }
} elseif ($Mode -eq "roam") {
    $crawlArgs = "-m crawler --mode roam --build --data-dir `"$DataDir`" --limit $Limit --workers $Workers --interval $IntervalSeconds --roam-jump 0.3 --fanout 2 --jump-sources aid,precious,series,popular"
} else {
    $crawlArgs = "-m crawler --mode crawl --build --data-dir `"$DataDir`" --limit $Limit"
}
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
