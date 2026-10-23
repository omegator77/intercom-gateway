# Builds the Windows intercom client and, if Inno Setup is installed, its installer.
#   .\build-client.ps1                 tests -> PyInstaller -> smoke tests -> installer
#   .\build-client.ps1 -SkipTests      without pytest
#   .\build-client.ps1 -NoInstaller    only the app folder dist\IntercomClient
# Needs the build extras:  pip install -e ".[client,dev,build]"
param(
    [switch]$SkipTests,
    [switch]$NoInstaller
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if (Test-Path ".venv\Scripts\python.exe") { (Resolve-Path ".venv\Scripts\python.exe").Path } else { "python" }
function Run([string]$what, [scriptblock]$cmd) {
    Write-Host "==> $what" -ForegroundColor Cyan
    # Windows PowerShell 5.1 turns anything a native tool writes to stderr (PyInstaller logs there) into an
    # error under "Stop"; the exit code is checked below instead.
    $ErrorActionPreference = "Continue"
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

$version = (& $py -c "import intercom_client; print(intercom_client.__version__)").Trim()
Write-Host "Intercom Client $version"

if (-not $SkipTests) { Run "pytest" { & $py -m pytest -q } }

Run "PyInstaller" { & $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\client packaging\intercom-client.spec }

$app = "dist\IntercomClient"
Run "smoke test (intercom-client.exe --version)" {
    $out = & "$app\intercom-client.exe" --version
    if ($out.Trim() -ne $version) { throw "expected version $version, got '$out'" }
}
# loads every library the packaged app needs; a missing one only shows up in the frozen app
Run "self test (intercom-client.exe --selftest)" {
    $out = & "$app\intercom-client.exe" --selftest
    $out | ForEach-Object { Write-Host $_ }
    if (($out | Select-Object -Last 1) -ne "selftest ok") { throw "self test failed" }
}

if ($NoInstaller) { Write-Host "App folder: $app"; exit 0 }

$iscc = @(
    (Get-Command ISCC -ErrorAction SilentlyContinue).Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    Write-Warning "Inno Setup 6 not found - installer skipped. Install it (winget install JRSoftware.InnoSetup) and run again."
    Write-Host "App folder: $app"
    exit 0
}
Run "Inno Setup" { & $iscc "/DAppVersion=$version" packaging\installer_client.iss }
Write-Host "Installer: dist\installer\intercom-client-$version-setup.exe" -ForegroundColor Green
