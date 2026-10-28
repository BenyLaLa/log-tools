param(
    [string]$PythonPath,
    [switch]$Offline,
    [switch]$SkipConfigure,
    [switch]$NoPause
)
# Compatible with Windows PowerShell 5.1. Keep this bootstrap ASCII-only.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$resultCode = 0
$pythonVersion = '3.13.17'
$installerSha256 = '5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190'

function Test-Python([string]$Executable) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    try {
        $probe = & $Executable -I -c "import sys,struct; print('OK' if sys.version_info[:2] == (3,13) and struct.calcsize('P') == 8 else 'NO')" 2>$null
        return ($LASTEXITCODE -eq 0 -and $probe -eq 'OK')
    } catch { return $false }
}

function Invoke-Python([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE). Resolve the error above, then run setup.cmd again." }
}

function Test-OfflinePayload {
    $offlineRoot = Join-Path $projectRoot 'offline'
    $manifestPath = Join-Path $offlineRoot 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Offline manifest is missing. Obtain a complete offline ZIP; no online fallback is allowed.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.format -ne 1 -or $manifest.platform -ne 'cp313-win_amd64' -or $manifest.python_version -ne $pythonVersion) { throw 'Unsupported offline payload format or platform.' }
    $requirements = Join-Path $projectRoot 'requirements.lock.txt'
    if ((Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash -ne $manifest.requirements_sha256) { throw 'Offline requirements hash mismatch.' }
    foreach ($line in (Get-Content -LiteralPath $requirements -Encoding UTF8)) {
        $line = $line.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line -notmatch '^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!]+$') {
            throw 'Offline requirements must contain only name==version; URLs and pip options are forbidden.'
        }
    }
    $expected = @{}
    $installerRelative = "offline/python-$pythonVersion-amd64.exe"
    foreach ($entry in $manifest.files) {
        $name = [string]$entry.path
        if ($name -ne $installerRelative -and $name -notmatch '^offline/wheels/[A-Za-z0-9_.+!-]+\.whl$') { throw 'Invalid offline manifest path.' }
        if ($expected.ContainsKey($name)) { throw 'Duplicate offline manifest entry.' }
        $expected[$name] = $true
        $file = Join-Path $projectRoot $name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Offline file missing: $name" }
        if ((Get-Item -LiteralPath $file).Length -ne $entry.bytes -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $entry.sha256) { throw "Offline file checksum mismatch: $name" }
    }
    if (-not $expected.ContainsKey($installerRelative) -or $expected.Count -lt 2) { throw 'Offline installer or wheels are missing.' }
    if ((Get-FileHash -LiteralPath (Join-Path $projectRoot $installerRelative) -Algorithm SHA256).Hash -ne $installerSha256) { throw 'Offline Python installer does not match the pinned official checksum.' }
    foreach ($file in (Get-ChildItem -LiteralPath $offlineRoot -Recurse -File -Force)) {
        $relative = $file.FullName.Substring($projectRoot.Length + 1).Replace('\', '/')
        if ($relative -ne 'offline/manifest.json' -and -not $expected.ContainsKey($relative)) { throw "Unlisted offline payload file: $relative" }
    }
    Write-Host 'Offline payload checksums verified. Installation will use local files only.'
}

try {
    # Python/PowerShell 7 parents may pass a module search path without the native
    # Windows PowerShell utility functions (including Get-FileHash).
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')
    if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) {
        throw 'This setup supports Windows 10/11 x64 only.'
    }
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($architecture -ne 'AMD64') { throw 'This package requires an x64 Windows machine (ARM64 is not supported by this bootstrap).' }
    $Offline = $Offline -or (Test-Path -LiteralPath (Join-Path $projectRoot 'offline'))
    if ($Offline) {
        Test-OfflinePayload
        # Prevent inherited index, find-links, target and proxy-specific pip options
        # from changing the closed set of local wheel inputs.
        Get-ChildItem Env:PIP_* | ForEach-Object { Remove-Item -LiteralPath $_.PSPath }
        $env:PIP_CONFIG_FILE = 'NUL'
        $env:PIP_NO_INDEX = '1'
    }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PIP_NO_INPUT = '1'
    $env:PIP_REQUIRE_VIRTUALENV = 'true'
    $setupCache = Join-Path $projectRoot '.setup-cache'
    New-Item -ItemType Directory -Path $setupCache -Force | Out-Null
    $env:PIP_CACHE_DIR = Join-Path $setupCache 'pip'

    $venvDir = Join-Path $projectRoot '.venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venvDir) {
        if (-not (Test-Python $venvPython)) {
            throw 'Existing .venv is invalid or is not Python 3.13 x64. Stop the MCP, rename .venv as a backup, and rerun setup. Do not copy virtual environments between machines.'
        }
        Write-Host 'Using existing .venv.'
    } else {
        $python = $null
        if ($PythonPath) {
            $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
            if (-not (Test-Python $PythonPath)) { throw '-PythonPath must point to a working Python 3.13 x64 interpreter.' }
            $python = $PythonPath
        } else {
            $candidates = @((Join-Path $projectRoot '.python\python.exe'))
            foreach ($registryPath in @('HKCU:\Software\Python\PythonCore\3.13\InstallPath', 'HKLM:\Software\Python\PythonCore\3.13\InstallPath')) {
                if (Test-Path -LiteralPath $registryPath) {
                    $installPath = (Get-Item -LiteralPath $registryPath).GetValue('')
                    if ($installPath) { $candidates += Join-Path $installPath 'python.exe' }
                }
            }
            $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
            if ($pythonCommand -and $pythonCommand.Source -notmatch 'WindowsApps') { $candidates += $pythonCommand.Source }
            foreach ($candidate in $candidates) {
                if (Test-Python $candidate) { $python = $candidate; break }
            }
        }

        if (-not $python) {
            $downloadDir = Join-Path $projectRoot '.setup-cache'
            New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
            $installer = if ($Offline) { Join-Path $projectRoot "offline\python-$pythonVersion-amd64.exe" } else { Join-Path $downloadDir "python-$pythonVersion-amd64.exe" }
            if (-not $Offline -and (-not (Test-Path -LiteralPath $installer) -or (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $installerSha256)) {
                Write-Host "Downloading official Python $pythonVersion x64 installer..."
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-amd64.exe" -OutFile $installer
            }
            if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $installerSha256) { throw 'Python installer SHA256 mismatch. Installer was not executed.' }
            if (-not $Offline) {
                Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1')
                $signature = Get-AuthenticodeSignature -LiteralPath $installer
                if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
                    throw 'Python installer signature validation failed. Installer was not executed.'
                }
            }
            $installDir = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313'
            if (Test-Path -LiteralPath $installDir) { throw "Existing Python installation at $installDir could not be used. Repair it or supply -PythonPath; setup will not overwrite it." }
            $installerArguments = @('/quiet', 'InstallAllUsers=0', 'Include_launcher=0', 'PrependPath=0', 'AssociateFiles=0', 'Shortcuts=0', 'Include_test=0', 'Include_doc=0', 'Include_tcltk=0', 'Include_pip=1', "TargetDir=`"$installDir`"")
            Write-Host 'Installing Python for the current Windows user (system PATH unchanged)...'
            $process = Start-Process -FilePath $installer -ArgumentList $installerArguments -PassThru -WindowStyle Hidden
            while (-not $process.WaitForExit(10000)) { Write-Host 'Python installer is still running...' }
            if ($process.ExitCode -notin @(0, 3010)) { throw "Python installer failed (exit $($process.ExitCode))." }
            $python = Join-Path $installDir 'python.exe'
            if (-not (Test-Python $python)) { throw 'Python validation failed after installation. A Windows restart or installer repair may be needed.' }
        }
        Write-Host "Creating .venv with $python"
        Invoke-Python $python @('-m', 'venv', $venvDir)
    }

    # A moved/copied virtual environment can still launch but point at another location.
    Invoke-Python $venvPython @('-I', '-c', "import pathlib,sys; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve(), 'Virtual environment path mismatch'", $venvDir)
    if ($Offline) {
        Write-Host 'Installing locked dependencies from bundled wheels (no index, no cache)...'
        Invoke-Python $venvPython @('-m', 'pip', 'install', '--no-index', '--no-cache-dir', '--only-binary=:all:', '--find-links', (Join-Path $projectRoot 'offline\wheels'), '-r', (Join-Path $projectRoot 'requirements.lock.txt'))
    } else {
        Write-Host 'Installing locked dependencies from PyPI (internet access required)...'
        Invoke-Python $venvPython @('-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', '-r', (Join-Path $projectRoot 'requirements.lock.txt'))
    }
    Invoke-Python $venvPython @('-m', 'pip', 'install', '--no-index', '--no-deps', '--no-build-isolation', '-e', $projectRoot)
    Invoke-Python $venvPython @('-m', 'pip', 'check')
    $deployment = Join-Path $projectRoot 'src\log_tools\deployment.py'
    Invoke-Python $venvPython @($deployment, 'initialize', '--root', $projectRoot)
    if (-not $SkipConfigure) { Invoke-Python $venvPython @($deployment, 'configure', '--root', $projectRoot) }
    Invoke-Python $venvPython @('-m', 'log_tools.client', '--config', (Join-Path $projectRoot 'config.json'), 'check')
    Write-Host 'Setup finished. This check did not test remote SFTP login. See docs\distribution.md for the files command and first download.' -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $resultCode = 1
} finally {
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $resultCode
