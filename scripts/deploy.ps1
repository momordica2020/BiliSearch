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
    [switch]$SkipShards,
    [string]$Worktree = ".worktrees/gh-pages"
)
$ErrorActionPreference = "Stop"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { $env:PATH = "C:\Program Files\Git\cmd;" + $env:PATH }


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
$cfg = $null
if (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
}
if ($SkipBuild) {
    Write-Host "==> 跳过构建（使用现有 site/data）"
} elseif ($cfg) {
    Write-Host "==> 使用 deploy.config.json 构建参数"
    & python build_index.py $cfg.buildArgs
} else {
    & python build_index.py
}
if (-not $SkipBuild -and $LASTEXITCODE -ne 0) { throw "build_index.py 失败" }

if ($cfg -and (-not $SkipShards)) {
    Write-Host "==> 发布外部托管的分片组"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\publish_shards.ps1") -Remote $Remote
}
if ($cfg) {
    # 外部分片组不进入 gh-pages（无论是否重推分片都要剔除）
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

# 发布前检查构建锁：若另一个 build_index.py 正在运行，站点数据可能不完整，直接中止
$lockPath = Join-Path $root "site\data\.build.lock"
if (Test-Path $lockPath) {
    try {
        $fh = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $fh.Close()
    } catch {
        throw "检测到另一个构建正在进行（site/data/.build.lock 被占用），已中止发布以避免推送不完整索引"
    }
}
$metaCheck = Join-Path $root "site\data\meta.json"
if (-not (Test-Path $metaCheck)) {
    throw "site/data/meta.json 不存在，请先运行 build_index.py"
}

Write-Host "==> 同步 site/ 到工作树"
Get-ChildItem -LiteralPath $wt -Force | Where-Object { $_.Name -ne ".git" } | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}
# 排除构建临时文件（.build.lock / .tmp_post），避免占用或把临时文件带进站点
Get-ChildItem -LiteralPath (Join-Path $root "site") -Force | Where-Object { $_.Name -ne "data" } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $wt -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $wt "data") | Out-Null
Get-ChildItem -LiteralPath (Join-Path $root "site\data") -Force | Where-Object { $_.Name -notin @(".build.lock", ".tmp_post") } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $wt "data") -Recurse -Force
}

Push-Location $wt
git config http.postBuffer 1073741824
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

# 只统计真正进入 gh-pages 的部分（目录 + 本地分片组），外部分片组不算在 Pages 配额里
$pagesBytes = 0
foreach ($part in @("site\data\dir", "site\data\meta.json")) {
    $p = Join-Path $root $part
    if (Test-Path $p) {
        $pagesBytes += (Get-ChildItem $p -Recurse -File | Measure-Object Length -Sum).Sum
    }
}
$localShards = Join-Path $root "site\data\shards"
if (Test-Path $localShards) {
    $pagesBytes += (Get-ChildItem $localShards -Recurse -File | Measure-Object Length -Sum).Sum
}
$sizeMB = [math]::Round($pagesBytes / 1MB)
Write-Host "gh-pages 将发布约 ${sizeMB}MB（目录 + 本地分片组）"
if ($sizeMB -gt 900) {
    Write-Warning "gh-pages 体积 ${sizeMB}MB，接近 GitHub Pages 1GB 软上限！请考虑 --desc-len 0、提高 --recs-per-shard 或多仓库分片。"
} elseif ($sizeMB -gt 600) {
    Write-Warning "gh-pages 体积 ${sizeMB}MB，建议提高 --recs-per-shard 以缩小目录分片。"
}

Write-Host "完成。站点目录：$wt"
if (-not $Push) { Write-Host "加上 -Push 参数即可推送到 $Remote/gh-pages" }
