# CY-CLI Windows installer build script (shared by build-windows.yml and validation workflows)
param(
    [Parameter(Mandatory = $true)][string]$BinPath,
    [Parameter(Mandatory = $true)][string]$WrapperPath,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$OutDir = (Get-Location).Path,
    [string]$InstallerName = 'CY-CLI-x86_64-setup.exe'
)

$ErrorActionPreference = 'Stop'

$INSTALLER = Join-Path $OutDir $InstallerName
$BinDir    = Join-Path $OutDir 'bin'
$LicenseTxt = Join-Path $OutDir 'license.txt'
$IssPath   = Join-Path $OutDir 'installer.iss'

# Copy-Item fails outright with "Cannot overwrite the item ... with itself" when the
# source resolves to the destination, which is what happens whenever a caller passes a
# -BinPath that already sits in $OutDir\bin. Staging an input that is already staged
# should be a no-op, not a hard error.
function Copy-IfDistinct {
    param([string]$From, [string]$To)

    $src = (Resolve-Path -LiteralPath $From).ProviderPath
    $dst = if (Test-Path -LiteralPath $To) { (Resolve-Path -LiteralPath $To).ProviderPath } else { $To }
    if ($src -ne $dst) {
        Copy-Item -LiteralPath $src -Destination $To -Force
    }
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Copy-IfDistinct -From $BinPath -To (Join-Path $BinDir 'cy.exe')
Copy-IfDistinct -From $WrapperPath -To (Join-Path $OutDir 'cy-wrapper.ps1')

@"
CY-CLI is licensed under the Apache License 2.0.
See https://github.com/SYMBIOTYC/CY-CLI-releases/blob/main/LICENSE
"@ | Set-Content -Path $LicenseTxt -Encoding ASCII

$iss = @"
; CY-CLI Windows Installer
#define MyAppName "CY-CLI"
#define MyAppVersion "$Version"
#define MyAppPublisher "SYMBIOTYC"
#define MyAppURL "https://github.com/SYMBIOTYC/CY-CLI-releases"
#define MyAppExeName "cy-wrapper.ps1"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=license.txt
OutputDir=.
OutputBaseFilename=CY-CLI-x86_64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Add {#MyAppName} to your PATH"; GroupDescription: "Additional options:"

[Files]
Source: "bin\cy.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "cy-wrapper.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\cy-wrapper.ps1"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autostartup}\{#MyAppName}"; Filename: "{app}\cy-wrapper.ps1"; Comment: "CY-CLI Self-Updating Wrapper"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\cy-wrapper.ps1"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\cy-wrapper.ps1"" --version"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Flags: preservestringtype; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Path: string): Boolean;
var
  OldPath: string;
begin
  Result := False;
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OldPath) then
    Exit;
  Result := Pos(UpperCase(Path), Uppercase(OldPath)) = 0;
end;
"@

Set-Content -Path $IssPath -Value $iss -Encoding ASCII

# Locate the Inno Setup compiler (preinstalled on windows-2022; choco as fallback)
$candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path ${env:ProgramFiles} 'Inno Setup 6\ISCC.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup\ISCC.exe'),
    (Join-Path ${env:ProgramFiles} 'Inno Setup\ISCC.exe')
)
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    $shim = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($shim) { $iscc = $shim.Source }
}
if (-not $iscc) {
    choco install innosetup -y --no-progress | Out-Host
    $shim = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($shim) { $iscc = $shim.Source }
}
if (-not $iscc) {
    Write-Error 'Inno Setup compiler (ISCC.exe) was not found on this runner'
    exit 1
}

Write-Host "Using Inno Setup compiler: $iscc"
Push-Location $OutDir
try {
    & $iscc $IssPath
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

if (-not (Test-Path $INSTALLER)) { throw "Installer not created: $INSTALLER" }

Write-Host "Created $INSTALLER"
Get-ChildItem $INSTALLER | Select-Object FullName, Length
Get-FileHash -Algorithm SHA256 $INSTALLER | Select-Object -ExpandProperty Hash | Out-File -FilePath "$INSTALLER.sha256"
