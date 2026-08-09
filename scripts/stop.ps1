<#
.SYNOPSIS
安全停止正在运行的 BiliSearch 爬虫（不依赖 Ctrl+C 能否送达）。
原理：写入 data\stop 标记文件，爬虫会在数秒内保存状态并退出。
#>
$root = Split-Path -Parent $PSScriptRoot
$stop = Join-Path $root "data\stop"
New-Item -ItemType File -Force -Path $stop | Out-Null
Write-Host "已写入停止指令：$stop"
Write-Host "爬虫将在数秒内保存状态并退出（若已退出则忽略）"
