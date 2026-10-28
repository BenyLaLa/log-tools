param(
    [Parameter(Mandatory = $true)][ValidateSet('pack', 'configure')][string]$Action,
    [switch]$Offline,
    [string]$InstallerPath,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$resultCode = 0
try {
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Run setup.cmd first to create the Python environment.'
    }
    if (($Offline -or $InstallerPath) -and $Action -ne 'pack') { throw '-Offline and -InstallerPath are only available for pack.' }
    if ($InstallerPath -and -not $Offline) { throw '-InstallerPath requires -Offline.' }
    $arguments = @((Join-Path $projectRoot 'src\log_tools\deployment.py'), $Action, '--root', $projectRoot)
    if ($Offline) { $arguments += '--offline' }
    if ($InstallerPath) { $arguments += @('--installer-path', $InstallerPath) }
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "$Action failed (exit $LASTEXITCODE)." }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $resultCode = 1
} finally {
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $resultCode
