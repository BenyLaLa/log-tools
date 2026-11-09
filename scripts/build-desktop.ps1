param(
    [string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$IsccPath,
    [switch]$NoPause
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$code=0
try {
    if (-not $PythonPath) { $PythonPath=Join-Path $root '.venv\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $IsccPath)) { throw 'Supply the installed Inno Setup ISCC.exe path.' }
    & $PythonPath (Join-Path $root 'scripts\collect-desktop-licenses.py')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency notice collection failed.' }
    & $PythonPath -m PyInstaller --clean --noconfirm --distpath (Join-Path $root 'dist\desktop') --workpath (Join-Path $root 'build\desktop') (Join-Path $root 'packaging\logtools.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $IsccPath "/DBundleDir=$(Join-Path $root 'dist\desktop\LogTools')" "/DOutputDir=$(Join-Path $root 'dist')" (Join-Path $root 'packaging\setup.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
    $installer=Join-Path $root 'dist\LogTools-Setup-0.2.4-x64.exe'
    $sha=[Security.Cryptography.SHA256]::Create()
    $inputStream=[IO.File]::OpenRead($installer)
    try { $digest=([BitConverter]::ToString($sha.ComputeHash($inputStream))).Replace('-','').ToLowerInvariant() }
    finally { $inputStream.Dispose(); $sha.Dispose() }
    [IO.File]::WriteAllText($installer+'.sha256', $digest+'  '+[IO.Path]::GetFileName($installer)+"`n")
    Write-Host "Installer ready: $installer"
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $code=1
} finally {
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $code
