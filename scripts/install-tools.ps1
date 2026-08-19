[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseDirectory,

    [switch]$InstallFfmpeg,

    [string]$PythonExecutable,

    [ValidateSet('auto', 'edge', 'chrome')]
    [string]$BrowserChannel = 'auto'
)

$ErrorActionPreference = 'Stop'

$releasePath = (Resolve-Path -LiteralPath $ReleaseDirectory).Path
$collectorWheels = @(
    Get-ChildItem -LiteralPath $releasePath -Filter 'video_material_collector-*.whl'
)
$semvideoWheels = @(
    Get-ChildItem -LiteralPath $releasePath -Filter 'semvideo-*.whl'
)
$conformanceWheels = @(
    Get-ChildItem -LiteralPath $releasePath -Filter 'media_conformance-*.whl'
)

if ($collectorWheels.Count -ne 1 -or $semvideoWheels.Count -ne 1 -or
    $conformanceWheels.Count -ne 1) {
    throw 'ReleaseDirectory must contain exactly one wheel for each of the three tools.'
}

$collectorWheel = $collectorWheels[0]
$semvideoWheel = $semvideoWheels[0]
$conformanceWheel = $conformanceWheels[0]

function Test-BrowserChannel {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('edge', 'chrome')]
        [string]$Channel
    )

    $relativePath = if ($Channel -eq 'edge') {
        'Microsoft\Edge\Application\msedge.exe'
    } else {
        'Google\Chrome\Application\chrome.exe'
    }
    $roots = @(
        [Environment]::GetFolderPath('ProgramFiles'),
        [Environment]::GetFolderPath('ProgramFilesX86'),
        [Environment]::GetFolderPath('LocalApplicationData')
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    foreach ($root in $roots) {
        if (Test-Path -LiteralPath (Join-Path $root $relativePath) -PathType Leaf) {
            return $true
        }
    }
    return $false
}

$resolvedBrowserChannel = $null
if ($BrowserChannel -eq 'auto') {
    foreach ($candidate in @('edge', 'chrome')) {
        if (Test-BrowserChannel -Channel $candidate) {
            $resolvedBrowserChannel = $candidate
            break
        }
    }
} elseif (Test-BrowserChannel -Channel $BrowserChannel) {
    $resolvedBrowserChannel = $BrowserChannel
}

if ($null -eq $resolvedBrowserChannel) {
    throw "Browser channel '$BrowserChannel' is unavailable. Install or repair Microsoft Edge or Google Chrome."
}

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

foreach ($wheel in @($collectorWheel, $semvideoWheel, $conformanceWheel)) {
    if (-not $expectedHashes.ContainsKey($wheel.Name)) {
        throw "Missing checksum for $($wheel.Name)."
    }
    $actualHash = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHashes[$wheel.Name]) {
        throw "Checksum mismatch for $($wheel.Name)."
    }
}

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pythonLauncher) {
        $pythonPathOutput = @(& $pythonLauncher.Source -3.14 -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $pythonPathOutput.Count -gt 0) {
            $PythonExecutable = $pythonPathOutput[-1].Trim()
        }
    }
    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
            $PythonExecutable = $pythonCommand.Source
        }
    }
}

if ([string]::IsNullOrWhiteSpace($PythonExecutable) -or
    -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw 'A CPython 3.14 executable is required. Pass its path with -PythonExecutable.'
}

$pythonInfoOutput = @(
    & $PythonExecutable -c "import json, sys; print(json.dumps({'version': list(sys.version_info[:3]), 'implementation': sys.implementation.name}))"
)
if ($LASTEXITCODE -ne 0 -or $pythonInfoOutput.Count -eq 0) {
    throw "Unable to inspect Python executable '$PythonExecutable'."
}
$pythonInfo = $pythonInfoOutput[-1] | ConvertFrom-Json
$pythonVersion = @($pythonInfo.version | ForEach-Object { [int]$_ })
$pythonSupported = $pythonInfo.implementation -eq 'cpython' -and
    $pythonVersion[0] -eq 3 -and
    $pythonVersion[1] -eq 14 -and
    $pythonVersion[2] -ge 6
if (-not $pythonSupported) {
    $foundVersion = $pythonVersion -join '.'
    throw "Semvideo requires CPython >=3.14.6,<3.15; found '$($pythonInfo.implementation) $foundVersion'."
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
    & uv tool install --force --python $PythonExecutable $collectorWheel.FullName 2>&1
)
if ($LASTEXITCODE -ne 0) {
    throw "material-collector installation failed.`n$($collectorOutput -join [Environment]::NewLine)"
}

$semvideoOutput = @(
    & $PythonExecutable -m pip install --user --upgrade $semvideoWheel.FullName 2>&1
)
if ($LASTEXITCODE -ne 0) {
    throw "semvideo installation failed.`n$($semvideoOutput -join [Environment]::NewLine)"
}

$conformanceOutput = @(
    & uv tool install --force --python $PythonExecutable $conformanceWheel.FullName 2>&1
)
if ($LASTEXITCODE -ne 0) {
    throw "media-conformance installation failed.`n$($conformanceOutput -join [Environment]::NewLine)"
}

$collectorCommand = Get-Command material-collector -ErrorAction SilentlyContinue
$semvideoCommand = Get-Command semvideo -ErrorAction SilentlyContinue
$conformanceCommand = Get-Command media-conformance -ErrorAction SilentlyContinue

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
    media_conformance = if ($null -ne $conformanceCommand) {
        $conformanceCommand.Source
    } else {
        'installed by uv; reopen the terminal or run uv tool update-shell'
    }
    ffmpeg_requested = [bool]$InstallFfmpeg
    browser_channel_requested = $BrowserChannel
    browser_channel_resolved = $resolvedBrowserChannel
    checksums_verified = $true
    python = $PythonExecutable
} | ConvertTo-Json -Depth 3
