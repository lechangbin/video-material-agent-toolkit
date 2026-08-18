[CmdletBinding()]
param(
    [string]$Operation,

    [string]$CollectorPath = 'material-collector',

    [string]$ResolvedCollectorPath,

    [string]$Workspace,

    [string]$InputPath,

    [string]$QueryPlansPath,

    [string]$SessionId,

    [string]$RequestTimeoutSeconds = '30',

    [string]$MaxRounds = '3',

    [string]$MaxVideos = '18',

    [string]$BrowserChannel = 'auto',

    [switch]$ShowSearchBrowsers,

    [string]$ProgressFormat = 'jsonl',

    [string]$ControlDirectory,

    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]]$UnexpectedArguments
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

function Stop-WithContractError {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,

        [Parameter(Mandatory = $true)]
        [string]$Message,

        [int]$ExitCode = 30
    )

    [ordered]@{
        schema_version = '1.0'
        status = 'error'
        error = [ordered]@{
            code = $Code
            message = $Message
        }
    } | ConvertTo-Json -Compress -Depth 4
    exit $ExitCode
}

if ($null -ne $UnexpectedArguments -and $UnexpectedArguments.Count -gt 0) {
    Stop-WithContractError `
        -Code 'arguments_invalid' `
        -Message 'The runner received unsupported arguments.' `
        -ExitCode 40
}
if (@('auto', 'edge', 'chrome') -notcontains $BrowserChannel.ToLowerInvariant()) {
    Stop-WithContractError `
        -Code 'browser_channel_invalid' `
        -Message 'BrowserChannel must be auto, edge, or chrome.' `
        -ExitCode 40
}
$BrowserChannel = $BrowserChannel.ToLowerInvariant()

$collectorPathWasExplicit = $PSBoundParameters.ContainsKey('CollectorPath')
$resolvedCollectorPathWasExplicit = $PSBoundParameters.ContainsKey('ResolvedCollectorPath')
if ($resolvedCollectorPathWasExplicit) {
    if ([string]::IsNullOrWhiteSpace($ResolvedCollectorPath) -or
        -not (Test-Path -LiteralPath $ResolvedCollectorPath -PathType Leaf)) {
        Stop-WithContractError `
            -Code 'resolved_collector_path_invalid' `
            -Message 'ResolvedCollectorPath must be the compatible executable returned by the Skill resolver.' `
            -ExitCode 40
    }
    $executorPath = (Resolve-Path -LiteralPath $ResolvedCollectorPath).Path
    $CollectorPath = $executorPath
}
else {
    $executorCommand = Get-Command 'material-collector' -ErrorAction SilentlyContinue
    if ($null -ne $executorCommand) {
        $executorPath = $executorCommand.Source
    }
    else {
        $uvToolCandidate = Join-Path (
            [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
        ) '.local\bin\material-collector.exe'
        if (Test-Path -LiteralPath $uvToolCandidate -PathType Leaf) {
            $executorPath = $uvToolCandidate
        }
        elseif ($collectorPathWasExplicit -and
            -not [string]::IsNullOrWhiteSpace($CollectorPath) -and
            (Test-Path -LiteralPath $CollectorPath -PathType Leaf)) {
            $executorPath = (Resolve-Path -LiteralPath $CollectorPath).Path
        }
        else {
            Stop-WithContractError `
                -Code 'collector_not_found' `
                -Message 'The material-collector executable could not be resolved.'
        }
    }
}
if (-not $collectorPathWasExplicit -or $resolvedCollectorPathWasExplicit) {
    $CollectorPath = $executorPath
}

$arguments = [System.Collections.Generic.List[string]]::new()
$arguments.Add('executor')
$arguments.Add('invoke')
if ($PSBoundParameters.ContainsKey('Operation')) {
    $arguments.Add('--operation')
    $arguments.Add($Operation)
}
$arguments.Add('--collector-path')
$arguments.Add($CollectorPath)
if ($PSBoundParameters.ContainsKey('Workspace')) {
    $arguments.Add('--workspace')
    $arguments.Add($Workspace)
}
if ($PSBoundParameters.ContainsKey('InputPath')) {
    $arguments.Add('--input')
    $arguments.Add($InputPath)
}
if ($PSBoundParameters.ContainsKey('QueryPlansPath')) {
    $arguments.Add('--query-plans')
    $arguments.Add($QueryPlansPath)
}
if ($PSBoundParameters.ContainsKey('SessionId')) {
    $arguments.Add('--session-id')
    $arguments.Add($SessionId)
}
$arguments.Add('--request-timeout-seconds')
$arguments.Add($RequestTimeoutSeconds)
$arguments.Add('--max-rounds')
$arguments.Add($MaxRounds)
$arguments.Add('--max-videos')
$arguments.Add($MaxVideos)
$arguments.Add('--browser-channel')
$arguments.Add($BrowserChannel)
if ($ShowSearchBrowsers) {
    $arguments.Add('--show-search-browsers')
}
$arguments.Add('--progress-format')
$arguments.Add($ProgressFormat)
if ($PSBoundParameters.ContainsKey('ControlDirectory')) {
    $arguments.Add('--control-directory')
    $arguments.Add($ControlDirectory)
}

& $executorPath @arguments
exit $LASTEXITCODE
