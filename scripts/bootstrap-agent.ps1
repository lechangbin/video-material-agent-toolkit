[CmdletBinding()]
param(
    [string]$Repository = 'lechangbin/video-material-agent-toolkit',
    [string]$ReleaseTag = 'latest',
    [string]$BrowserChannel = 'auto',
    [string[]]$Agents = @('*'),
    [string[]]$AdditionalSkillsDirectory = @(),
    [switch]$SkipSystemDependencies,
    [switch]$SkipCliTools,
    [switch]$SkipSkills,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$installedPackages = [System.Collections.Generic.List[string]]::new()
$temporaryReleaseDirectory = $null

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = @($machinePath, $userPath) -join ';'
}

function Test-CommandAvailable {
    param([Parameter(Mandatory = $true)][string]$Name)

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Resolve-Python314 {
    $candidates = [System.Collections.Generic.List[string]]::new()

    if (Test-CommandAvailable 'py.exe') {
        $launcherResult = @(& py.exe -3.14 -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $launcherResult.Count -gt 0) {
            $candidates.Add($launcherResult[-1].Trim())
        }
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        $candidates.Add($pythonCommand.Source)
    }

    foreach ($candidate in $candidates | Select-Object -Unique) {
        $versionResult = @(
            & $candidate -c "import sys; ok=(3,14,6) <= sys.version_info[:3] < (3,15,0); print('true' if ok else 'false')" 2>$null
        )
        if ($LASTEXITCODE -eq 0 -and $versionResult.Count -gt 0 -and $versionResult[-1].Trim() -eq 'true') {
            return $candidate
        }
    }

    return $null
}

function Test-BrowserChannel {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('edge', 'chrome')]
        [string]$Channel
    )

    $commandName = if ($Channel -eq 'edge') { 'msedge.exe' } else { 'chrome.exe' }
    if (Test-CommandAvailable $commandName) {
        return $true
    }

    $relativePath = if ($Channel -eq 'edge') {
        'Microsoft\Edge\Application\msedge.exe'
    }
    else {
        'Google\Chrome\Application\chrome.exe'
    }
    $roots = @(
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:LOCALAPPDATA
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    return $null -ne (
        $roots |
            ForEach-Object { Join-Path $_ $relativePath } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Select-Object -First 1
    )
}

function Resolve-BrowserChannel {
    if ($BrowserChannel -eq 'auto') {
        foreach ($candidate in @('edge', 'chrome')) {
            if (Test-BrowserChannel -Channel $candidate) {
                return $candidate
            }
        }
        return $null
    }
    if (Test-BrowserChannel -Channel $BrowserChannel) {
        return $BrowserChannel
    }
    return $null
}

function Get-MissingDependencies {
    $missing = [System.Collections.Generic.List[object]]::new()

    if ($null -eq (Resolve-Python314)) {
        $missing.Add([pscustomobject]@{ name = 'python'; package = 'Python.Python.3.14' })
    }
    if (-not (Test-CommandAvailable 'uv.exe')) {
        $missing.Add([pscustomobject]@{ name = 'uv'; package = 'astral-sh.uv' })
    }
    if (-not (Test-CommandAvailable 'node.exe') -or
        (-not (Test-CommandAvailable 'npx.cmd') -and -not (Test-CommandAvailable 'npx.exe'))) {
        $missing.Add([pscustomobject]@{ name = 'node'; package = 'OpenJS.NodeJS.LTS' })
    }
    if ($null -eq (Resolve-BrowserChannel)) {
        $browserDependency = if ($BrowserChannel -eq 'chrome') {
            [pscustomobject]@{ name = 'chrome'; package = 'Google.Chrome' }
        }
        else {
            [pscustomobject]@{ name = 'edge'; package = 'Microsoft.Edge' }
        }
        $missing.Add($browserDependency)
    }
    if (-not (Test-CommandAvailable 'ffmpeg.exe') -or -not (Test-CommandAvailable 'ffprobe.exe')) {
        $missing.Add([pscustomobject]@{ name = 'ffmpeg'; package = 'Gyan.FFmpeg' })
    }

    return @($missing)
}

function Install-WingetPackage {
    param([Parameter(Mandatory = $true)][string]$PackageId)

    $output = @(
        & winget.exe install --id $PackageId --exact --accept-package-agreements `
            --accept-source-agreements --disable-interactivity 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed for '$PackageId'.`n$($output -join [Environment]::NewLine)"
    }
    $installedPackages.Add($PackageId)
}

function Copy-SkillsToDirectory {
    param([Parameter(Mandatory = $true)][string]$Destination)

    $destinationRoot = [System.IO.Path]::GetFullPath($Destination)
    [System.IO.Directory]::CreateDirectory($destinationRoot) | Out-Null
    foreach ($skillDirectory in Get-ChildItem -LiteralPath (Join-Path $repositoryRoot 'skills') -Directory) {
        Copy-Item -LiteralPath $skillDirectory.FullName -Destination $destinationRoot -Recurse -Force
    }
    return $destinationRoot
}

try {
    $BrowserChannel = $BrowserChannel.ToLowerInvariant()
    if (@('auto', 'edge', 'chrome') -notcontains $BrowserChannel) {
        throw 'BrowserChannel must be auto, edge, or chrome.'
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'The automated bootstrap currently supports Windows x64 only.'
    }

    Refresh-ProcessPath
    $missingDependencies = @(Get-MissingDependencies)
    $resolvedBrowserChannel = Resolve-BrowserChannel

    if ($CheckOnly) {
        [ordered]@{
            schema_version = 1
            status = if ($missingDependencies.Count -eq 0) { 'ready' } else { 'missing_dependencies' }
            missing_dependencies = @($missingDependencies | Select-Object -ExpandProperty name)
            browser_channel_requested = $BrowserChannel
            browser_channel_resolved = $resolvedBrowserChannel
            repository = $Repository
        } | ConvertTo-Json -Depth 4
        exit 0
    }

    if ($missingDependencies.Count -gt 0 -and $SkipSystemDependencies) {
        $names = @($missingDependencies | Select-Object -ExpandProperty name) -join ', '
        throw "Required system dependencies are missing: $names"
    }

    if ($missingDependencies.Count -gt 0) {
        if (-not (Test-CommandAvailable 'winget.exe')) {
            throw 'winget is required to install missing system dependencies.'
        }
        foreach ($dependency in $missingDependencies) {
            Install-WingetPackage -PackageId $dependency.package
        }
        Refresh-ProcessPath
    }

    $remainingDependencies = @(Get-MissingDependencies)
    if ($remainingDependencies.Count -gt 0) {
        $names = @($remainingDependencies | Select-Object -ExpandProperty name) -join ', '
        throw "Dependencies were installed but are not visible in this process: $names. Reopen PowerShell and rerun the bootstrap script."
    }

    $pythonExecutable = Resolve-Python314
    $toolInstallResult = $null
    $resolvedReleaseTag = $null

    if (-not $SkipCliTools) {
        $releaseUri = if ($ReleaseTag -eq 'latest') {
            "https://api.github.com/repos/$Repository/releases/latest"
        } else {
            "https://api.github.com/repos/$Repository/releases/tags/$ReleaseTag"
        }
        $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'video-material-agent-toolkit-bootstrap' }
        $release = Invoke-RestMethod -Uri $releaseUri -Headers $headers
        $resolvedReleaseTag = $release.tag_name
        $requiredAssets = @(
            $release.assets | Where-Object {
                $_.name -eq 'SHA256SUMS.txt' -or
                $_.name -like 'video_material_collector-*.whl' -or
                $_.name -like 'semvideo-*.whl'
            }
        )
        if ($requiredAssets.Count -ne 3) {
            throw "Release '$resolvedReleaseTag' must contain two wheels and SHA256SUMS.txt."
        }

        $temporaryReleaseDirectory = Join-Path ([System.IO.Path]::GetTempPath()) (
            'video-material-agent-toolkit-' + [guid]::NewGuid().ToString('N')
        )
        [System.IO.Directory]::CreateDirectory($temporaryReleaseDirectory) | Out-Null
        foreach ($asset in $requiredAssets) {
            Invoke-WebRequest -Uri $asset.browser_download_url -Headers $headers `
                -OutFile (Join-Path $temporaryReleaseDirectory $asset.name)
        }

        $installerOutput = @(
            & (Join-Path $PSScriptRoot 'install-tools.ps1') `
                -ReleaseDirectory $temporaryReleaseDirectory `
                -PythonExecutable $pythonExecutable `
                -BrowserChannel $BrowserChannel
        )
        if ($LASTEXITCODE -ne 0) {
            throw 'CLI tool installation failed.'
        }
        $toolInstallResult = ($installerOutput -join [Environment]::NewLine) | ConvertFrom-Json
    }

    $skillInstallStatus = 'skipped'
    if (-not $SkipSkills) {
        $npxCommand = Get-Command npx.cmd -ErrorAction SilentlyContinue
        if ($null -eq $npxCommand) {
            $npxCommand = Get-Command npx.exe -ErrorAction SilentlyContinue
        }
        if ($null -eq $npxCommand) {
            throw 'npx is required to install Agent Skills.'
        }

        $skillArguments = @('--yes', 'skills', 'add', $repositoryRoot, '--skill', '*', '--global', '--yes')
        foreach ($agent in $Agents) {
            $skillArguments += @('--agent', $agent)
        }
        $skillOutput = @(& $npxCommand.Source @skillArguments 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Skill installation failed.`n$($skillOutput -join [Environment]::NewLine)"
        }
        $skillInstallStatus = 'installed'
    }

    $additionalSkillRoots = @(
        foreach ($directory in $AdditionalSkillsDirectory) {
            Copy-SkillsToDirectory -Destination $directory
        }
    )

    [ordered]@{
        schema_version = 1
        status = 'configured'
        repository = $Repository
        release_tag = $resolvedReleaseTag
        system_packages_installed = @($installedPackages)
        cli_tools = $toolInstallResult
        browser_channel_requested = $BrowserChannel
        browser_channel_resolved = if ($null -ne $toolInstallResult) {
            $toolInstallResult.browser_channel_resolved
        }
        else {
            Resolve-BrowserChannel
        }
        skills = [ordered]@{
            status = $skillInstallStatus
            agents = @($Agents)
            additional_roots = @($additionalSkillRoots)
        }
        manual_actions = @(
            'Provide SEMVIDEO_API_KEY through a secret manager or process environment before video understanding.',
            'Complete visible platform login when material-collector requests interactive authentication.',
            'Run semvideo init and semvideo doctor for the user-selected workspace.'
        )
    } | ConvertTo-Json -Depth 6
}
catch {
    [ordered]@{
        schema_version = 1
        status = 'error'
        error = $_.Exception.Message
        repository = $Repository
        system_packages_installed = @($installedPackages)
    } | ConvertTo-Json -Depth 5
    exit 1
}
finally {
    if ($null -ne $temporaryReleaseDirectory -and
        (Test-Path -LiteralPath $temporaryReleaseDirectory) -and
        $temporaryReleaseDirectory.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryReleaseDirectory -Recurse -Force
    }
}
