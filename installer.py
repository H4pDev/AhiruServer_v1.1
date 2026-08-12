# -*- coding: utf-8 -*-
"""
AhiruServer GUI インストーラー (UIレイアウト＆ボタン確定版)
"""

import os
import sys
import shutil
import subprocess
import threading
import traceback
import logging
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ログファイルの設定
log_path = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "AhiruServer_install.log")
logging.basicConfig(
    filename=log_path,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

logging.info("================ インストーラー起動 ================")
logging.info(f"Pythonバージョン: {sys.version}")
logging.info(f"実行可能パス: {sys.executable}")

def get_resource_path(relative_path):
    """ PyInstaller のリソースパスを取得 """
    if getattr(sys, 'frozen', False):
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
        dist_path = os.path.join(base_path, 'dist', relative_path)
        if os.path.exists(dist_path):
            return dist_path
    
    target_path = os.path.join(base_path, relative_path)
    logging.info(f"リソース検索: relative='{relative_path}' -> target='{target_path}' (存在: {os.path.exists(target_path)})")
    return target_path

def create_shortcut(target_exe, shortcut_path, description="AhiruServer"):
    """ PowerShellを使用してショートカットを作成 """
    shortcut_dir = os.path.dirname(shortcut_path)
    if not os.path.exists(shortcut_dir):
        os.makedirs(shortcut_dir, exist_ok=True)

    ps_script = f'''
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
    $Shortcut.TargetPath = "{target_exe}"
    $Shortcut.WorkingDirectory = "{os.path.dirname(target_exe)}"
    $Shortcut.Description = "{description}"
    $Shortcut.Save()
    '''
    try:
        logging.info(f"ショートカット作成開始: {shortcut_path}")
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            check=True,
            timeout=10
        )
        logging.info(f"ショートカット作成完了: {shortcut_path}")
    except Exception as e:
        logging.error(f"ショートカット作成エラー ({shortcut_path}): {e}")

def kill_running_instances():
    """ 既に起動している AhiruServer.exe を安全に終了 """
    try:
        logging.info("既存の AhiruServer.exe プロセスチェック...")
        subprocess.run(
            ["taskkill", "/F", "/IM", "AhiruServer.exe"],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        logging.warning(f"プロセス終了試行中の警告: {e}")

class InstallerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AhiruServer セットアップ")
        self.root.geometry("540x440")
        self.root.resizable(False, False)

        # メッセージキュー (スレッド間通信用)
        self.msg_queue = queue.Queue()

        # ウィンドウを最前面に持ってきて背面隠れを予防
        self.root.attributes("-topmost", True)
        self.root.after(800, lambda: self.root.attributes("-topmost", False))

        # フォント設定
        self.font_main = ("Meiryo UI", 9)
        self.font_title = ("Meiryo UI", 14, "bold")
        self.font_header = ("Meiryo UI", 10, "bold")

        # デフォルトインストールパス (%LOCALAPPDATA%\AhiruServer)
        default_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AhiruServer")
        self.install_dir_var = tk.StringVar(value=default_dir)
        self.desktop_shortcut_var = tk.BooleanVar(value=True)
        self.startmenu_shortcut_var = tk.BooleanVar(value=True)
        self.launch_after_var = tk.BooleanVar(value=True)

        self.setup_ui()
        # キューの監視を開始
        self.root.after(100, self.check_queue)

    def setup_ui(self):
        # 1. ヘッダーフレーム (上部固定)
        header_frame = tk.Frame(self.root, bg="#1C202B", height=65)
        header_frame.pack(fill="x", side="top")
        header_frame.pack_propagate(False)

        title_container = tk.Frame(header_frame, bg="#1C202B", padx=20)
        title_container.pack(fill="both", expand=True)

        title_yellow = tk.Label(
            title_container, text="🐥 AhiruServer",
            font=self.font_title, fg="#FBBF24", bg="#1C202B", anchor="w"
        )
        title_yellow.pack(side="left")

        title_sub = tk.Label(
            title_container, text=" セットアップ",
            font=self.font_title, fg="#E5E7EB", bg="#1C202B", anchor="w"
        )
        title_sub.pack(side="left")

        # 2. 下部ボタンフレーム (下部固定 - pack順序を前にして見切れを絶対に防止！)
        self.btn_frame = tk.Frame(self.root, padx=20, pady=12, bg="#F0F0F0")
        self.btn_frame.pack(fill="x", side="bottom")

        self.cancel_btn = tk.Button(
            self.btn_frame, text="キャンセル", font=self.font_main,
            padx=12, pady=4, command=self.root.destroy
        )
        self.cancel_btn.pack(side="right", padx=(8, 0))

        # 目立つアヒルイエローのインストールボタン
        self.install_btn = tk.Button(
            self.btn_frame, text=" インストール ", font=("Meiryo UI", 10, "bold"),
            bg="#F59E0B", fg="#121212", activebackground="#D97706", activeforeground="#121212",
            relief="raised", bd=2, padx=16, pady=4, command=self.start_installation
        )
        self.install_btn.pack(side="right")

        # 3. メインコンテンツフレーム (中央残り全域)
        self.main_frame = tk.Frame(self.root, padx=20, pady=12)
        self.main_frame.pack(fill="both", expand=True)

        # 説明文
        desc_label = tk.Label(
            self.main_frame,
            text="AhiruServer をコンピューターにインストールします。\nインストール先フォルダとオプションを確認し、「インストール」を押してください。",
            font=self.font_main, justify="left", anchor="w"
        )
        desc_label.pack(fill="x", pady=(0, 10))

        # インストール先選択
        dir_frame = tk.LabelFrame(self.main_frame, text="インストール先フォルダ", font=self.font_header, padx=10, pady=8)
        dir_frame.pack(fill="x", pady=(0, 10))

        dir_entry = tk.Entry(dir_frame, textvariable=self.install_dir_var, font=self.font_main)
        dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        browse_btn = tk.Button(dir_frame, text="参照...", font=self.font_main, command=self.browse_directory)
        browse_btn.pack(side="right")

        # オプション設定
        opt_frame = tk.LabelFrame(self.main_frame, text="オプション", font=self.font_header, padx=10, pady=5)
        opt_frame.pack(fill="x", pady=(0, 10))

        chk_desktop = ttk.Checkbutton(opt_frame, text="デスクトップにショートカットを作成する", variable=self.desktop_shortcut_var)
        chk_desktop.pack(anchor="w", pady=2)

        chk_start = ttk.Checkbutton(opt_frame, text="スタートメニューに登録する", variable=self.startmenu_shortcut_var)
        chk_start.pack(anchor="w", pady=2)

        # プログレスバー & ステータス
        self.status_label = tk.Label(self.main_frame, text="右下の「インストール」ボタンを押してください", font=self.font_main, anchor="w", fg="#333333")
        self.status_label.pack(fill="x", pady=(5, 2))

        self.progress = ttk.Progressbar(self.main_frame, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 5))

    def browse_directory(self):
        selected = filedialog.askdirectory(title="インストール先フォルダを選択", initialdir=self.install_dir_var.get())
        if selected:
            self.install_dir_var.set(os.path.normpath(selected))

    def start_installation(self):
        target_dir = self.install_dir_var.get().strip()
        logging.info(f"インストールボタン押下: target_dir='{target_dir}'")
        if not target_dir:
            messagebox.showerror("エラー", "インストール先フォルダを指定してください。")
            return

        self.install_btn.config(state="disabled", bg="#CCCCCC")
        self.cancel_btn.config(state="disabled")
        self.progress["value"] = 5
        self.status_label.config(text="処理を開始しています...", fg="#333333")

        # ワーカースレッドの起動
        worker = threading.Thread(target=self.run_install_process, args=(target_dir,), daemon=True)
        worker.start()

    def run_install_process(self, target_dir):
        """ バックグラウンドスレッドで重い処理を実行し、結果をQueueに送る """
        try:
            # 1. 既存プロセス終了
            self.msg_queue.put(("status", "既存プロセスを確認中...", 15))
            kill_running_instances()

            # 2. フォルダ作成
            self.msg_queue.put(("status", "インストール先フォルダを準備中...", 30))
            logging.info(f"フォルダ作成: {target_dir}")
            os.makedirs(target_dir, exist_ok=True)

            # 3. EXEファイルの抽出・コピー
            self.msg_queue.put(("status", "AhiruServer.exe をコピー中...", 50))
            src_exe = get_resource_path("AhiruServer.exe")
            if not os.path.exists(src_exe):
                err = f"同梱の AhiruServer.exe が見つかりません。\n検索パス: {src_exe}"
                logging.error(err)
                raise FileNotFoundError(err)

            dest_exe = os.path.join(target_dir, "AhiruServer.exe")
            logging.info(f"ファイルコピー開始: {src_exe} -> {dest_exe}")
            shutil.copy2(src_exe, dest_exe)
            logging.info("ファイルコピー成功")

            # 4. アンインストーラー作成
            self.msg_queue.put(("status", "アンインストーラーを生成中...", 70))
            uninstall_bat = os.path.join(target_dir, "uninstall.bat")
            desktop_link = os.path.join(os.path.expanduser("~"), "Desktop", "AhiruServer.lnk")
            startmenu_dir = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")
            startmenu_link = os.path.join(startmenu_dir, "AhiruServer.lnk")

            uninstall_script = f"""@echo off
chcp 65001 >nul
title AhiruServer アンインストール
echo AhiruServer をアンインストールしています...
timeout /t 1 /nobreak >nul
del /f /q "{desktop_link}" 2>nul
del /f /q "{startmenu_link}" 2>nul
cd /d "%TEMP%"
rmdir /s /q "{target_dir}"
echo.
echo アンインストールが完了しました。
pause
"""
            with open(uninstall_bat, "w", encoding="utf-8") as f:
                f.write(uninstall_script)
            logging.info("アンインストーラー生成完了")

            # 5. ショートカットの作成
            self.msg_queue.put(("status", "ショートカットを作成中...", 85))
            if self.desktop_shortcut_var.get():
                create_shortcut(dest_exe, desktop_link, "AhiruServer - LANトンネリング共有ツール")

            if self.startmenu_shortcut_var.get():
                create_shortcut(dest_exe, startmenu_link, "AhiruServer - LANトンネリング共有ツール")

            self.msg_queue.put(("status", "インストール完了！", 100))
            logging.info("インストール成功！")
            self.msg_queue.put(("complete", dest_exe))

        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f"インストール例外発生: {e}\n{tb}")
            self.msg_queue.put(("error", str(e), tb))

    def check_queue(self):
        """ メインUIスレッドで定期的にQueueを消化して安全にUI更新 """
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                msg_type = msg[0]

                if msg_type == "status":
                    _, text, val = msg
                    self.status_label.config(text=text)
                    self.progress["value"] = val
                    logging.info(f"UI更新 (Progress: {val}%): {text}")

                elif msg_type == "complete":
                    _, dest_exe = msg
                    self.show_completion_page(dest_exe)

                elif msg_type == "error":
                    _, err_msg, detail = msg
                    self.show_error(err_msg, detail)

                self.msg_queue.task_done()
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.check_queue)

    def show_completion_page(self, dest_exe):
        self.dest_exe = dest_exe

        # UI要素のクリア
        for widget in self.main_frame.winfo_children():
            widget.destroy()
        for widget in self.btn_frame.winfo_children():
            widget.destroy()

        # 完了メッセージ
        complete_label = tk.Label(
            self.main_frame,
            text="インストールが正常に完了しました！",
            font=self.font_title, fg="#27AE60", anchor="w"
        )
        complete_label.pack(fill="x", pady=(10, 15))

        info_label = tk.Label(
            self.main_frame,
            text=f"インストール先:\n{os.path.dirname(dest_exe)}",
            font=self.font_main, justify="left", anchor="w", fg="#333333"
        )
        info_label.pack(fill="x", pady=(0, 20))

        chk_launch = ttk.Checkbutton(self.main_frame, text="AhiruServer を今すぐ起動する", variable=self.launch_after_var)
        chk_launch.pack(anchor="w", pady=10)

        finish_btn = tk.Button(
            self.btn_frame, text=" 完了 ", font=("Meiryo UI", 10, "bold"),
            bg="#27AE60", fg="white", padx=16, pady=4, command=self.finish_installation
        )
        finish_btn.pack(side="right")

    def finish_installation(self):
        if self.launch_after_var.get() and hasattr(self, 'dest_exe') and os.path.exists(self.dest_exe):
            logging.info(f"インストール完了後の起動: {self.dest_exe}")
            subprocess.Popen([self.dest_exe], cwd=os.path.dirname(self.dest_exe))
        self.root.destroy()

    def show_error(self, err_msg, detail=""):
        logging.error(f"エラーダイアログ表示: {err_msg}")
        messagebox.showerror("インストール失敗", f"エラーが発生しました:\n{err_msg}\n\n詳細ログ: {log_path}")
        self.install_btn.config(state="normal", bg="#27AE60")
        self.cancel_btn.config(state="normal")
        self.status_label.config(text="エラーが発生しました", fg="red")

if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = InstallerApp(root)
        root.mainloop()
    except Exception as outer_e:
        outer_tb = traceback.format_exc()
        logging.critical(f"FATAL UI ERROR: {outer_e}\n{outer_tb}")
        messagebox.showerror("起動エラー", f"致命的なエラーが発生しました:\n{outer_e}")
