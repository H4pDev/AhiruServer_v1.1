@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title AhiruServer + Cloudflare Tunnel 起動ランチャー

echo ======================================================
echo    🐥 AhiruServer - Cloudflare 公開ランチャー
echo ======================================================
echo.

cd /d "%~dp0"

:: 1. Python環境チェック (.venv優先)
set "PY_CMD="
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
) else (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=python"
    ) else (
        py -3 -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            set "PY_CMD=py -3"
        )
    )
)

if not defined PY_CMD (
    echo [エラー] Pythonが見つかりません。先に setup.bat を実行するか、Pythonをインストールしてください。
    pause
    exit /b 1
)

:: 2. 依存パッケージのチェック (.venv未構築時のみ)
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 依存関係を確認中...
    %PY_CMD% -m pip install -r requirements.txt --quiet
) else (
    echo [1/3] 仮想環境 ^(.venv^) を使用します。
)

:: 3. cloudflared の準備
echo [2/3] Cloudflare Tunnel (cloudflared) を確認中...
set CLOUDFLARED_CMD=cloudflared
where cloudflared >nul 2>&1
if errorlevel 1 (
    if exist "cloudflared.exe" (
        set CLOUDFLARED_CMD=.\cloudflared.exe
    ) else (
        echo cloudflared が見つかりません。自動ダウンロードしています...
        powershell -Command "Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe'"
        if exist "cloudflared.exe" (
            set CLOUDFLARED_CMD=.\cloudflared.exe
            echo cloudflared のダウンロードが完了しました。
        ) else (
            echo [警告] cloudflared のダウンロードに失敗しました。ローカルモードのみで起動します。
            set CLOUDFLARED_CMD=
        )
    )
)

:: 4. AhiruServer バックエンドの起動 (バックグラウンド)
echo [3/3] AhiruServer を起動中 (ポート 8000)...
start /b %PY_CMD% main.py

timeout /t 2 /nobreak >nul

:: 5. Cloudflare Tunnel の実行
if not "%CLOUDFLARED_CMD%"=="" (
    echo.
    echo ======================================================
    echo   Cloudflare Tunnel を起動しています...
    echo   以下に表示される「https://*.trycloudflare.com」のURLに
    echo   アクセスしてどこからでもWeb管理画面を利用できます！
    echo ======================================================
    echo.
    %CLOUDFLARED_CMD% tunnel --url http://127.0.0.1:8000
) else (
    echo ローカルブラウザで http://127.0.0.1:8000 を開きます。
    pause
)
