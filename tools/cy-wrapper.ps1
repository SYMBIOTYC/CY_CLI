# CY-CLI Self-Updating Wrapper (PowerShell)
# Проверяет обновления на GitHub Releases и автоматически обновляет бинарник.

param()

$ErrorActionPreference = "Stop"

$CY_REPO = if ($env:CY_REPO) { $env:CY_REPO } else { "SYMBIOTYC/CY-CLI" }
$CY_INSTALL_DIR = if ($env:CY_INSTALL_DIR) { $env:CY_INSTALL_DIR } else { "$env:USERPROFILE\.local\share\cy" }
$CY_VERSION_FILE = Join-Path $CY_INSTALL_DIR "VERSION"
$CY_BIN_DIR = Join-Path $CY_INSTALL_DIR "bin"

if (-not (Test-Path $CY_BIN_DIR)) {
    New-Item -ItemType Directory -Path $CY_BIN_DIR -Force | Out-Null
}

function Get-LatestTag {
    $response = Invoke-RestMethod -Uri "https://api.github.com/repos/${CY_REPO}/releases/latest" -Method Get
    return $response.tag_name
}

function Get-LocalVersion {
    if (Test-Path $CY_VERSION_FILE) {
        return Get-Content $CY_VERSION_FILE -Raw
    }
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
    
    $zipPath = Join-Path $tmpdir $AssetName
    Expand-Archive -Path $zipPath -DestinationPath $tmpdir -Force
    
    $cyExe = Join-Path $tmpdir "cy.exe"
    if (-not (Test-Path $cyExe)) {
        $cyExe = Join-Path $tmpdir "cy"
    }
    
    Copy-Item $cyExe (Join-Path $CY_BIN_DIR "cy.exe") -Force
    Set-Content -Path $CY_VERSION_FILE -Value $Tag.TrimStart('v')
    Write-Host "[cy-wrapper] Installed ${Tag}" -ForegroundColor Green
    
    Remove-Item -Recurse -Force $tmpdir
}

function Update-IfNeeded {
    try {
        $latestTag = Get-LatestTag
        if ([string]::IsNullOrEmpty($latestTag)) {
            Write-Host "[cy-wrapper] No releases found" -ForegroundColor Yellow
            return
        }
        
        $localVersion = Get-LocalVersion
        $cleanTag = $latestTag.TrimStart('v')
        
        if ($localVersion -ne $cleanTag) {
            Write-Host "[cy-wrapper] Update available: ${localVersion} -> ${latestTag}" -ForegroundColor Yellow
            $assetName = "cy-x86_64-pc-windows-msvc.zip"
            if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
                $assetName = "cy-aarch64-pc-windows-msvc.zip"
            }
            Download-And-Install -Tag $latestTag -AssetName $assetName
        }
    } catch {
        Write-Host "[cy-wrapper] Failed to check for updates: $_" -ForegroundColor Red
    }
}

function Find-FallbackCy {
    param()
    
    $managedPath = Join-Path $CY_BIN_DIR "cy.exe"
    if (Test-Path $managedPath) {
        return $managedPath
    }
    
    $fallback = Get-Command cy -ErrorAction SilentlyContinue
    if ($fallback) {
        return $fallback.Source
    }
    
    return $null
}

function Main {
    Update-IfNeeded
    
    $cyBinary = Find-FallbackCy
    if (-not $cyBinary) {
        Write-Host "[cy-wrapper] No cy binary found. Please run install script first." -ForegroundColor Red
        exit 1
    }
    
    $argsList = @($args)
    & $cyBinary @argsList
}

Main @args
