param(
    [Parameter(Mandatory = $true)]
    [string]$SdkRoot
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$sdkCoreDir = Join-Path $SdkRoot 'Samples/TypeScript/Demo/public/Core'
$coreSource = Join-Path $sdkCoreDir 'live2dcubismcore.min.js'
$licenseSource = Join-Path $sdkCoreDir 'LICENSE.md'
if (-not (Test-Path -LiteralPath $coreSource -PathType Leaf)) {
    throw "未找到 SDK Core：$coreSource"
}
if (-not (Test-Path -LiteralPath $licenseSource -PathType Leaf)) {
    throw "未找到 SDK Core 许可文件：$licenseSource"
}

$runtimeDir = Join-Path $repoRoot '.data/live2d-core'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$coreTarget = Join-Path $runtimeDir 'live2dcubismcore.min.js'
Copy-Item -LiteralPath $coreSource -Destination $coreTarget -Force
Copy-Item -LiteralPath $licenseSource -Destination (Join-Path $runtimeDir 'LICENSE.md') -Force
$sourceHash = (Get-FileHash -LiteralPath $coreSource -Algorithm SHA256).Hash
$targetHash = (Get-FileHash -LiteralPath $coreTarget -Algorithm SHA256).Hash
if ($sourceHash -ne $targetHash) {
    throw 'Core 复制后的 SHA-256 不一致'
}
Write-Output "Core 已安装并校验：$coreTarget"
Write-Output "本地 SCID_LIVE2D_CORE_PATH=$coreTarget"
