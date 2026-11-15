@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title AhiruServer セットアップ

echo ======================================================
echo    AhiruServer セットアップ (Windowsセキュリティ対応版)
echo ======================================================
echo.

cd /d "%~dp0"

:: 1. Pythonの検出 (Windows Storeのダミーエイリアス対策としてimport sysで検証)
set "PY_CMD="
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
) else (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=py -3"
    )
)

:: 2. Pythonが見つからない場合はwingetで公式Pythonを導入
if not defined PY_CMD (
    echo [情報] Pythonが見つかりませんでした。公式Pythonのインストールを試みます...
    winget --version >nul 2>&1
    if errorlevel 1 (
        echo [エラー] Pythonおよびwingetが見つかりません。
        echo 公式サイトからPython 3.10以降をインストールしてから、再度 setup.bat を実行してください。
        echo https://www.python.org/downloads/
        pause
        exit /b 1
    )

    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [エラー] Pythonの自動インストールに失敗しました。
        echo 公式サイトからPythonをインストールしてください: https://www.python.org/downloads/
        pause
        exit /b 1
    )

    :: 新規インストール直後のパスを探索
    if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
        set "PY_CMD="%LocalAppData%\Programs\Python\Python312\python.exe""
    ) else (
        py -3 -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            set "PY_CMD=py -3"
        ) else (
            echo [情報] Pythonのインストールは完了しました。
            echo 環境変数を反映するため、このウィンドウを閉じてから再度 setup.bat を実行してください。
            pause
            exit /b 0
        )
    )
)

echo [1/4] Pythonを確認しました: %PY_CMD%

:: 3. 仮想環境 (.venv) の作成
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] 仮想環境 ^(.venv^) を作成しています...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [エラー] 仮想環境の作成に失敗しました。
        pause
        exit /b 1
    )
) else (
    echo [2/4] 既存の仮想環境 ^(.venv^) を使用します。
)

:: 4. 依存パッケージのインストール
echo [3/4] 必要なパッケージをインストールしています...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [エラー] パッケージのインストールに失敗しました。ネットワーク接続を確認してください。
    pause
    exit /b 1
)

:: 5. ショートカットとアンインストーラーの作成
echo [4/4] ショートカットを作成しています...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$WshShell = New-Object -ComObject WScript.Shell; " ^
    "$Desktop = [Environment]::GetFolderPath('Desktop'); " ^
    "$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'; " ^
    "foreach ($dir in @($Desktop, $StartMenu)) { " ^
    "  if (Test-Path $dir) { " ^
    "    $sc = $WshShell.CreateShortcut((Join-Path $dir 'AhiruServer.lnk')); " ^
    "    $sc.TargetPath = '%~dp0run.bat'; " ^
    "    $sc.WorkingDirectory = '%~dp0'; " ^
    "    $sc.Description = 'AhiruServer - Minecraft LAN共有ツール'; " ^
    "    $sc.Save(); " ^
    "  } " ^
    "}"

:: アンインストーラー (ショートカットと仮想環境のみ削除)
(
    echo @echo off
    echo chcp 65001 ^>nul
    echo title AhiruServer アンインストール
    echo echo AhiruServer のショートカットと仮想環境を削除しています...
    echo powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = [Environment]::GetFolderPath('Desktop'); $s = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'; Remove-Item (Join-Path $d 'AhiruServer.lnk'),(Join-Path $s 'AhiruServer.lnk') -ErrorAction SilentlyContinue"
    echo if exist "%~dp0.venv" rmdir /s /q "%~dp0.venv"
    echo echo アンインストールが完了しました。フォルダ自体は手動で削除できます。
    echo pause
) > "%~dp0uninstall.bat"

echo.
echo ======================================================
echo    セットアップが正常に完了しました！
echo    デスクトップおよびスタートメニューにショートカットを作成しました。
echo ======================================================
echo.

choice /c YN /n /m "今すぐ AhiruServer を起動しますか？ [Y/N]: "
if errorlevel 2 goto :end
if errorlevel 1 (
    start "" "%~dp0run.bat"
)

:end
endlocal
