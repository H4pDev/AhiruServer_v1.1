"""
AhiruServer - Minecraft LAN共有GUIアプリケーション
ポート開放なしでMinecraftのLAN公開ワールドを外部共有するためのe4mc互換クライアント。

使い方:
  1. pip install -r requirements.txt
  2. python main.py
  3. MinecraftでLANに公開 → 表示されたポート番号を入力 → 「公開開始」を押す
  4. 表示されたアドレスを友人に共有する

プロトコル:
  e4mc (Dialtone/QUIC) プロトコルを使用。
  1. ブローカーAPI (https://broker.e4mc.link/getBestRelay) で最適リレーを取得
  2. QUICでリレーサーバーに接続 (ALPN: "e4mc-dialtone")
  3. 制御ストリームでJSON制御メッセージを交換
  4. 各プレイヤー接続は新しいQUICストリームとして確立
"""

import asyncio
import atexit
import json
import logging
import ssl
import os
import sys
import threading
import urllib.request
from typing import Optional, Callable

import customtkinter as ctk
from PIL import Image

def resource_path(relative_path: str) -> str:
    """ PyInstaller実行環境および通常環境に対応するリソース絶対パスを返却 """
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_path, relative_path)

# aioquicのインポート
try:
    from aioquic.asyncio import connect as quic_connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import (
        QuicEvent,
        StreamDataReceived,
        StreamReset,
        ConnectionTerminated,
        HandshakeCompleted,
    )
except ImportError:
    print("エラー: aioquicライブラリが見つかりません。")
    print("以下のコマンドでインストールしてください:")
    print("  pip install aioquic>=1.3.0")
    sys.exit(1)

# ロガー設定
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AhiruServer")

# =============================================================================
# 定数
# =============================================================================

# e4mcブローカーAPI（最適なリレーサーバーを取得）
E4MC_BROKER_URL = "https://broker.e4mc.link/getBestRelay"

# QUICプロトコル識別子（ALPN）
E4MC_ALPN = "quiclime"

# デフォルトの接続先ホスト（ローカルMinecraftサーバー）
LOCAL_HOST = "127.0.0.1"

# ステータス定義
STATUS_DISCONNECTED = "未接続"
STATUS_CONNECTING = "接続中..."
STATUS_ONLINE = "公開中（オンライン）"
STATUS_ERROR = "エラー"

# ステータスに対応する色 (状況に応じて直感的なカラーを表示)
STATUS_COLORS = {
    STATUS_DISCONNECTED: "#888888",  # グレー (未接続)
    STATUS_CONNECTING: "#F39C12",    # アンバー/オレンジ (接続中)
    STATUS_ONLINE: "#2ECC71",        # エメラルドグリーン (公開中/オンライン)
    STATUS_ERROR: "#E74C3C",         # レッド (エラー)
}

# GUIカラーパレット (Monochrome Simple Design)
COLOR_BG_CANVAS = "#121212"        # メイン背景 (ダーク)
COLOR_BG_PANEL = "#1e1e1e"         # パネル背景
COLOR_BG_HERO = "#1e1e1e"          # ヘッダー背景
COLOR_BG_SURFACE = "#242424"       # カード/入力枠背景
COLOR_BG_CARBON = "#181818"        # ログ表示部背景
COLOR_ACCENT_SIGNAL = "#ffffff"    # シグナル (白)
COLOR_ACCENT_AMBER = "#cccccc"     # アンバー相当 (ライトグレー)
COLOR_ACCENT_PRIMARY = "#ffffff"   # プライマリアクセント (純白)
COLOR_ACCENT_PRIMARY_HOVER = "#dddddd" # プライマリアクセントホバー
COLOR_ACCENT_RED = "#333333"       # 停止ボタン (ダークグレー)
COLOR_BORDER = "#333333"           # 枠線
COLOR_TEXT_INK = "#ffffff"         # タイトル/インクテキスト
COLOR_TEXT_SOFT = "#aaaaaa"        # サブタイトルテキスト
COLOR_TEXT_BODY = "#cccccc"        # 本文テキスト
COLOR_TEXT_MUTED = "#777777"       # 補足テキスト
COLOR_TEXT_ON_DARK = "#ffffff"     # 暗色背景上のテキスト

# UIフォント設定
FONT_FAMILY = "Meiryo"
FONT_TITLE = "Meiryo"
FONT_NUMBER = "Meiryo"


# =============================================================================
# E4mcProtocol - QUIC通信プロトコルハンドラ
# =============================================================================

class E4mcProtocol(QuicConnectionProtocol):
    """
    e4mc (Dialtone) QUICプロトコルの実装。

    制御ストリーム (stream_id=0):
      クライアント → サーバー:
        {"kind": "request_domain_assignment"}
      サーバー → クライアント:
        {"kind": "domain_assignment_complete", "domain": "xxx.e4mc.link"}

    データストリーム (stream_id >= 4):
      サーバーが新しいストリームをオープンする = 新しいプレイヤー接続
      ストリーム上のデータ = MinecraftのTCPデータ（双方向）
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # コールバック関数群（外部から設定）
        self.on_domain_assigned: Optional[Callable[[str], None]] = None
        self.on_status_change: Optional[Callable[[str, str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None

        # ローカルポート番号
        self.local_port: int = 25565

        # ストリームID → TCP WriterのマッピングとStreamReader保持
        self._tcp_writers: dict[int, asyncio.StreamWriter] = {}
        self._tcp_relay_tasks: dict[int, asyncio.Task] = {}

        # 制御ストリームID
        self._control_stream_id: Optional[int] = None

        # バッファ（制御ストリームのデータをバッファリング）
        self._control_buffer = b""

        # イベントループ参照
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # 割り当てドメイン
        self.domain: Optional[str] = None

    def connection_made(self, transport):
        """QUIC接続が確立された時のコールバック"""
        super().connection_made(transport)
        self._loop = asyncio.get_event_loop()

    async def start_control(self):
        """制御ストリームを開いてドメイン割り当てをリクエストする"""
        # クライアントから双方向ストリームをオープン
        self._control_stream_id = self._quic.get_next_available_stream_id()
        logger.info(f"制御ストリームを開設: stream_id={self._control_stream_id}")

        # ドメイン割り当てリクエストを送信
        request = json.dumps({"kind": "request_domain_assignment"}).encode("utf-8")
        # 長さプレフィックス (ULEB128 Varint) + JSONデータ
        length_prefix = self._encode_varint(len(request))
        self._quic.send_stream_data(
            self._control_stream_id, length_prefix + request, end_stream=False
        )
        self.transmit()
        self._emit_log("ドメイン割り当てをリクエストしました")

    def quic_event_received(self, event: QuicEvent):
        """QUICイベントを受信した時の処理"""
        if isinstance(event, HandshakeCompleted):
            logger.info("QUICハンドシェイク完了")
            self._emit_log("リレーサーバーに接続しました")
            # 制御ストリームを開始
            asyncio.ensure_future(self.start_control())

        elif isinstance(event, StreamDataReceived):
            stream_id = event.stream_id
            data = event.data

            if stream_id == self._control_stream_id:
                # 制御ストリームのデータ
                self._handle_control_data(data)
            else:
                # データストリーム（プレイヤー接続）
                asyncio.ensure_future(
                    self._handle_player_data(stream_id, data, event.end_stream)
                )

        elif isinstance(event, StreamReset):
            stream_id = event.stream_id
            logger.info(f"ストリームリセット: stream_id={stream_id}")
            if stream_id != self._control_stream_id:
                asyncio.ensure_future(self._close_player_stream(stream_id))

        elif isinstance(event, ConnectionTerminated):
            logger.info(f"QUIC接続終了: {event.reason_phrase}")
            self._emit_log(f"接続終了: {event.reason_phrase or '不明'}")
            self._emit_status(STATUS_DISCONNECTED)

    def _encode_varint(self, value: int) -> bytes:
        """整数をULEB128 Varintにエンコードする"""
        res = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                res.append(byte | 0x80)
            else:
                res.append(byte)
                break
        return bytes(res)

    def _decode_varint(self, data: bytes):
        """バイト列からULEB128 Varintをデコードする。返り値は (値, 読み込んだバイト数)"""
        value = 0
        shift = 0
        for i, byte in enumerate(data):
            value |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                return value, i + 1
            shift += 7
        return None, 0

    def _handle_control_data(self, data: bytes):
        """制御ストリームからのデータを処理する"""
        self._control_buffer += data

        # 長さプレフィックス(Varint)付きメッセージを解析
        while self._control_buffer:
            msg_len, bytes_read = self._decode_varint(self._control_buffer)
            if msg_len is None:
                break  # 長さプレフィックスがまだ完全に届いていない

            if len(self._control_buffer) < bytes_read + msg_len:
                break  # メッセージ全体がまだ届いていない

            msg_bytes = self._control_buffer[bytes_read : bytes_read + msg_len]
            self._control_buffer = self._control_buffer[bytes_read + msg_len :]

            try:
                msg = json.loads(msg_bytes.decode("utf-8"))
                self._process_control_message(msg)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(f"制御メッセージの解析エラー: {e}")

    def _process_control_message(self, msg: dict):
        """制御メッセージを処理する"""
        kind = msg.get("kind", "")

        if kind == "domain_assignment_complete":
            self.domain = msg.get("domain", "不明")
            logger.info(f"ドメイン割り当て完了: {self.domain}")
            self._emit_log(f"共有アドレス: {self.domain}")
            self._emit_status(STATUS_ONLINE)
            if self.on_domain_assigned:
                self.on_domain_assigned(self.domain)

        elif kind == "has_capabilities":
            caps = msg.get("caps", [])
            logger.info(f"サーバーケイパビリティ: {caps}")

        elif kind == "request_message_broadcast":
            message = msg.get("message", "")
            logger.info(f"ブロードキャストメッセージ: {message}")
            self._emit_log(f"📢 {message}")

        elif kind == "ticket_registered":
            logger.info("チケット登録完了")

        else:
            logger.info(f"未知の制御メッセージ: {msg}")

    async def _handle_player_data(self, stream_id: int, data: bytes, end_stream: bool):
        """プレイヤーのデータストリームを処理する"""
        # 新しいストリーム = 新しいプレイヤー接続
        if stream_id not in self._tcp_writers:
            logger.info(f"新しいプレイヤー接続: stream_id={stream_id}")
            self._emit_log(f"プレイヤー接続 (ストリーム:{stream_id})")
            await self._open_tcp_connection(stream_id)

        # TCPにデータ転送
        writer = self._tcp_writers.get(stream_id)
        if writer and not writer.is_closing() and data:
            try:
                writer.write(data)
                await writer.drain()
            except (ConnectionError, OSError) as e:
                logger.warning(f"TCP書き込みエラー (stream:{stream_id}): {e}")
                await self._close_player_stream(stream_id)
                return

        # ストリーム終了
        if end_stream:
            await self._close_player_stream(stream_id)

    async def _open_tcp_connection(self, stream_id: int):
        """ローカルMinecraftサーバーへのTCP接続を確立する"""
        try:
            reader, writer = await asyncio.open_connection(
                LOCAL_HOST, self.local_port
            )
            self._tcp_writers[stream_id] = writer

            # TCP→QUICのリレータスクを開始
            task = asyncio.ensure_future(
                self._tcp_to_quic_relay(stream_id, reader)
            )
            self._tcp_relay_tasks[stream_id] = task

        except ConnectionRefusedError:
            logger.error(
                f"ローカルMinecraftサーバー(ポート{self.local_port})に接続できません"
            )
            self._emit_log(
                f"⚠ ローカルサーバー(:{self.local_port})に接続拒否"
            )
            # QUICストリームを閉じる
            self._quic.reset_stream(stream_id, 0)
            self.transmit()
        except Exception as e:
            logger.error(f"TCP接続エラー (stream:{stream_id}): {e}")

    async def _tcp_to_quic_relay(self, stream_id: int, reader: asyncio.StreamReader):
        """TCPからデータを読み取り、QUICストリームに送信する"""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self._quic.send_stream_data(stream_id, data, end_stream=False)
                self.transmit()
        except (ConnectionError, OSError):
            pass
        except Exception as e:
            logger.debug(f"TCP→QUIC転送エラー (stream:{stream_id}): {e}")
        finally:
            # ストリーム終了を送信
            try:
                self._quic.send_stream_data(stream_id, b"", end_stream=True)
                self.transmit()
            except Exception:
                pass
            await self._close_player_stream(stream_id)

    async def _close_player_stream(self, stream_id: int):
        """プレイヤーのストリームとTCP接続を閉じる"""
        # TCP接続を閉じる
        writer = self._tcp_writers.pop(stream_id, None)
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        # リレータスクをキャンセル
        task = self._tcp_relay_tasks.pop(stream_id, None)
        if task and not task.done():
            task.cancel()

        self._emit_log(f"プレイヤー切断 (ストリーム:{stream_id})")

    async def cleanup(self):
        """すべてのリソースをクリーンアップする"""
        stream_ids = list(self._tcp_writers.keys())
        for sid in stream_ids:
            await self._close_player_stream(sid)

    def _emit_status(self, status: str):
        color = STATUS_COLORS.get(status, "#888888")
        if self.on_status_change:
            self.on_status_change(status, color)

    def _emit_log(self, message: str):
        logger.info(message)
        if self.on_log:
            self.on_log(message)


# =============================================================================
# E4mcTunnel - トンネルエンジン（ブローカー + QUIC接続の管理）
# =============================================================================

class E4mcTunnel:
    """
    e4mcブローカーAPIからリレーサーバーを取得し、
    QUICトンネルを確立してローカルMinecraftサーバーへの接続を中継するクラス。
    """

    def __init__(
        self,
        local_port: int,
        on_domain_assigned: Optional[Callable[[str], None]] = None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.local_port = local_port
        self.on_domain_assigned = on_domain_assigned
        self.on_status_change = on_status_change
        self.on_error = on_error
        self.on_log = on_log

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._protocol: Optional[E4mcProtocol] = None
        self.domain: Optional[str] = None

    def start(self):
        """バックグラウンドスレッドでトンネルを開始する"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """トンネルを停止する"""
        self._running = False

        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._cleanup(), self._loop
            )
            try:
                future.result(timeout=5.0)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

        self._thread = None
        self._loop = None
        self._protocol = None
        self.domain = None

    def _run_event_loop(self):
        """非同期イベントループを実行する"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._connect())
        except Exception as e:
            logger.error(f"イベントループエラー: {e}")
            self._emit_error(f"接続エラー: {e}")
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                self._loop.close()
            self._emit_status(STATUS_DISCONNECTED)

    async def _connect(self):
        """ブローカーからリレーを取得し、QUIC接続を確立する"""
        self._emit_status(STATUS_CONNECTING)
        self._emit_log("ブローカーAPIにリレー情報を問い合わせ中...")

        # ブローカーAPIからリレー情報を取得
        try:
            relay_info = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_relay_info
            )
        except Exception as e:
            self._emit_error(f"ブローカーAPIエラー: {e}")
            return

        relay_host = relay_info["host"]
        relay_port = relay_info["port"]
        self._emit_log(f"リレーサーバー: {relay_host}:{relay_port}")

        # QUIC設定
        configuration = QuicConfiguration(
            is_client=True,
            alpn_protocols=[E4MC_ALPN],
        )
        # 自己署名証明書を許可（e4mcリレーサーバーは独自証明書の場合がある）
        configuration.verify_mode = ssl.CERT_NONE

        self._emit_log("QUICトンネルを確立中...")

        try:
            async with quic_connect(
                relay_host,
                relay_port,
                configuration=configuration,
                create_protocol=E4mcProtocol,
            ) as protocol:
                self._protocol = protocol

                # コールバックを設定
                protocol.on_domain_assigned = self.on_domain_assigned
                protocol.on_status_change = self.on_status_change
                protocol.on_error = self.on_error
                protocol.on_log = self.on_log
                protocol.local_port = self.local_port

                self._emit_log("QUIC接続が確立されました")

                # 接続が維持されるまで待機
                while self._running:
                    await asyncio.sleep(1)

        except Exception as e:
            if self._running:
                logger.error(f"QUIC接続エラー: {e}")
                self._emit_error(f"QUIC接続エラー: {e}")

    def _fetch_relay_info(self) -> dict:
        """ブローカーAPIからリレーサーバー情報を取得する"""
        req = urllib.request.Request(
            E4MC_BROKER_URL,
            headers={"User-Agent": "AhiruServer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        logger.info(f"ブローカーレスポンス: {data}")
        return data

    async def _cleanup(self):
        """リソースをクリーンアップする"""
        if self._protocol:
            try:
                await self._protocol.cleanup()
            except Exception:
                pass

    def _emit_status(self, status: str):
        color = STATUS_COLORS.get(status, "#888888")
        if self.on_status_change:
            self.on_status_change(status, color)

    def _emit_error(self, message: str):
        self._emit_status(STATUS_ERROR)
        if self.on_error:
            self.on_error(message)

    def _emit_log(self, message: str):
        logger.info(message)
        if self.on_log:
            self.on_log(message)


# =============================================================================
# App - CustomTkinter GUIアプリケーション
# =============================================================================

class App(ctk.CTk):
    """
    AhiruServer のメインGUIウィンドウ。
    CustomTkinterを使用したモダンなダークテーマUI。
    """

    def __init__(self):
        super().__init__()

        # ウィンドウ基本設定
        self.title("AhiruServer - Minecraft LAN共有")
        self.geometry("520x720")
        self.minsize(480, 650)
        self.resizable(True, True)

        # ダークモード強制
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ウィンドウの背景色
        self.configure(fg_color=COLOR_BG_CANVAS)

        # トンネルエンジン
        self._tunnel: Optional[E4mcTunnel] = None
        self._is_connected = False
        self._assigned_domain: Optional[str] = None

        # UIを構築
        self._build_ui()

        # ウィンドウ閉じ時のクリーンアップ
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        atexit.register(self._force_cleanup)

    def _build_ui(self):
        """GUIコンポーネントを構築する"""

        # ============================
        # ヘッダー (Hero Panel)
        # ============================
        header_frame = ctk.CTkFrame(
            self, fg_color=COLOR_BG_HERO, corner_radius=0,
            border_width=0
        )
        header_frame.pack(fill="x", padx=24, pady=(24, 16))

        # アイコンとタイトルを配置する水平コンテナ
        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.pack(anchor="w", fill="x")

        # タイトルテキスト表示
        text_box = ctk.CTkFrame(title_box, fg_color="transparent")
        text_box.pack(side="left", fill="both", expand=True)

        title_label = ctk.CTkLabel(
            text_box,
            text="AhiruServer",
            font=ctk.CTkFont(family=FONT_FAMILY, size=32, weight="bold"),
            text_color=COLOR_TEXT_INK,
        )
        title_label.pack(anchor="w", pady=(0, 2))

        subtitle_label = ctk.CTkLabel(
            text_box,
            text="Minecraft LAN共有 — ポート開放不要",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            text_color=COLOR_TEXT_SOFT,
        )
        subtitle_label.pack(anchor="w", pady=(0, 0))

        # ============================
        # ポート入力エリア (Form Panel)
        # ============================
        port_frame = ctk.CTkFrame(
            self, fg_color=COLOR_BG_CANVAS, corner_radius=0,
            border_width=0
        )
        port_frame.pack(fill="x", padx=24, pady=8)

        port_label = ctk.CTkLabel(
            port_frame,
            text="LANポート番号",
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
            text_color=COLOR_TEXT_ON_DARK,
        )
        port_label.pack(anchor="w", pady=(0, 8))

        self._port_entry = ctk.CTkEntry(
            port_frame,
            placeholder_text="例: 55432",
            font=ctk.CTkFont(family=FONT_NUMBER, size=16, weight="bold"),
            height=48,
            fg_color=COLOR_BG_SURFACE,
            border_color=COLOR_BORDER,
            border_width=1,
            text_color=COLOR_TEXT_INK,
            placeholder_text_color="#888888",
            corner_radius=8,
        )
        self._port_entry.pack(fill="x")

        # ============================
        # 公開ボタン (Button Submit)
        # ============================
        self._toggle_btn = ctk.CTkButton(
            self,
            text="公開開始",
            font=ctk.CTkFont(family=FONT_FAMILY, size=16, weight="bold"),
            height=48,
            corner_radius=6,
            fg_color=COLOR_ACCENT_PRIMARY,
            hover_color=COLOR_ACCENT_PRIMARY_HOVER,
            text_color="#121212",
            command=self._toggle_connection,
            border_width=0,
        )
        self._toggle_btn.pack(fill="x", padx=24, pady=16)

        # ============================
        # 共有アドレス表示エリア (Info Box)
        # ============================
        addr_frame = ctk.CTkFrame(
            self, fg_color=COLOR_BG_SURFACE, corner_radius=12,
            border_width=1, border_color=COLOR_BORDER
        )
        addr_frame.pack(fill="x", padx=24, pady=8)

        addr_inner = ctk.CTkFrame(addr_frame, fg_color="transparent")
        addr_inner.pack(fill="x", padx=24, pady=24)

        self._domain_label = ctk.CTkLabel(
            addr_inner,
            text="---",
            font=ctk.CTkFont(family=FONT_NUMBER, size=24, weight="bold"),
            text_color=COLOR_ACCENT_PRIMARY,
            wraplength=400,
        )
        self._domain_label.pack(anchor="center", pady=(0, 16))

        self._copy_btn = ctk.CTkButton(
            addr_inner,
            text="アドレスをコピー",
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
            height=40,
            corner_radius=6,
            fg_color=COLOR_BG_SURFACE,
            hover_color="#333333",
            text_color=COLOR_TEXT_ON_DARK,
            border_width=1,
            border_color=COLOR_BORDER,
            command=self._copy_domain,
            state="disabled",
        )
        self._copy_btn.pack(fill="x")

        # ============================
        # ステータス表示 (System Tile)
        # ============================
        status_frame = ctk.CTkFrame(
            self, fg_color=COLOR_BG_SURFACE, corner_radius=12,
            border_width=1, border_color=COLOR_BORDER
        )
        status_frame.pack(fill="x", padx=24, pady=8)

        status_inner = ctk.CTkFrame(status_frame, fg_color="transparent")
        status_inner.pack(fill="x", padx=20, pady=16)

        status_header = ctk.CTkLabel(
            status_inner,
            text="ステータス",
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
            text_color=COLOR_TEXT_INK,
        )
        status_header.pack(side="left")

        self._status_label = ctk.CTkLabel(
            status_inner,
            text=f"●  {STATUS_DISCONNECTED}",
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
            text_color=STATUS_COLORS[STATUS_DISCONNECTED],
        )
        self._status_label.pack(side="right")

        # ============================
        # ログエリア (Carbon Command Layer)
        # ============================
        log_frame = ctk.CTkFrame(
            self, fg_color=COLOR_BG_CARBON, corner_radius=8,
            border_width=1, border_color=COLOR_BORDER
        )
        log_frame.pack(fill="both", expand=True, padx=24, pady=(16, 24))

        log_header = ctk.CTkLabel(
            log_frame,
            text="ログ出力",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_MUTED,
        )
        log_header.pack(anchor="w", padx=16, pady=(12, 0))

        self._log_textbox = ctk.CTkTextbox(
            log_frame,
            font=ctk.CTkFont(family=FONT_NUMBER, size=12),
            fg_color=COLOR_BG_CARBON,
            text_color=COLOR_TEXT_BODY,
            corner_radius=0,
            border_width=0,
            activate_scrollbars=True,
            state="disabled",
        )
        self._log_textbox.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    # =========================================================================
    # アクションハンドラ
    # =========================================================================

    def _toggle_connection(self):
        """公開開始/停止のトグル処理"""
        if self._is_connected:
            self._stop_tunnel()
        else:
            self._start_tunnel()

    def _start_tunnel(self):
        """トンネルを開始する"""
        port_str = self._port_entry.get().strip()
        if not port_str:
            self._show_error("ポート番号を入力してください")
            return

        try:
            port = int(port_str)
        except ValueError:
            self._show_error("ポート番号は数値で入力してください")
            return

        if not (1 <= port <= 65535):
            self._show_error("ポート番号は 1〜65535 の範囲で入力してください")
            return

        # UI更新
        self._is_connected = True
        self._toggle_btn.configure(
            text="停止する",
            fg_color=COLOR_ACCENT_RED,
            hover_color="#444444",
            text_color=COLOR_TEXT_ON_DARK,
        )
        self._port_entry.configure(state="disabled")
        self._domain_label.configure(text="接続中...")
        self._copy_btn.configure(state="disabled")
        self._append_log(f"ポート {port} でトンネルを開始します...")

        # トンネル開始
        self._tunnel = E4mcTunnel(
            local_port=port,
            on_domain_assigned=self._on_domain_assigned_threadsafe,
            on_status_change=self._on_status_change_threadsafe,
            on_error=self._on_error_threadsafe,
            on_log=self._on_log_threadsafe,
        )
        self._tunnel.start()

    def _stop_tunnel(self):
        """トンネルを停止する"""
        self._append_log("トンネルを停止しています...")

        if self._tunnel:
            self._tunnel.stop()
            self._tunnel = None

        self._is_connected = False
        self._assigned_domain = None
        self._toggle_btn.configure(
            text="公開開始",
            fg_color=COLOR_ACCENT_PRIMARY,
            hover_color=COLOR_ACCENT_PRIMARY_HOVER,
            text_color="#121212",
        )
        self._port_entry.configure(state="normal")
        self._domain_label.configure(text="---", text_color=COLOR_ACCENT_PRIMARY)
        self._copy_btn.configure(state="disabled")
        self._update_status(STATUS_DISCONNECTED, STATUS_COLORS[STATUS_DISCONNECTED])
        self._append_log("トンネルを停止しました")

    def _copy_domain(self):
        """ドメインをクリップボードにコピーする"""
        if self._assigned_domain:
            self.clipboard_clear()
            self.clipboard_append(self._assigned_domain)
            original_text = self._copy_btn.cget("text")
            self._copy_btn.configure(text="✅  コピーしました！")
            self.after(1500, lambda: self._copy_btn.configure(text=original_text))

    # =========================================================================
    # スレッドセーフなコールバック
    # =========================================================================

    def _on_domain_assigned_threadsafe(self, domain: str):
        self.after(0, self._on_domain_assigned, domain)

    def _on_status_change_threadsafe(self, status: str, color: str):
        self.after(0, self._update_status, status, color)

    def _on_error_threadsafe(self, message: str):
        self.after(0, self._show_error, message)

    def _on_log_threadsafe(self, message: str):
        self.after(0, self._append_log, message)

    # =========================================================================
    # GUI更新メソッド
    # =========================================================================

    def _on_domain_assigned(self, domain: str):
        self._assigned_domain = domain
        self._domain_label.configure(text=domain, text_color=COLOR_ACCENT_PRIMARY)
        self._copy_btn.configure(state="normal")

    def _update_status(self, status: str, color: str):
        self._status_label.configure(
            text=f"●  {status}",
            text_color=color,
        )

        if status in (STATUS_ERROR, STATUS_DISCONNECTED) and self._is_connected:
            if status == STATUS_ERROR:
                self._is_connected = False
                self._toggle_btn.configure(
                    text="公開開始",
                    fg_color=COLOR_ACCENT_PRIMARY,
                    hover_color=COLOR_ACCENT_PRIMARY_HOVER,
                    text_color="#121212",
                )
                self._port_entry.configure(state="normal")
                self._copy_btn.configure(state="disabled")
                self._domain_label.configure(text="---")
                if self._tunnel:
                    threading.Thread(
                        target=self._tunnel.stop, daemon=True
                    ).start()
                    self._tunnel = None

    def _show_error(self, message: str):
        self._append_log(f"❌ エラー: {message}")

    def _append_log(self, message: str):
        self._log_textbox.configure(state="normal")
        self._log_textbox.insert("end", f"{message}\n")
        self._log_textbox.see("end")
        self._log_textbox.configure(state="disabled")

    # =========================================================================
    # クリーンアップ
    # =========================================================================

    def _on_closing(self):
        self._force_cleanup()
        self.destroy()

    def _force_cleanup(self):
        if self._tunnel:
            try:
                self._tunnel.stop()
            except Exception:
                pass
            self._tunnel = None


# =============================================================================
# エントリーポイント
# =============================================================================

if __name__ == "__main__":
    app = App()
    app.mainloop()
