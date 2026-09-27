[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$revision = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
$modelRoot = Join-Path $env:LOCALAPPDATA "video-material-agent-toolkit\models\multilingual-e5-small\$revision"
$runtimeRoot = Join-Path $modelRoot 'runtime'
$runtimePython = Join-Path $runtimeRoot 'Scripts\python.exe'
$requirementsLock = Join-Path $PSScriptRoot 'requirements-embedding.lock'
$assets = @(
    [pscustomobject]@{
        Name = 'model.onnx'
        Remote = 'onnx/model.onnx'
        Sha256 = 'ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665'
    },
    [pscustomobject]@{
        Name = 'tokenizer.json'
        Remote = 'tokenizer.json'
        Sha256 = '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'
    }
)

function Test-Asset {
    param([Parameter(Mandatory = $true)]$Asset)
    $path = Join-Path $modelRoot $Asset.Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $false
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    return $actual -eq $Asset.Sha256
}

$missing = @($assets | Where-Object { -not (Test-Asset -Asset $_) } | ForEach-Object { $_.Name })
$dependenciesReady = $false
if (Test-Path -LiteralPath $runtimePython -PathType Leaf) {
    $dependencyCheck = @(
        & $runtimePython -c 'import importlib.metadata as m; import numpy, onnxruntime, tokenizers; print("ready" if all(m.version(p)==v for p,v in (("numpy","2.5.1"),("onnxruntime","1.29.0"),("tokenizers","0.23.1"))) else "missing")' 2>$null
    )
    $dependenciesReady = $LASTEXITCODE -eq 0 -and $dependencyCheck.Count -gt 0 -and $dependencyCheck[-1] -eq 'ready'
}

if (-not $CheckOnly) {
    if (-not $dependenciesReady) {
        if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
            $venvOutput = @(& uv venv --python $PythonExecutable $runtimeRoot 2>&1)
            if ($LASTEXITCODE -ne 0) {
                throw "Embedding runtime creation failed.`n$($venvOutput -join [Environment]::NewLine)"
            }
        }
        $installOutput = @(
            & uv pip sync --python $runtimePython --require-hashes $requirementsLock 2>&1
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Embedding runtime dependency installation failed.`n$($installOutput -join [Environment]::NewLine)"
        }
        $dependenciesReady = $true
    }
    [System.IO.Directory]::CreateDirectory($modelRoot) | Out-Null
    foreach ($asset in $assets) {
        if (Test-Asset -Asset $asset) { continue }
        $target = Join-Path $modelRoot $asset.Name
        $temporary = "$target.$([guid]::NewGuid().ToString('N')).download"
        try {
            $uri = "https://huggingface.co/intfloat/multilingual-e5-small/resolve/$revision/$($asset.Remote)"
            Invoke-WebRequest -Uri $uri -OutFile $temporary
            $actual = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $asset.Sha256) {
                throw "Checksum mismatch for embedding model asset '$($asset.Name)'."
            }
            Move-Item -LiteralPath $temporary -Destination $target -Force
        }
        finally {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
    }
    $missing = @($assets | Where-Object { -not (Test-Asset -Asset $_) } | ForEach-Object { $_.Name })
}

[ordered]@{
    status = if ($dependenciesReady -and $missing.Count -eq 0) { 'ready' } else { 'missing' }
    model = 'intfloat/multilingual-e5-small'
    revision = $revision
    model_directory = $modelRoot
    runtime_python = $runtimePython
    dependencies_ready = $dependenciesReady
    missing_assets = $missing
} | ConvertTo-Json -Depth 4
