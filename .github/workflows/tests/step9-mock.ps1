$ErrorActionPreference = 'Stop'

$BIN = Join-Path $env:GITHUB_WORKSPACE 'bin/cy.exe'
$WRAPPER = Join-Path $env:GITHUB_WORKSPACE 'tools/cy-wrapper.ps1'
$INSTALLER = 'CY-CLI-x86_64-setup.exe'
$VERSION = '0.1.5'

if (-not (Test-Path $BIN)) {
  Write-Error "Binary missing: $BIN"
  exit 1
}
if (-not (Test-Path $WRAPPER)) {
  Write-Error "Wrapper missing: $WRAPPER"
  exit 1
}

$issPath = Join-Path $env:GITHUB_WORKSPACE 'installer.iss'
$lines = @(
  '; CY-CLI Windows Installer'
  '#define MyAppName "CY-CLI"'
  '#define MyAppVersion "0.1.5"'
  '[Setup]'
  'AppName={#MyAppName}'
  'OutputDir=C:\mock'
  'OutputBaseFilename=CY-CLI-x86_64-setup'
  '[Files]'
  'Source: "bin\cy.exe"; DestDir: "{app}"; Flags: ignoreversion'
)
[System.IO.File]::WriteAllLines($issPath, $lines)

if (Test-Path $issPath) {
  Write-Host "Step 9: ISS script generated"
} else {
  Write-Error "ISS script missing"
  exit 1
}
