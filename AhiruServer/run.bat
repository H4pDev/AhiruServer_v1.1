@echo off
chcp 65001 >nul
setlocal

title AhiruServer 起動ランチャー

echo ======================================================
echo    🐥 AhiruServer 起動ランチャー (セキュリティ警告回避版)
echo ======================================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Pythonが見つかりません。Pythonがインストールされているか確認してください。
    pause
    exit /b 1
)

pip install -r requirements.txt --quiet

echo AhiruServer を起動しています...
python main.py

pause
