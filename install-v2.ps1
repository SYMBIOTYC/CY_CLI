# CY-CLI Windows Installer v2 (Self-Updating)
# Repo: https://github.com/SYMBIOTYC/CY-CLI-releases

param(
    [string]$InstallDir = "$env:USERPROFILE\.local\bin",
    [string]$Repo = "SYMBIOTYC/CY-CLI-releases",
    [string]$StoreDir = "$env:USERPROFILE\.local\share\cy"
)

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

$arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    "AMD64" { "x86_64" }
    "ARM64" { "aarch64" }
    default { Write-Err "Unsupported architecture: $env:PROCESSOR_ARCHITECTURE" }
}

$triple = "$arch-pc-windows-msvc"
$asset = "cy-${triple}.zip"

Write-Info "CY-CLI Windows Installer v2 (self-updating)"
Write-Info "Architecture: $arch ($triple)"

if ($env:CY_VERSION) {
    $version = $env:CY_VERSION
} else {
    $apiUrl = "https://api.github.com/repos/$Repo/releases/latest"
    try {
        $release = Invoke-RestMethod -Uri $apiUrl -Method Get
        $version = $release.tag_name.TrimStart('v')
    } catch {
        Write-Err "Could not determine latest release. Is the repo public? Specify CY_VERSION env var."
    }
}

Write-Info "Version: $version"

$baseUrl = "https://github.com/$Repo/releases/download/v$version"
$assetUrl = "$baseUrl/$asset"

$tmpdir = Join-Path $env:TEMP "cy-install-$(New-Guid)"
New-Item -ItemType Directory -Path $tmpdir -Force | Out-Null
trap { Remove-Item -Recurse -Force $tmpdir }

Write-Info "Downloading $asset..."
Invoke-WebRequest -Uri $assetUrl -OutFile (Join-Path $tmpdir $asset) -UseBasicParsing

Write-Info "Extracting..."
Expand-Archive -Path (Join-Path $tmpdir $asset) -DestinationPath $tmpdir -Force

Write-Info "Installing initial binary to $StoreDir\bin..."
New-Item -ItemType Directory -Path (Join-Path $StoreDir "bin") -Force | Out-Null
$cyExe = Join-Path $tmpdir "cy.exe"
if (-not (Test-Path $cyExe)) {
    $cyExe = Join-Path $tmpdir "cy"
}
Copy-Item $cyExe (Join-Path $StoreDir "bin\cy.exe") -Force
Set-Content -Path (Join-Path $StoreDir "VERSION") -Value $version

Write-Info "Installing self-updating wrapper to $InstallDir\cy.ps1..."
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

$wrapperContent = @'
# CY-CLI Self-Updating Wrapper (embedded)
param()
$ErrorActionPreference = "Stop"
$CY_REPO = $env:CY_REPO -ne $null ? $env:CY_REPO : "SYMBIOTYC/CY-CLI-releases"
$CY_INSTALL_DIR = $env:CY_INSTALL_DIR -ne $null ? $env:CY_INSTALL_DIR : "$env:USERPROFILE\.local\share\cy"
$CY_VERSION_FILE = Join-Path $CY_INSTALL_DIR "VERSION"
$CY_BIN_DIR = Join-Path $CY_INSTALL_DIR "bin"
if (-not (Test-Path $CY_BIN_DIR)) { New-Item -ItemType Directory -Path $CY_BIN_DIR -Force | Out-Null }

function Get-LatestTag {
    $response = Invoke-RestMethod -Uri "https://api.github.com/repos/${CY_REPO}/releases/latest" -Method Get
    return $response.tag_name
}

function Get-LocalVersion {
    if (Test-Path $CY_VERSION_FILE) { return Get-Content $CY_VERSION_FILE -Raw }
    return ""
}

function Download-And-Install {
    param([string]$Tag, [string]$AssetName)
    $url = "https://github.com/${CY_REPO}/releases/download/${Tag}/${AssetName}"
    $tmpdir = Join-Path $env:TEMP "cy-install-$(New-Guid)"
    New-Item -ItemType Directory -Path $tmpdir -Force | Out-Null
    trap { Remove-Item -Recurse -Force $tmpdir }
    Write-Host "[cy-wrapper] Downloading ${AssetName}..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmpdir $AssetName) -UseBasicParsing
    Expand-Archive -Path (Join-Path $tmpdir $AssetName) -DestinationPath $tmpdir -Force
    $cyExe = Join-Path $tmpdir "cy.exe"
    if (-not (Test-Path $cyExe)) { $cyExe = Join-Path $tmpdir "cy" }
    Copy-Item $cyExe (Join-Path $CY_BIN_DIR "cy.exe") -Force
    Set-Content -Path $CY_VERSION_FILE -Value $Tag.TrimStart('v')
    Write-Host "[cy-wrapper] Installed ${Tag}" -ForegroundColor Green
    Remove-Item -Recurse -Force $tmpdir
}

function Update-IfNeeded {
    try {
        $latestTag = Get-LatestTag
        if ([string]::IsNullOrEmpty($latestTag)) { return }
        $localVersion = Get-LocalVersion
        $cleanTag = $latestTag.TrimStart('v')
        if ($localVersion -ne $cleanTag) {
            Write-Host "[cy-wrapper] Update available: ${localVersion:-none} -> ${latestTag}" -ForegroundColor Yellow
            $assetName = "cy-x86_64-pc-windows-msvc.zip"
            if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { $assetName = "cy-aarch64-pc-windows-msvc.zip" }
            Download-And-Install -Tag $latestTag -AssetName $assetName
        }
    } catch { Write-Host "[cy-wrapper] Update check failed: $_" -ForegroundColor Red }
}

function Find-FallbackCy {
    $managedPath = Join-Path $CY_BIN_DIR "cy.exe"
    if (Test-Path $managedPath) { return $managedPath }
    $fallback = Get-Command cy -ErrorAction SilentlyContinue
    if ($fallback) { return $fallback.Source }
    return $null
}

Update-IfNeeded
$cyBinary = Find-FallbackCy
if (-not $cyBinary) { Write-Host "[cy-wrapper] No cy binary found" -ForegroundColor Red; exit 1 }
$argsList = @($args)
& $cyBinary @argsList
'@

Set-Content -Path (Join-Path $InstallDir "cy.ps1") -Value $wrapperContent -Encoding UTF8

$cmdShim = "@echo off`r`npowershell -ExecutionPolicy Bypass -File `"%USERPROFILE%\.local\bin\cy.ps1`" %*"
Set-Content -Path (Join-Path $InstallDir "cy.cmd") -Value $cmdShim -Encoding ASCII

Write-Info "Installed cy to $InstallDir\cy.ps1 (and cy.cmd shim)"
Write-Info "Binary stored at $StoreDir\bin\cy.exe"

$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($currentPath -notlike "*$InstallDir*") {
    Write-Warn "$InstallDir is not in your PATH."
    Write-Warn "Add it: [Environment]::SetEnvironmentVariable('Path', `$env:Path + ';$InstallDir', 'User')"
}

Write-Info "Verifying..."
& powershell -ExecutionPolicy Bypass -File (Join-Path $InstallDir "cy.ps1") --version

Write-Info "Installation complete! cy will auto-update on each launch."
