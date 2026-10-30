param(
    [Parameter(Mandatory=$true)][string]$RepoUrl,
    [string]$Ref = 'main',
    [string]$CodexHome = (Join-Path $HOME '.codex'),
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
# Require an explicit trusted network repository; never evaluate a shell command from it.
if ($RepoUrl -notmatch '^(https://|ssh://|git@)' -or $RepoUrl.Contains("`n")) {
    throw 'Use an explicitly trusted HTTPS or SSH Git repository URL without embedded credentials.'
}
if ($RepoUrl -match '^https://[^/]*@') { throw 'Do not put credentials in the repository URL.' }
if ($Ref.StartsWith('-') -or $Ref.Contains("`n")) { throw 'Invalid branch/tag.' }
$checkout = Join-Path ([System.IO.Path]::GetTempPath()) ('office-agents-update-' + [guid]::NewGuid().ToString('N'))
try {
    # Clone data only. Disable hooks and recursive submodules.
    & git -c core.hooksPath=NUL clone --depth 1 --single-branch --branch $Ref --no-recurse-submodules -- $RepoUrl $checkout
    if ($LASTEXITCODE -ne 0) { throw 'Git fetch failed; installed version was not changed.' }
    $package = Join-Path $checkout 'distribution/office-agents'
    if (-not (Test-Path -LiteralPath (Join-Path $package 'manifest.json'))) {
        throw 'Repository does not contain distribution/office-agents/manifest.json.'
    }
    $mode = if ($CheckOnly) { 'check' } else { 'update' }
    # Execute OUR installed script, never the downloaded installer/build scripts.
    & python (Join-Path $PSScriptRoot 'install.py') $mode --codex-home $CodexHome --package $package
    if ($LASTEXITCODE -ne 0) { throw 'Update stopped. Review checksum/conflict/version diagnostics.' }
} finally {
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\','/')
    $resolved = [System.IO.Path]::GetFullPath($checkout)
    if ($resolved.StartsWith($tempRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) -and
        [System.IO.Path]::GetFileName($resolved).StartsWith('office-agents-update-') -and (Test-Path -LiteralPath $resolved)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
