[CmdletBinding()]
param(
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repositoryRoot 'dist'
}
elseif (-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $repositoryRoot $OutputDirectory
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$collectorRoot = Join-Path $repositoryRoot 'packages\material-collector'
$semvideoRoot = Join-Path $repositoryRoot 'packages\semvideo'

Push-Location $collectorRoot
try {
    & uv build --wheel --out-dir $OutputDirectory
    if ($LASTEXITCODE -ne 0) {
        throw 'material-collector wheel build failed.'
    }
}
finally {
    Pop-Location
}

Push-Location $semvideoRoot
try {
    & python -m pip wheel . --no-deps --wheel-dir $OutputDirectory
    if ($LASTEXITCODE -ne 0) {
        throw 'semvideo wheel build failed.'
    }
}
finally {
    Pop-Location
}

$installerSource = Join-Path $PSScriptRoot 'install-tools.ps1'
$installerDestination = Join-Path $OutputDirectory 'install-tools.ps1'
[System.IO.File]::Copy($installerSource, $installerDestination, $true)

$checksumPath = Join-Path $OutputDirectory 'SHA256SUMS.txt'
$checksumInputs = @(
    Get-ChildItem -LiteralPath $OutputDirectory -Filter '*.whl'
    Get-Item -LiteralPath $installerDestination
)
$checksumLines = $checksumInputs |
    Sort-Object Name |
    ForEach-Object {
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $($_.Name)"
    }

[System.IO.File]::WriteAllLines($checksumPath, $checksumLines)

[ordered]@{
    status = 'built'
    output_directory = $OutputDirectory
    artifacts = @(
        Get-ChildItem -LiteralPath $OutputDirectory -File |
            Sort-Object Name |
            Select-Object -ExpandProperty FullName
    )
} | ConvertTo-Json -Depth 4
