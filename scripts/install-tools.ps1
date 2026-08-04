[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseDirectory,

    [switch]$InstallFfmpeg
)

$ErrorActionPreference = 'Stop'

$releasePath = (Resolve-Path -LiteralPath $ReleaseDirectory).Path
$collectorWheels = @(
    Get-ChildItem -LiteralPath $releasePath -Filter 'video_material_collector-*.whl'
)
$semvideoWheels = @(
    Get-ChildItem -LiteralPath $releasePath -Filter 'semvideo-*.whl'
)

if ($collectorWheels.Count -ne 1 -or $semvideoWheels.Count -ne 1) {
    throw 'ReleaseDirectory must contain exactly one wheel for each tool.'
}

$collectorWheel = $collectorWheels[0]
$semvideoWheel = $semvideoWheels[0]

$checksumPath = Join-Path $releasePath 'SHA256SUMS.txt'
if (-not (Test-Path -LiteralPath $checksumPath)) {
    throw 'SHA256SUMS.txt is required beside the release wheels.'
}

$expectedHashes = @{}
foreach ($line in Get-Content -LiteralPath $checksumPath) {
    if ($line -match '^(?<hash>[0-9a-fA-F]{64})\s{2}(?<name>.+)$') {
        $expectedHashes[$Matches.name] = $Matches.hash.ToLowerInvariant()
    }
}

foreach ($wheel in @($collectorWheel, $semvideoWheel)) {
    if (-not $expectedHashes.ContainsKey($wheel.Name)) {
        throw "Missing checksum for $($wheel.Name)."
    }
    $actualHash = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHashes[$wheel.Name]) {
        throw "Checksum mismatch for $($wheel.Name)."
    }
}

$pythonVersion = & python -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne '3.14.6') {
    throw "Semvideo requires CPython 3.14.6; found '$pythonVersion'."
}

if ($InstallFfmpeg) {
    $ffmpegOutput = @(
        & winget install --id Gyan.FFmpeg --exact --accept-package-agreements `
            --accept-source-agreements 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg installation failed.`n$($ffmpegOutput -join [Environment]::NewLine)"
    }
}

$collectorOutput = @(
    & uv tool install --force --python 3.14.6 $collectorWheel.FullName 2>&1
)
if ($LASTEXITCODE -ne 0) {
    throw "material-collector installation failed.`n$($collectorOutput -join [Environment]::NewLine)"
}

$semvideoOutput = @(
    & python -m pip install --user --upgrade $semvideoWheel.FullName 2>&1
)
if ($LASTEXITCODE -ne 0) {
    throw "semvideo installation failed.`n$($semvideoOutput -join [Environment]::NewLine)"
}

$collectorCommand = Get-Command material-collector -ErrorAction SilentlyContinue
$semvideoCommand = Get-Command semvideo -ErrorAction SilentlyContinue

[ordered]@{
    status = 'installed'
    material_collector = if ($null -ne $collectorCommand) {
        $collectorCommand.Source
    } else {
        'installed by uv; reopen the terminal or run uv tool update-shell'
    }
    semvideo = if ($null -ne $semvideoCommand) {
        $semvideoCommand.Source
    } else {
        'installed in the Python user Scripts directory; the Skill can resolve it directly'
    }
    ffmpeg_requested = [bool]$InstallFfmpeg
    checksums_verified = $true
} | ConvertTo-Json -Depth 3
