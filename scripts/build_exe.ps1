# CodeReader 一键打包脚本
# 产物：release\CodeReader\  （整个文件夹即为离线部署包）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#   可选参数：
#     -SkipFrontend   跳过前端构建（frontend\dist 已是最新时）
#     -SkipModel      不拷贝模型文件（部署包体积小，模型另行拷贝）
#     -SkipRuntime    不拷贝 llama.cpp（部署包体积小，运行时另行下载）

param(
    [switch]$SkipFrontend,
    [switch]$SkipModel,
    [switch]$SkipRuntime
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $root "release"))
$out = [System.IO.Path]::GetFullPath((Join-Path $releaseRoot "CodeReader"))
$releasePrefix = $releaseRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
    [System.IO.Path]::DirectorySeparatorChar
if (-not $out.StartsWith($releasePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "拒绝清理非 release 目录: $out"
}

Write-Host "== CodeReader 打包 ==" -ForegroundColor Cyan
Write-Host "项目根目录: $root"

$pythonVersion = uv run --frozen python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.13") {
    throw "需要 Python 3.13 与已同步的 uv.lock"
}
$nodeMajor = [int]((node --version).TrimStart('v').Split('.')[0])
if ($nodeMajor -lt 24) { throw "需要 Node.js 24 LTS" }

# 1. 前端构建
if (-not $SkipFrontend) {
    Write-Host "`n[1/4] 构建前端…" -ForegroundColor Cyan
    Push-Location (Join-Path $root "frontend")
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
    Pop-Location
} else {
    Write-Host "`n[1/4] 跳过前端构建" -ForegroundColor Yellow
}
if (-not (Test-Path (Join-Path $root "frontend\dist\index.html"))) {
    throw "缺少 frontend\dist，请先构建前端"
}

# 2. PyInstaller 打包 exe
Write-Host "`n[2/4] PyInstaller 打包…" -ForegroundColor Cyan
if (Test-Path -LiteralPath $out) {
    Remove-Item -LiteralPath $out -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $out | Out-Null
Push-Location (Join-Path $root "backend")
uv run --frozen pyinstaller --noconfirm --clean --onefile run.py --name CodeReader `
    --distpath $out `
    --workpath (Join-Path $root "backend\build") `
    --specpath (Join-Path $root "backend") `
    --add-data "$root\frontend\dist;static" `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.loops.asyncio `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.http.h11_impl `
    --hidden-import uvicorn.protocols.websockets.auto `
    --hidden-import uvicorn.lifespan.on `
    --hidden-import anyio._backends._asyncio
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
Pop-Location

# 3. 拷贝运行时资源
Write-Host "`n[3/4] 组装部署目录…" -ForegroundColor Cyan
Copy-Item (Join-Path $root "config.json") $out -Force

$llamaOut = Join-Path $out "llama"
New-Item -ItemType Directory -Force $llamaOut | Out-Null
if (-not $SkipRuntime) {
    Copy-Item (Join-Path $root "llama\llama-server.exe") $llamaOut -Force
    Copy-Item (Join-Path $root "llama\*.dll") $llamaOut -Force
} else {
    Copy-Item (Join-Path $root "packaging\llama-runtime-README.txt") (Join-Path $llamaOut "README.txt") -Force
    Write-Host "跳过 llama-server 运行时（请按 README.txt 下载）" -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force (Join-Path $out "data") | Out-Null

$modelsOut = Join-Path $out "models"
New-Item -ItemType Directory -Force $modelsOut | Out-Null
if (-not $SkipModel) {
    Write-Host "拷贝 models\ 下全部 GGUF（当前约 10 GB，需要一点时间）…"
    Copy-Item (Join-Path $root "models\*.gguf") $modelsOut -Force
} else {
    Copy-Item (Join-Path $root "packaging\models-README.txt") (Join-Path $modelsOut "README.txt") -Force
    Write-Host "跳过模型拷贝（请按 README.txt 下载）" -ForegroundColor Yellow
}

Copy-Item (Join-Path $root "README.md") (Join-Path $out "使用说明.md") -Force

$checksumPath = Join-Path $out "SHA256SUMS.txt"
Get-ChildItem $out -Recurse -File |
    Where-Object { $_.FullName -ne $checksumPath } |
    ForEach-Object {
        $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName
        $relative = $_.FullName.Substring($out.Length + 1)
        "$($hash.Hash.ToLowerInvariant())  $relative"
    } | Set-Content -Encoding UTF8 $checksumPath

# 4. 汇总
Write-Host "`n[4/4] 完成！" -ForegroundColor Green
$size = (Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum / 1GB
Write-Host ("部署包: {0}  (共 {1:N2} GB)" -f $out, $size)
Write-Host "把整个 CodeReader 文件夹拷贝到内网机器，双击 CodeReader.exe 即可使用。"
