# =============================================================================
# SciDirector —— 本地开发环境变量（Windows / PowerShell）
# -----------------------------------------------------------------------------
# 用法（每次开新终端时执行一次）：
#     . .\scripts\dev-env.ps1
#
# 作用：把 Go 的缓存与 Python 的额外依赖固定在仓库内，
#       避免污染全局环境，也让受限环境（沙箱）下一切可写可用。
#
# 重要：本脚本只设置环境变量，不修改任何全局状态。
# =============================================================================

$ErrorActionPreference = 'Stop'

# 仓库根目录 = 本脚本所在目录的上一级。
$RepoRoot = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------------------
# Go：把 GOPATH/GOMODCACHE/GOCACHE 固定到仓库内
# ---------------------------------------------------------------------------
# 这样做的另一个好处：CI 与本地可以共享完全一致的依赖快照。
$env:GOPATH = Join-Path $RepoRoot '.gocache\gopath'
$env:GOMODCACHE = Join-Path $env:GOPATH 'pkg\mod'
$env:GOCACHE = Join-Path $RepoRoot '.gocache\build'
$env:GOTELEMETRY = 'off'
$env:GOTELEMETRYDIR = Join-Path $RepoRoot '.gocache\telemetry'
$env:GOFLAGS = '-mod=mod'
$env:PATH = (Join-Path $env:GOPATH 'bin') + ';' + $env:PATH

# ---------------------------------------------------------------------------
# Python：把仓库内的 .pylibs 追加到模块搜索路径的**末尾**
# ---------------------------------------------------------------------------
# .pylibs 用于存放不便于装进全局环境的依赖（例如受限环境下的 grpcio）。
#
# 顺序很关键：必须放在**最后**。
# .pylibs 里可能包含 typing_extensions 这类被其他库深度依赖的包，
# 若把它放在前面，它会遮蔽 conda/venv 中版本更完整的同名包，
# 表现为 pydantic 导入时莫名其妙的 "cannot import name"（本项目已踩过一次）。
$Pylibs = Join-Path $RepoRoot '.pylibs'
$AiRoot = Join-Path $RepoRoot 'ai'
$env:PYTHONPATH = "$AiRoot;$env:PYTHONPATH"
if (Test-Path $Pylibs) {
    $env:PYTHONPATH = "$env:PYTHONPATH;$Pylibs"
}

# 中文输出在 Windows 控制台的乱码问题：显式指定 UTF-8。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

# ---------------------------------------------------------------------------
# 本地开发默认值
# ---------------------------------------------------------------------------
$env:SCID_ENV = 'dev'
$env:SCID_LOG_LEVEL = 'debug'
# 未配置密钥时自动进入 mock 模式，保证零配置可跑通全流程。
if (-not $env:OPENAI_API_KEY) { $env:SCID_LLM_PROVIDER = 'mock' }
$env:SCID_REDIS_ADDR = 'localhost:6379'
$env:SCID_AI_GRPC_ADDR = 'localhost:50051'
$env:SCID_MEDIA_WORK_DIR = Join-Path $RepoRoot '.data\work'

# pip 的临时目录：受限环境下系统临时目录可能不可写，指向仓库内更可靠。
$env:TEMP = Join-Path $RepoRoot '.tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null

Write-Host '[dev-env] 环境已就绪' -ForegroundColor Green
Write-Host "  RepoRoot   = $RepoRoot"
Write-Host "  GOPATH     = $env:GOPATH"
Write-Host "  PYTHONPATH = $env:PYTHONPATH"
Write-Host "  LLM        = $env:SCID_LLM_PROVIDER"
