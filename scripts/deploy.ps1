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
    [switch]$SkipBuild,
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
$cfgPath = Join-Path $root "deploy.config.json"
if ($SkipBuild) {
    Write-Host "==> 跳过构建（使用现有 site/data）"
} elseif (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    Write-Host "==> 使用 deploy.config.json 构建参数"
    & python build_index.py $cfg.buildArgs
} else {
    & python build_index.py
}
if (-not $SkipBuild -and $LASTEXITCODE -ne 0) { throw "build_index.py 失败" }

if (Test-Path $cfgPath) {
    Write-Host "==> 发布外部托管的分片组"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\publish_shards.ps1") -Remote $Remote
    foreach ($b in $cfg.bases) {
        if (-not $b.url) { continue }
        $gdir = Join-Path $root "site\data\shards\g$($b.group)"
        if (Test-Path $gdir) {
            Remove-Item -LiteralPath $gdir -Recurse -Force
        }
    }
}

Write-Host "==> 准备 gh-pages 工作树: $wt"
if (-not (Test-Path (Join-Path $wt ".git"))) {
    if (git show-ref --verify --quiet "refs/heads/gh-pages") {
        git worktree add $wt gh-pages
    } else {
        git worktree add --detach $wt
        Push-Location $wt
        git switch --orphan gh-pages
        git rm -rf --quiet .
        Pop-Location
    }
}

Write-Host "==> 同步 site/ 到工作树"
Get-ChildItem -LiteralPath $wt -Force | Where-Object { $_.Name -ne ".git" } | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}
Copy-Item -Path (Join-Path $root "site\*") -Destination $wt -Recurse -Force

Push-Location $wt
git config http.postBuffer 524288000
# 快照式发布：每次生成"单一根提交"替换 gh-pages 历史，
# 避免 git 历史随索引增长无限膨胀（此前已因此涨到 1.7GB）
if ((git branch --show-current) -ne "_snapshot") {
    git branch -D _snapshot
    git checkout --orphan _snapshot
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "创建快照分支失败" }
}
git add -A
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "git add 失败" }
git -c user.name="BiliSearch Bot" -c user.email="bot@localhost" `
    commit -m "site: update $(Get-Date -Format 'yyyy-MM-dd HH:mm')" --allow-empty
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "git commit 失败" }
git branch -f gh-pages HEAD
git checkout gh-pages
git branch -D _snapshot
if ($Push) {
    Write-Host "==> 推送 $Remote/gh-pages（快照式，历史仅 1 个提交）"
    git push --force $Remote gh-pages
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "git push 失败（请检查 GitHub 凭据/网络）" }
}
Pop-Location

$dataSize = (Get-ChildItem (Join-Path $root "site\data") -Recurse -File | Measure-Object Length -Sum).Sum
$sizeMB = [math]::Round($dataSize / 1MB)
if ($sizeMB -gt 900) {
    Write-Warning "索引已达 ${sizeMB}MB，接近 GitHub Pages 1GB 软上限！请考虑 --desc-len 0 或多仓库分片。"
} elseif ($sizeMB -gt 300) {
    Write-Warning "索引 ${sizeMB}MB，推送体积较大，建议提高 --sync-minutes 或使用 --desc-len 0。"
}

Write-Host "完成。站点目录：$wt"
if (-not $Push) { Write-Host "加上 -Push 参数即可推送到 $Remote/gh-pages" }
