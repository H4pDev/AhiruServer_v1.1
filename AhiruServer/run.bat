@echo off
chcp 65001 >nul
setlocal

title AhiruServer 起動ランチャー

echo ======================================================
echo    AhiruServer 起動ランチャー (セキュリティ警告回避版)
echo ======================================================
echo.

cd /d "%~dp0"

:: 仮想環境が存在する場合はそのまま起動
if exist ".venv\Scripts\python.exe" (
    echo AhiruServer を起動しています...
    ".venv\Scripts\python.exe" main.py
    pause
    exit /b %errorlevel%
)

:: 仮想環境がない場合は setup.bat による初期セットアップを実行
if exist "setup.bat" (
    echo [情報] 初回セットアップが未完了のため setup.bat を実行します...
    call setup.bat
    exit /b %errorlevel%
)

:: setup.bat がない場合のフォールバック
python -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [エラー] Pythonが見つかりません。setup.bat を実行するか、Pythonをインストールしてください。
    pause
    exit /b 1
)

pip install -r requirements.txt --quiet

echo AhiruServer を起動しています...
python main.py

pause
