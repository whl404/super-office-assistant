param(
    [ValidateSet('install', 'update', 'check')][string]$Mode = 'install',
    [string]$CodexHome = (Join-Path $HOME '.codex')
)
$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'install.py') $Mode --codex-home $CodexHome
if ($LASTEXITCODE -ne 0) { throw "Office agents $Mode failed (exit $LASTEXITCODE)." }
