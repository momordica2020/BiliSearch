<#
.SYNOPSIS
构建索引并把 site/ 发布到 gh-pages 分支（GitHub Pages）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Push
#>
param(
    [string]$Remote = "origin",
    [switch]$Push,
    [string]$Worktree = ".worktrees/gh-pages"
)
$ErrorActionPreference = "Stop"

$root = git rev-parse --show-toplevel
if (-not $root) { throw "当前目录不在 git 仓库中" }
$root = (Resolve-Path $root).Path
$wt = [System.IO.Path]::GetFullPath((Join-Path $root $Worktree))

# 安全校验：工作树必须位于仓库内部
$sep = [System.IO.Path]::DirectorySeparatorChar
if (-not $wt.StartsWith($root + $sep)) {
    throw "工作树路径不合法（必须在仓库内）: $wt"
}

Write-Host "==> 构建索引"
& python build_index.py
if ($LASTEXITCODE -ne 0) { throw "build_index.py 失败" }

Write-Host "==> 准备 gh-pages 工作树: $wt"
if (-not (Test-Path (Join-Path $wt ".git"))) {
    if (git show-ref --verify --quiet "refs/heads/gh-pages") {
        git worktree add $wt gh-pages
    } else {
        git worktree add --detach $wt
        Push-Location $wt
        git switch --orphan gh-pages
        git rm -rf --quiet . 2>$null
        Pop-Location
    }
}

Write-Host "==> 同步 site/ 到工作树"
Get-ChildItem -LiteralPath $wt -Force | Where-Object { $_.Name -ne ".git" } | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}
Copy-Item -Path (Join-Path $root "site\*") -Destination $wt -Recurse -Force

Push-Location $wt
git add -A
git -c user.name="BiliSearch Bot" -c user.email="bot@localhost" `
    commit -m "site: update $(Get-Date -Format 'yyyy-MM-dd HH:mm')" --allow-empty
if ($Push) {
    Write-Host "==> 推送 $Remote/gh-pages"
    git push $Remote gh-pages --force
}
Pop-Location

Write-Host "完成。站点目录：$wt"
if (-not $Push) { Write-Host "加上 -Push 参数即可推送到 $Remote/gh-pages" }
