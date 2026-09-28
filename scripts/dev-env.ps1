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

# 本脚本内部用 Stop，保证自己的错误立刻暴露；
# 但**必须在结束时把调用者的设置还原回去**。
#
# 为什么：本脚本是用 `. .\scripts\dev-env.ps1` source 进当前会话的，
# 直接留下 $ErrorActionPreference='Stop' 会让**所有后续原生命令**遭殃 ——
# PowerShell 会把它们写到 stderr 的任何内容包装成 NativeCommandError 并当成
# 终止性错误。实测后果：
#   * `go build` 首次下载依赖时打印 `go: downloading ...` 到 stderr → 构建中断
#   * `git fetch` 的进度、`npm install` 的 warning 同理
# 而这些都只是进度信息，不是错误。
$__ScidPrevEAP = $ErrorActionPreference
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
# 顺序很重要：**先加载根目录 .env，再补默认值**。
#
# 根目录的 .env 只在 `docker compose` 插值时生效 —— Go 不读 .env 文件、
# Python 的 env_file 又是相对当前目录解析的（找的是 ai/.env），
# 于是"照着 .env.example 配好密钥、再跑本地进程"会**静默进入 mock 模式**，
# 内容全是占位且没有任何报错。这一步把那一环补上（语义见 load-env.ps1）。
. (Join-Path $PSScriptRoot 'load-env.ps1')

# 下面一律"未设置才填"，因此 .env 与显式环境变量都优先于这里的默认值。
if (-not $env:SCID_ENV) { $env:SCID_ENV = 'dev' }
if (-not $env:SCID_LOG_LEVEL) { $env:SCID_LOG_LEVEL = 'debug' }
# 一个密钥都没有时进 mock 模式，保证零配置能跑通全流程。
if (-not $env:SCID_LLM_PROVIDER) { $env:SCID_LLM_PROVIDER = 'mock' }
if (-not $env:SCID_REDIS_ADDR) { $env:SCID_REDIS_ADDR = 'localhost:6379' }
if (-not $env:SCID_AI_GRPC_ADDR) { $env:SCID_AI_GRPC_ADDR = 'localhost:50051' }

# ---------------------------------------------------------------------------
# LaTeX（MiKTeX / TeX Live）：manim 渲染数学公式的硬依赖
# ---------------------------------------------------------------------------
# 为什么非要有这一段：
# manim 用**裸命令名**调用 `latex` 与 `dvisvgm`（见 manim/utils/tex_file_writing.py
# 的 convert_to_svg），完全靠 PATH 解析。而 Windows 在进程**启动那一刻**就把环境块
# 固定住了 —— 装完 MiKTeX 往 User PATH 里写一行，**已经开着的终端**以及它的所有
# 子进程都看不到。于是 manim 抛：
#     "Your installation does not support converting .dvi files to SVG.
#      Consider updating dvisvgm to at least version 2.4."
# 这条信息把人和 AI 都往"dvisvgm 版本太老"的方向带，而真实原因只是 PATH 里没有它。
# 本项目为此浪费了一整轮排查，所以在这里主动解析并注入：装完 MiKTeX 直接双击
# dev.bat 就能跑，不需要重启终端、更不需要注销登录。
#
# 解析顺序：SCID_TEX_BIN（.env 或环境变量）> 注册表 PATH > 常见安装位置。
$TexBin = $env:SCID_TEX_BIN
if ($TexBin -and -not (Test-Path -LiteralPath (Join-Path $TexBin 'latex.exe'))) {
    Write-Host "[dev-env] 警告：SCID_TEX_BIN=$TexBin 下没有 latex.exe，改为自动探测。" -ForegroundColor Yellow
    $TexBin = $null
}
if (-not $TexBin) {
    $texCandidates = @()
    # 注册表里的 PATH 是"用户刚装好"最真实的记录：当前会话可能还是旧环境块，
    # 但注册表已经被安装程序更新过了。
    foreach ($scope in @(
            'HKCU:\Environment',
            'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment')) {
        $regPath = (Get-ItemProperty -Path $scope -Name Path -ErrorAction SilentlyContinue).Path
        if ($regPath) {
            $texCandidates += @($regPath -split ';' | Where-Object { $_ -match 'MiKTeX|texlive' })
        }
    }
    $texCandidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\MiKTeX\miktex\bin\x64'),
        'C:\Program Files\MiKTeX\miktex\bin\x64',
        'C:\texlive\2025\bin\windows',
        'C:\texlive\2024\bin\windows'
    )
    foreach ($cand in $texCandidates) {
        if ([string]::IsNullOrWhiteSpace($cand)) { continue }
        $cand = $cand.Trim().TrimEnd('\')
        if (Test-Path -LiteralPath (Join-Path $cand 'latex.exe')) { $TexBin = $cand; break }
    }
}
if ($TexBin) {
    # 回写环境变量：沙箱等子进程据此也能定位 TeX，不必重复探测。
    $env:SCID_TEX_BIN = $TexBin
    if (-not (($env:PATH -split ';') -contains $TexBin)) {
        $env:PATH = $TexBin + ';' + $env:PATH
    }
}
$script:ScidTexBin = $TexBin

# 工作目录一律解析成**绝对路径**。
#
# 踩过的坑：Python 的 sandbox_work_dir 默认是相对路径 `./.data/sandbox`，
# 于是它跟着**启动时的 cwd** 走 —— `cd ai` 之后启动就落到 ai\.data\sandbox，
# 在仓库根启动就落到 .data\sandbox。产物因此分裂成两棵树，
# 清理与排查时极易找错地方。
#
# 注意这里不是"无条件覆盖"：用户或 .env 里显式配的值仍然生效，
# 只是把**相对路径**按仓库根展开成绝对路径 ——
# 显式配置应当被尊重，但它不该因为 cwd 不同而指向不同的地方。
foreach ($name in 'SCID_MEDIA_WORK_DIR', 'SCID_SANDBOX_WORK_DIR', 'SCID_ARCHIVE_LOCAL_DIR') {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($value)) { continue }
    if (-not [System.IO.Path]::IsPathRooted($value)) {
        # GetFullPath 顺带把 `\.\`、`\..\` 规范化掉，否则 .env 里的
        # `./.data/work` 会拼出 `...\.\.data\work` 这种噪音路径。
        $value = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $value))
    }
    Set-Item -Path ('Env:' + $name) -Value $value
}
if (-not $env:SCID_MEDIA_WORK_DIR) { $env:SCID_MEDIA_WORK_DIR = Join-Path $RepoRoot '.data\work' }
if (-not $env:SCID_SANDBOX_WORK_DIR) { $env:SCID_SANDBOX_WORK_DIR = Join-Path $RepoRoot '.data\sandbox' }

# pip 的临时目录：受限环境下系统临时目录可能不可写，指向仓库内更可靠。
$env:TEMP = Join-Path $RepoRoot '.tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null

Write-Host '[dev-env] 环境已就绪' -ForegroundColor Green
Write-Host "  RepoRoot   = $RepoRoot"
Write-Host "  GOPATH     = $env:GOPATH"
Write-Host "  PYTHONPATH = $env:PYTHONPATH"
Write-Host "  LLM        = $env:SCID_LLM_PROVIDER"
if ($script:ScidTexBin) {
    Write-Host "  LaTeX      = $($script:ScidTexBin)" -ForegroundColor Green
} else {
    Write-Host '  LaTeX      = 未找到（manim 渲染公式会失败；装 MiKTeX 或设 SCID_TEX_BIN）' -ForegroundColor Yellow
}
if (Test-Path -LiteralPath (Join-Path $RepoRoot '.env')) {
    Write-Host "  .env       = 已加载（显式环境变量优先）" -ForegroundColor Green
}

# 还原调用者原来的 $ErrorActionPreference —— 见文件开头的原因说明。
$ErrorActionPreference = $__ScidPrevEAP
Remove-Variable __ScidPrevEAP -ErrorAction SilentlyContinue
