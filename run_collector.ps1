# Runs the collector with the Python from .venv, shows its output and appends it to logs\collector.log.
# The exit code is passed on (0 done, 1 error, 2 a source refused).
# -Assisted: the 2GIS page reader with a visible browser window, see README. Other arguments go to collect_cli.py.
param(
    [switch]$Assisted,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Output "Not found: $python. See 'Installing the collector' in README."
    exit 1
}
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'logs') | Out-Null
$log = Join-Path $PSScriptRoot 'logs\collector.log'

$arguments = @()
if ($Assisted) { $arguments += '--assisted' }
if ($Rest) { $arguments += $Rest }

& $python collect_cli.py @arguments | ForEach-Object {
    Write-Output $_
    Add-Content -LiteralPath $log -Value $_ -Encoding utf8
}
exit $LASTEXITCODE
