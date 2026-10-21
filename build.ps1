# Builds the Windows app and, if Inno Setup is installed, the installer.
#   .\build.ps1                 tests -> PyInstaller -> smoke test -> installer
#   .\build.ps1 -SkipTests      without pytest
#   .\build.ps1 -NoInstaller    only the app folder dist\IntercomGateway
# Needs the build extras:  pip install -e ".[ui,dev,build]"
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

$version = (& $py -c "import gateway; print(gateway.__version__)").Trim()
Write-Host "Intercom Gateway $version"

if (-not $SkipTests) { Run "pytest" { & $py -m pytest -q } }

Run "PyInstaller" { & $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\intercom-gateway.spec }

$app = "dist\IntercomGateway"
Run "smoke test (intercom-gateway.exe --version)" {
    $out = & "$app\intercom-gateway.exe" --version
    if ($out.Trim() -ne $version) { throw "expected version $version, got '$out'" }
    & "$app\intercom-gateway.exe" devices | Out-Null
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
Run "Inno Setup" { & $iscc "/DAppVersion=$version" packaging\installer.iss }
Write-Host "Installer: dist\installer\intercom-gateway-$version-setup.exe" -ForegroundColor Green
