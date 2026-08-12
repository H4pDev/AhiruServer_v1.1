$ErrorActionPreference = "Stop"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " AhiruServer Installer Build Script" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. PyInstaller setup
Write-Host "[1/4] Preparing PyInstaller..." -ForegroundColor Yellow
pip install pyinstaller --quiet

# 2. Build AhiruServer.exe
Write-Host "[2/4] Building AhiruServer.exe..." -ForegroundColor Yellow
python -m PyInstaller --noconfirm --onefile --windowed --add-data "assets;assets" --name AhiruServer main.py

if (-not (Test-Path "dist/AhiruServer.exe")) {
    Write-Error "Failed to build dist/AhiruServer.exe"
}

# 3. Build GUI Installer (AhiruServer_Setup.exe)
Write-Host "[3/4] Building AhiruServer_Setup.exe..." -ForegroundColor Yellow
python -m PyInstaller --noconfirm --onefile --windowed --add-data "dist/AhiruServer.exe;." --name AhiruServer_Setup installer.py

if (-not (Test-Path "dist/AhiruServer_Setup.exe")) {
    Write-Error "Failed to build dist/AhiruServer_Setup.exe"
}

# 4. 出力先フォルダ (installer/) への配置
Write-Host "[4/5] Copying installer to installer/..." -ForegroundColor Yellow
$installerDir = Join-Path $PSScriptRoot "installer"
if (-not (Test-Path $installerDir)) {
    New-Item -ItemType Directory -Path $installerDir | Out-Null
}

$destPath = Join-Path $installerDir "AhiruServer_Setup.exe"
Copy-Item -Path "dist/AhiruServer_Setup.exe" -Destination $destPath -Force

# 5. 中間生成物 (build/, dist/, *.spec) のクリーンアップ
Write-Host "[5/5] Cleaning up temporary build artifacts..." -ForegroundColor Yellow
Remove-Item -Path "build" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path "dist" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path "*.spec" -Force -ErrorAction SilentlyContinue

Write-Host "==========================================" -ForegroundColor Green
Write-Host " SUCCESS! Installer Path:" -ForegroundColor Green
Write-Host " $destPath" -ForegroundColor White
Write-Host "==========================================" -ForegroundColor Green
