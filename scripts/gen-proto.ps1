# =============================================================================
# SciDirector —— 由 proto 生成 Go + Python 的 gRPC 代码（Windows / PowerShell）
# -----------------------------------------------------------------------------
# 用法：
#     .\scripts\gen-proto.ps1
#
# 为什么还需要这个脚本，Makefile 里不是有 make proto 吗？
#   Makefile 依赖 GNU make 与 bash，Windows 原生环境通常都没有；
#   而契约生成是**每次改 proto 都要做的事**，本地必须有一条零依赖的路径。
#
# 支持两种 Python 生成方式（按可用性自动选择）：
#   1) grpc_tools.protoc —— 官方推荐，跨平台一致（需 pip install grpcio-tools）
#   2) 本地 protoc + grpc_python_plugin —— 无需安装 grpcio-tools 的退路
# =============================================================================

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$ProtoDir = Join-Path $RepoRoot 'proto'
$GoOut = Join-Path $RepoRoot 'backend\internal\pb'
$PyOut = Join-Path $RepoRoot 'ai\scidirector_ai\pb'

# 递归收集 proto 文件，并转成相对 -I 根的路径（protoc 要求）。
$Protos = Get-ChildItem -Recurse -Path $ProtoDir -Filter *.proto |
    ForEach-Object { (Resolve-Path -Relative $_.FullName).TrimStart('.\') -replace '\\', '/' }

if (-not $Protos) { throw "在 $ProtoDir 下没有找到任何 .proto 文件" }

Write-Host "[gen-proto] 发现 $($Protos.Count) 个 proto 文件" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 1) Go
# ---------------------------------------------------------------------------
$GoBin = Join-Path $env:GOPATH 'bin'
if (Test-Path $GoBin) { $env:PATH = "$GoBin;$env:PATH" }

foreach ($plugin in 'protoc-gen-go', 'protoc-gen-go-grpc') {
    if (-not (Get-Command $plugin -ErrorAction SilentlyContinue)) {
        Write-Host "[gen-proto] 安装 $plugin ..." -ForegroundColor Yellow
        $module = if ($plugin -eq 'protoc-gen-go') {
            'google.golang.org/protobuf/cmd/protoc-gen-go@latest'
        } else {
            'google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest'
        }
        & go install $module
    }
}

New-Item -ItemType Directory -Force -Path $GoOut | Out-Null
Write-Host '[gen-proto] 生成 Go 代码 ...' -ForegroundColor Cyan
& protoc -I proto `
    "--go_out=$GoOut" --go_opt=paths=source_relative `
    "--go-grpc_out=$GoOut" --go-grpc_opt=paths=source_relative `
    @Protos
if ($LASTEXITCODE -ne 0) { throw "Go 代码生成失败（exit=$LASTEXITCODE）" }

# ---------------------------------------------------------------------------
# 2) Python
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $PyOut | Out-Null

$UsedGRPCTools = $false
& python -c "import grpc_tools" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host '[gen-proto] 使用 grpc_tools.protoc 生成 Python 代码 ...' -ForegroundColor Cyan
    & python -m grpc_tools.protoc -I proto `
        "--python_out=$PyOut" "--pyi_out=$PyOut" "--grpc_python_out=$PyOut" `
        @Protos
    if ($LASTEXITCODE -ne 0) { throw "Python 代码生成失败（grpc_tools，exit=$LASTEXITCODE）" }
    $UsedGRPCTools = $true
}

if (-not $UsedGRPCTools) {
    # 退路：用系统 protoc 的内置 Python 生成器 + 独立的 grpc_python_plugin。
    # protoc 自带 --python_out/--pyi_out（无需插件），只有 gRPC stub 需要插件。
    $PluginPath = (Get-Command grpc_python_plugin.exe -ErrorAction SilentlyContinue).Source
    if (-not $PluginPath) {
        # 常见位置：Anaconda 的 Library\bin
        $Candidate = Join-Path (Split-Path (Get-Command python).Source -Parent) 'Library\bin\grpc_python_plugin.exe'
        if (Test-Path $Candidate) { $PluginPath = $Candidate }
    }
    if (-not $PluginPath) {
        throw @'
未找到 grpc_python_plugin.exe，且未安装 grpcio-tools。
请任选其一：
  * pip install grpcio-tools        （推荐，跨平台一致）
  * 安装带 grpc_python_plugin 的 protoc 发行版
'@
    }

    Write-Host "[gen-proto] 使用系统 protoc + $PluginPath 生成 Python 代码 ..." -ForegroundColor Yellow
    & protoc -I proto `
        "--plugin=protoc-gen-grpc_python=$PluginPath" `
        "--python_out=$PyOut" "--pyi_out=$PyOut" "--grpc_python_out=$PyOut" `
        @Protos
    if ($LASTEXITCODE -ne 0) { throw "Python 代码生成失败（protoc 退路，exit=$LASTEXITCODE）" }
}

Write-Host '[gen-proto] 完成' -ForegroundColor Green
Write-Host "  Go     -> $GoOut"
Write-Host "  Python -> $PyOut"
Write-Host '提示：生成物已入库，修改 proto 后请一并提交。'
