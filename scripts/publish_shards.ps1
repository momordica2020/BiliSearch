<#
.SYNOPSIS
把外部托管的分片组发布到各自分支/仓库（读取 deploy.config.json 的 bases）。
每个组用"快照式"单根提交强制替换目标分支历史，避免仓库膨胀。
#>
param([string]$Remote = "origin")
$ErrorActionPreference = "Stop"
$root = git rev-parse --show-toplevel
if (-not $root) { throw "当前目录不在 git 仓库中" }
$root = (Resolve-Path $root).Path
$cfgPath = Join-Path $root "deploy.config.json"
if (-not (Test-Path $cfgPath)) {
    Write-Host "没有 deploy.config.json，跳过分片发布"
    exit 0
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json

foreach ($b in $cfg.bases) {
    if (-not $b.url) { continue }
    $branch = $b.branch
    $group = $b.group
    $src = Join-Path $root "site\data\shards\g$group"
    if (-not (Test-Path $src)) { continue }
    $wt = Join-Path $root ".worktrees\$branch"

    if (-not (Test-Path (Join-Path $wt ".git"))) {
        if (git show-ref --verify --quiet "refs/heads/$branch") {
            git worktree add $wt $branch
        } else {
            git worktree add --detach $wt
            Push-Location $wt
            git switch --orphan $branch
            git rm -rf --quiet .
            Pop-Location
        }
    }
    Get-ChildItem -LiteralPath $wt -Force | Where-Object { $_.Name -ne ".git" } | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $wt "shards\g$group") | Out-Null
    Copy-Item -Path "$src\*" -Destination (Join-Path $wt "shards\g$group") -Recurse -Force

    Push-Location $wt
    git config http.postBuffer 524288000
    if ((git branch --show-current) -ne "_snapshot") {
        git branch -D _snapshot
        git checkout --orphan _snapshot
    }
    git add -A
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "git add 失败（组 $group）" }
    git -c user.name="BiliSearch Bot" -c user.email="bot@localhost" `
        commit -m "shards g${group}: $(Get-Date -Format 'yyyy-MM-dd HH:mm')" --allow-empty
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "git commit 失败（组 $group）" }
    git branch -f $branch HEAD
    git checkout $branch
    git branch -D _snapshot
    git push --force $Remote $branch
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "git push 失败（组 $group -> $Remote/$branch）" }
    Pop-Location
    Write-Host "已发布分片组 g$group -> $Remote/$branch"
}
