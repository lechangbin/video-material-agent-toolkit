[CmdletBinding()]
param(
    [string]$Operation,

    [string]$CollectorPath = 'material-collector',

    [string]$Workspace,

    [string]$InputPath,

    [string]$QueryPlansPath,

    [string]$SessionId,

    [string]$RequestTimeoutSeconds = '30',

    [string]$MaxRounds = '3',

    [string]$MaxVideos = '18',

    [string]$ProgressFormat = 'jsonl',

    [string]$ControlDirectory,

    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]]$UnexpectedArguments
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Stop-WithContractError {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,

        [Parameter(Mandatory = $true)]
        [string]$Message,

        [int]$ExitCode = 30,

        [System.Collections.IDictionary]$Context
    )

    $payload = [ordered]@{
        schema_version = '1.0'
        status = 'error'
        error = [ordered]@{
            code = $Code
            message = $Message
        }
    }
    if ($null -ne $Context) {
        foreach ($key in $Context.Keys) {
            $payload[$key] = $Context[$key]
        }
    }
    $payload | ConvertTo-Json -Compress -Depth 8
    exit $ExitCode
}

function Write-ControlRecord {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$Record
    )

    $temporaryPath = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    $json = $Record | ConvertTo-Json -Compress -Depth 6
    Set-Content -LiteralPath $temporaryPath -Value $json -Encoding utf8NoBOM
    Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    return $json
}

function Test-RecordedProcessAlive {
    param(
        [object]$RecordedProcessId,
        [object]$RecordedStartTicks
    )

    if ($null -eq $RecordedProcessId -or $null -eq $RecordedStartTicks) {
        return $false
    }
    try {
        $recordedProcess = Get-Process -Id ([int]$RecordedProcessId) -ErrorAction Stop
        return (
            $recordedProcess.StartTime.ToUniversalTime().Ticks -eq
            [long]$RecordedStartTicks
        )
    }
    catch {
        return $false
    }
}

if ($null -ne $UnexpectedArguments -and $UnexpectedArguments.Count -gt 0) {
    Stop-WithContractError `
        -Code 'arguments_invalid' `
        -Message 'The runner received unsupported arguments.' `
        -ExitCode 40
}

if ($Operation -notin @('run', 'resume', 'status', 'cancel')) {
    Stop-WithContractError `
        -Code 'operation_invalid' `
        -Message 'Operation must be run, resume, status, or cancel.' `
        -ExitCode 40
}

$parsedRequestTimeout = 0
if (
    -not [int]::TryParse($RequestTimeoutSeconds, [ref]$parsedRequestTimeout) -or
    $parsedRequestTimeout -lt 1 -or
    $parsedRequestTimeout -gt 3600
) {
    Stop-WithContractError `
        -Code 'request_timeout_invalid' `
        -Message 'RequestTimeoutSeconds must be an integer from 1 through 3600.' `
        -ExitCode 40
}
if ($ProgressFormat -notin @('jsonl', 'text')) {
    Stop-WithContractError `
        -Code 'progress_format_invalid' `
        -Message 'ProgressFormat must be jsonl or text.' `
        -ExitCode 40
}
$parsedMaxRounds = 0
if (
    -not [int]::TryParse($MaxRounds, [ref]$parsedMaxRounds) -or
    $parsedMaxRounds -lt 1
) {
    Stop-WithContractError `
        -Code 'max_rounds_invalid' `
        -Message 'MaxRounds must be a positive integer.' `
        -ExitCode 40
}
$parsedMaxVideos = 0
if (
    -not [int]::TryParse($MaxVideos, [ref]$parsedMaxVideos) -or
    $parsedMaxVideos -lt 1
) {
    Stop-WithContractError `
        -Code 'max_videos_invalid' `
        -Message 'MaxVideos must be a positive integer.' `
        -ExitCode 40
}

if ([string]::IsNullOrWhiteSpace($Workspace)) {
    Stop-WithContractError `
        -Code 'workspace_required' `
        -Message 'Workspace is required.' `
        -ExitCode 40
}
if ($Operation -eq 'run' -and (
    [string]::IsNullOrWhiteSpace($InputPath) -or
    [string]::IsNullOrWhiteSpace($QueryPlansPath)
)) {
    Stop-WithContractError `
        -Code 'run_inputs_required' `
        -Message 'InputPath and QueryPlansPath are required for run.' `
        -ExitCode 40
}
if (
    $Operation -in @('resume', 'status', 'cancel') -and
    [string]::IsNullOrWhiteSpace($SessionId)
) {
    Stop-WithContractError `
        -Code 'session_id_required' `
        -Message 'SessionId is required for resume, status, and cancel.' `
        -ExitCode 40
}

if ([string]::IsNullOrWhiteSpace($CollectorPath)) {
    Stop-WithContractError `
        -Code 'collector_path_invalid' `
        -Message 'CollectorPath must not be empty.' `
        -ExitCode 40
}
$collectorCommand = Get-Command $CollectorPath -ErrorAction SilentlyContinue
if ($null -ne $collectorCommand) {
    $CollectorPath = $collectorCommand.Source
}
elseif ($CollectorPath -eq 'material-collector') {
    $uvToolCandidate = Join-Path (
        [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    ) '.local\bin\material-collector.exe'
    if (Test-Path -LiteralPath $uvToolCandidate -PathType Leaf) {
        $CollectorPath = $uvToolCandidate
    }
}
if (-not (Test-Path -LiteralPath $CollectorPath -PathType Leaf)) {
    Stop-WithContractError `
        -Code 'collector_not_found' `
        -Message 'The material-collector executable could not be resolved.'
}

$workspacePath = [System.IO.Path]::GetFullPath($Workspace)
if ($Operation -in @('status', 'cancel')) {
    & $CollectorPath `
        $Operation `
        '--workspace' `
        $workspacePath `
        '--session-id' `
        $SessionId
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace($ControlDirectory)) {
    $ControlDirectory = Join-Path $workspacePath '.material-collector\agent-control'
}
$controlRoot = [System.IO.Path]::GetFullPath($ControlDirectory)
$null = New-Item -ItemType Directory -Force -Path $controlRoot

$launchLockPath = Join-Path $controlRoot '.launch.lock'
$launchLock = $null
$launchLockDeadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
while ($null -eq $launchLock -and [DateTimeOffset]::UtcNow -lt $launchLockDeadline) {
    try {
        $launchLock = [System.IO.File]::Open(
            $launchLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        Start-Sleep -Milliseconds 50
    }
}
if ($null -eq $launchLock) {
    Stop-WithContractError `
        -Code 'launch_lock_timeout' `
        -Message 'Another launcher is still preparing a collector executor.'
}

try {
foreach ($existingPath in Get-ChildItem -LiteralPath $controlRoot -Filter '*.control.json') {
    try {
        $existing = Get-Content -Raw -LiteralPath $existingPath.FullName |
            ConvertFrom-Json
        $sameExecution = (
            $Operation -eq 'resume' -and
            $existing.session_id -eq $SessionId
        )
        if (-not $sameExecution -or $existing.host_id -ne [Environment]::MachineName) {
            continue
        }
        $wrapperAlive = Test-RecordedProcessAlive `
            -RecordedProcessId $existing.process_id `
            -RecordedStartTicks $existing.process_start_ticks
        $collectorAlive = Test-RecordedProcessAlive `
            -RecordedProcessId $existing.collector_process_id `
            -RecordedStartTicks $existing.collector_process_start_ticks
        if (-not $wrapperAlive -and -not $collectorAlive) {
            continue
        }
        Stop-WithContractError `
            -Code 'execution_already_running' `
            -Message 'A collector executor is already running.' `
            -Context ([ordered]@{ existing = $existing })
    }
    catch {
        continue
    }
}

if ($Operation -eq 'resume') {
    try {
        $statusArguments = @(
            'status',
            '--workspace',
            $workspacePath,
            '--session-id',
            $SessionId
        )
        $statusText = [string](& $CollectorPath @statusArguments 2>$null | Out-String)
        if ($LASTEXITCODE -eq 0) {
            $statusPayload = $statusText | ConvertFrom-Json
            if (
                $statusPayload.runtime.state -in @('executing', 'cancelling') -and
                $statusPayload.runtime.lease_expired -ne $true
            ) {
                Stop-WithContractError `
                    -Code 'execution_already_running' `
                    -Message 'The session runtime reports a live executor.' `
                    -Context ([ordered]@{
                        existing = [ordered]@{
                        session_id = $SessionId
                        runtime = $statusPayload.runtime
                        }
                    })
            }
        }
        else {
            throw "Collector status returned exit code $LASTEXITCODE."
        }
    }
    catch {
        Stop-WithContractError `
            -Code 'session_status_unavailable' `
            -Message 'The session runtime could not be verified before resume.' `
            -Context ([ordered]@{ session_id = $SessionId })
    }
}

$launchId = 'launch_' + [guid]::NewGuid().ToString('N')
$stdoutPath = Join-Path $controlRoot "$launchId.stdout.json"
$stderrPath = Join-Path $controlRoot "$launchId.stderr.log"
$controlPath = Join-Path $controlRoot "$launchId.control.json"
$childHandshakePath = Join-Path $controlRoot "$launchId.child.json"
$null = New-Item -ItemType File -Path $stdoutPath
$null = New-Item -ItemType File -Path $stderrPath

$collectorArguments = [System.Collections.Generic.List[string]]::new()
$collectorArguments.Add($Operation)
$collectorArguments.Add('--workspace')
$collectorArguments.Add($workspacePath)
if ($Operation -eq 'run') {
    $collectorArguments.Add('--input')
    $collectorArguments.Add([System.IO.Path]::GetFullPath($InputPath))
    $collectorArguments.Add('--query-plans')
    $collectorArguments.Add([System.IO.Path]::GetFullPath($QueryPlansPath))
    $collectorArguments.Add('--request-timeout-seconds')
    $collectorArguments.Add($parsedRequestTimeout.ToString(
        [System.Globalization.CultureInfo]::InvariantCulture
    ))
    $collectorArguments.Add('--max-rounds')
    $collectorArguments.Add($parsedMaxRounds.ToString(
        [System.Globalization.CultureInfo]::InvariantCulture
    ))
    $collectorArguments.Add('--max-videos')
    $collectorArguments.Add($parsedMaxVideos.ToString(
        [System.Globalization.CultureInfo]::InvariantCulture
    ))
}
else {
    $collectorArguments.Add('--session-id')
    $collectorArguments.Add($SessionId)
}
$collectorArguments.Add('--progress-format')
$collectorArguments.Add($ProgressFormat)

$wrapperPayload = [ordered]@{
    collector_path = $CollectorPath
    arguments = @($collectorArguments)
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
    child_handshake_path = $childHandshakePath
} | ConvertTo-Json -Compress -Depth 4
$payloadBase64 = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($wrapperPayload)
)
$wrapperSource = @"
[Console]::OpenStandardOutput().Close()
[Console]::OpenStandardError().Close()
`$payloadJson = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('$payloadBase64')
)
`$payload = `$payloadJson | ConvertFrom-Json
`$start = [Diagnostics.ProcessStartInfo]::new()
`$start.UseShellExecute = `$false
`$start.CreateNoWindow = `$true
`$start.RedirectStandardOutput = `$true
`$start.RedirectStandardError = `$true
`$extension = [IO.Path]::GetExtension([string]`$payload.collector_path)
if (`$extension -in @('.cmd', '.bat')) {
    `$start.FileName = `$env:ComSpec
    `$start.ArgumentList.Add('/d')
    `$start.ArgumentList.Add('/s')
    `$start.ArgumentList.Add('/c')
    `$start.ArgumentList.Add([string]`$payload.collector_path)
}
elseif (`$extension -eq '.ps1') {
    `$start.FileName = (Get-Command pwsh -ErrorAction Stop).Source
    `$start.ArgumentList.Add('-NoLogo')
    `$start.ArgumentList.Add('-NoProfile')
    `$start.ArgumentList.Add('-NonInteractive')
    `$start.ArgumentList.Add('-File')
    `$start.ArgumentList.Add([string]`$payload.collector_path)
}
else {
    `$start.FileName = [string]`$payload.collector_path
}
foreach (`$argument in `$payload.arguments) {
    `$start.ArgumentList.Add([string]`$argument)
}
`$stdoutStream = [IO.FileStream]::new(
    [string]`$payload.stdout_path,
    [IO.FileMode]::Create,
    [IO.FileAccess]::Write,
    [IO.FileShare]::ReadWrite,
    1,
    [IO.FileOptions]::WriteThrough
)
`$stderrStream = [IO.FileStream]::new(
    [string]`$payload.stderr_path,
    [IO.FileMode]::Create,
    [IO.FileAccess]::Write,
    [IO.FileShare]::ReadWrite,
    1,
    [IO.FileOptions]::WriteThrough
)
try {
    `$child = [Diagnostics.Process]::new()
    `$child.StartInfo = `$start
    `$null = `$child.Start()
    `$childHandshake = [ordered]@{
        process_id = `$child.Id
        process_start_time = `$child.StartTime.ToUniversalTime().ToString('o')
        process_start_ticks = `$child.StartTime.ToUniversalTime().Ticks
    } | ConvertTo-Json -Compress
    `$childHandshakeTemporary = ([string]`$payload.child_handshake_path) + '.tmp'
    [IO.File]::WriteAllText(
        `$childHandshakeTemporary,
        `$childHandshake,
        [Text.UTF8Encoding]::new(`$false)
    )
    [IO.File]::Move(
        `$childHandshakeTemporary,
        [string]`$payload.child_handshake_path,
        `$true
    )
    `$stdoutCopy = `$child.StandardOutput.BaseStream.CopyToAsync(`$stdoutStream)
    `$stderrCopy = `$child.StandardError.BaseStream.CopyToAsync(`$stderrStream)
    `$child.WaitForExit()
    [Threading.Tasks.Task]::WaitAll(@(`$stdoutCopy, `$stderrCopy))
    `$exitCode = `$child.ExitCode
    `$child.Dispose()
}
finally {
    `$stdoutStream.Dispose()
    `$stderrStream.Dispose()
}
exit `$exitCode
"@
$encodedWrapper = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($wrapperSource)
)
$startInfo = @{
    FilePath = (Get-Command pwsh -ErrorAction Stop).Source
    ArgumentList = @(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-EncodedCommand',
        $encodedWrapper
    )
    WindowStyle = 'Hidden'
    PassThru = $true
}

try {
    $process = Start-Process @startInfo
}
catch {
    Stop-WithContractError `
        -Code 'collector_start_failed' `
        -Message 'The material collector process could not be started.' `
        -Context ([ordered]@{
            artifacts = [ordered]@{
            stdout_path = $stdoutPath
            stderr_path = $stderrPath
            control_path = $controlPath
            }
        })
}

$process.Refresh()
$resolvedSessionId = $(if ($Operation -eq 'resume') { $SessionId } else { $null })
$startedAt = [DateTimeOffset]::UtcNow.ToString('o')
$control = [ordered]@{
    schema_version = '1.0'
    status = 'starting'
    launch_id = $launchId
    operation = $Operation
    process_id = $process.Id
    process_start_time = $process.StartTime.ToUniversalTime().ToString('o')
    process_start_ticks = $process.StartTime.ToUniversalTime().Ticks
    host_id = [Environment]::MachineName
    started_at = $startedAt
    workspace_path = $workspacePath
    session_id = $resolvedSessionId
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
    control_path = $controlPath
    child_handshake_path = $childHandshakePath
    collector_process_id = $null
    collector_process_start_time = $null
    collector_process_start_ticks = $null
}
$null = Write-ControlRecord -Path $controlPath -Record $control

$handshake = $null
$handshakeDeadline = [DateTimeOffset]::UtcNow.AddSeconds(3)
while ($null -eq $handshake -and [DateTimeOffset]::UtcNow -lt $handshakeDeadline) {
    $process.Refresh()
    if ($process.HasExited) {
        break
    }
    if (Test-Path -LiteralPath $childHandshakePath -PathType Leaf) {
        try {
            $handshake = Get-Content -Raw -LiteralPath $childHandshakePath |
                ConvertFrom-Json
        }
        catch {
            $handshake = $null
        }
    }
    if ($null -eq $handshake) {
        Start-Sleep -Milliseconds 25
    }
}
if ($null -eq $handshake) {
    $control.status = 'exited'
    $null = Write-ControlRecord -Path $controlPath -Record $control
    Stop-WithContractError `
        -Code 'collector_start_not_confirmed' `
        -Message 'The wrapper did not confirm the collector child process.' `
        -Context ([ordered]@{ execution = $control })
}
$control.collector_process_id = [int]$handshake.process_id
$control.collector_process_start_time = [string]$handshake.process_start_time
$control.collector_process_start_ticks = [long]$handshake.process_start_ticks
$null = Write-ControlRecord -Path $controlPath -Record $control

Start-Sleep -Milliseconds 500
$process.Refresh()
try {
    $collectorProcess = Get-Process `
        -Id ([int]$control.collector_process_id) `
        -ErrorAction Stop
    $collectorStartTicks = $collectorProcess.StartTime.ToUniversalTime().Ticks
}
catch {
    $collectorProcess = $null
    $collectorStartTicks = 0
}
if (
    $process.HasExited -or
    $null -eq $collectorProcess -or
    $collectorStartTicks -ne [long]$control.collector_process_start_ticks
) {
    $control.status = 'exited'
    $null = Write-ControlRecord -Path $controlPath -Record $control
    Stop-WithContractError `
        -Code 'collector_exited_during_start' `
        -Message 'The material collector exited during startup stabilization.' `
        -Context ([ordered]@{ execution = $control })
}

if ($Operation -eq 'run') {
    $sessionDeadline = [DateTimeOffset]::UtcNow.AddSeconds(3)
    while (
        [string]::IsNullOrWhiteSpace($resolvedSessionId) -and
        [DateTimeOffset]::UtcNow -lt $sessionDeadline
    ) {
        $stderrText = [string](Get-Content -Raw -LiteralPath $stderrPath)
        $sessionMatch = [regex]::Match(
            $stderrText,
            'session_id"?\s*[:=]\s*"?(ses_[A-Za-z0-9_-]+)'
        )
        if ($sessionMatch.Success) {
            $resolvedSessionId = $sessionMatch.Groups[1].Value
            break
        }
        $process.Refresh()
        if ($process.HasExited) {
            $control.status = 'exited'
            $null = Write-ControlRecord -Path $controlPath -Record $control
            Stop-WithContractError `
                -Code 'collector_exited_during_start' `
                -Message 'The material collector exited before reporting a session id.' `
                -Context ([ordered]@{ execution = $control })
        }
        Start-Sleep -Milliseconds 50
    }
    if ([string]::IsNullOrWhiteSpace($resolvedSessionId)) {
        Stop-WithContractError `
            -Code 'session_id_not_observed' `
            -Message 'The collector did not report a session id during startup.' `
            -Context ([ordered]@{ execution = $control })
    }
}

$control.status = 'started'
$control.session_id = $resolvedSessionId
$controlJson = Write-ControlRecord -Path $controlPath -Record $control
$controlJson
}
finally {
    $launchLock.Dispose()
}
