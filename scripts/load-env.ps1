# =============================================================================
# SciDirector —— 加载仓库根目录的 .env
# -----------------------------------------------------------------------------
# 为什么需要它：
#   根目录的 .env **只在 `docker compose` 插值时生效**。
#   本地跑进程时两侧都不读它：
#     * Go 根本不读 .env 文件，只读进程环境变量；
#     * Python 的 env_file=".env" 是**相对当前目录**解析的（找的是 ai/.env）。
#   结果是"照着 .env.example 配好、直接跑本地进程"会**静默进入 mock 模式** ——
#   内容全是占位，且没有任何报错。
#
#   本项目在 POSIX 侧由 scripts/load-env.sh 补上这一环（make dev-* 会调用它）。
#   本文件是它的 Windows 等价物，**语义完全对齐**，避免两个平台行为不一致。
#
# 语义（与 dotenv / compose 一致）：
#   1. **已在环境里的变量不覆盖** —— 于是 `set SCID_LLM_PROVIDER=deepseek` 这类
#      显式设置优先于文件，符合"显式 > 文件"的直觉。
#   2. 跳过空行与整行注释（以 # 开头）。
#   3. 只处理 KEY=VALUE，其余（误写的裸词）跳过，不让整个启动流程挂掉。
#   4. 去掉值两侧**成对**的引号：A="x y" 得到 `x y`。
#   5. 行内注释只在 `#` **前面有空白**时截断，且引号内的 # 一律保留：
#        A="值 # 不是注释"   -> 引号内的 # 是值的一部分
#        B=值   # 注释        -> 从 # 截断
#        C=ab#cd             -> 保留（密码里带 # 很常见）
#      这一条最容易漏。`.env.example` 里就有 `SCID_SANDBOX_MANIM_QUALITY=l   # l=480p`
#      这样的写法，不处理的话值会变成整行，pydantic 直接报 literal_error、服务起不来。
#
# 两种用法：
#   * PowerShell 里 source（推荐）：
#         . .\scripts\load-env.ps1
#     直接设置当前进程的环境变量。
#   * cmd 里取值（由 scripts\dev.bat 调用）：
#         powershell -NoProfile -File .\scripts\load-env.ps1 -Format cmd
#     向 stdout 打印 `set "KEY=VALUE"` 行，由调用方 for /f 执行。
#     只打印**本进程尚未设置**的变量，因此仍然满足"显式优先"。
# =============================================================================

[CmdletBinding()]
param(
    # ps  = 直接设置当前进程的环境变量（供 source）
    # cmd = 向 stdout 打印 set "K=V" 行（供 .bat 消费）
    [ValidateSet('ps', 'cmd')]
    [string]$Format = 'ps',

    # 允许指定别的文件；默认仓库根目录的 .env。
    [string]$Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-RepoRoot {
    # 本文件位于 <repo>\scripts\，上一级即仓库根。
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

if (-not $Path) {
    $Path = Join-Path (Get-RepoRoot) '.env'
}

if (-not (Test-Path -LiteralPath $Path)) {
    # 文件不存在时安静返回：CI 里就没有 .env。
    return
}

function ConvertFrom-DotEnvLine {
    <#
      解析一行 .env，返回 @{ Key = ...; Value = ... }；不是 KEY=VALUE 则返回 $null。
      规则见文件头。
    #>
    param([string]$Line)

    # 去掉 Windows 编辑器可能留下的行尾回车。
    $line = $Line.TrimEnd("`r", "`n")

    if ($line -eq '' -or $line.StartsWith('#')) { return $null }

    $idx = $line.IndexOf('=')
    if ($idx -lt 1) { return $null }

    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1)
    if ($key -eq '') { return $null }

    $trimmed = $val.TrimStart()

    if ($trimmed.Length -ge 2) {
        $first = $trimmed[0]
        if (($first -eq '"' -or $first -eq "'") -and $trimmed[$trimmed.Length - 1] -eq $first) {
            # 成对引号：去掉引号，内部内容**原样保留**（含 # 与行尾空白）。
            $val = $trimmed.Substring(1, $trimmed.Length - 2)
            return @{ Key = $key; Value = $val }
        }
    }

    # 未加引号：先按 "空白 + #" 截断行内注释，再去行尾空白。
    $hash = [regex]::Match($val, '\s#')
    if ($hash.Success) {
        $val = $val.Substring(0, $hash.Index)
    }
    return @{ Key = $key; Value = $val.TrimEnd() }
}

$applied = 0
foreach ($raw in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
    $entry = ConvertFrom-DotEnvLine -Line $raw
    if ($null -eq $entry) { continue }

    # 显式设置优先：本进程已有非空值就不动它。
    $existing = [Environment]::GetEnvironmentVariable($entry.Key)
    if (-not [string]::IsNullOrEmpty($existing)) { continue }

    if ($Format -eq 'cmd') {
        # 交给 .bat 去 set。用双引号包住整条 set，值里的空格不会被吃掉。
        Write-Output ('set "{0}={1}"' -f $entry.Key, $entry.Value)
    }
    else {
        Set-Item -Path ("Env:" + $entry.Key) -Value $entry.Value
    }
    $applied++
}

if ($Format -eq 'ps') {
    Write-Verbose ("已从 {0} 载入 {1} 个变量" -f $Path, $applied)
}
