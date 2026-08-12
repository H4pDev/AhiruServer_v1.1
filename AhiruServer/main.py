"""
AhiruServer - Minecraft LAN共有 Webアプリケーション
ポート開放なしでMinecraftのLAN公開ワールドを外部共有するためのe4mc互換クライアント。

使い方:
  1. pip install -r requirements.txt
  2. python main.py
  3. 自動的にブラウザで http://127.0.0.1:8000 が開きます
  4. MinecraftでLANに公開 → 表示されたポート番号を入力 → 「起動」を押す
  5. 表示されたアドレスを友人に共有する

プロトコル:
  e4mc (Dialtone/QUIC) プロトコルを使用。
  1. ブローカーAPI (https://broker.e4mc.link/getBestRelay) で最適リレーを取得
  2. QUICでリレーサーバーに接続 (ALPN: "quiclime")
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
import webbrowser
from typing import Optional, Callable, List, Dict, Any

# Windows標準出力での絵文字エラー防止
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

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
STATUS_DISCONNECTED = "停止中"
STATUS_CONNECTING = "接続中..."
STATUS_ONLINE = "公開中（オンライン）"
STATUS_ERROR = "エラー"

# ステータスに対応する色 (状況に応じて直感的なカラーを表示)
STATUS_COLORS = {
    STATUS_DISCONNECTED: "#888888",  # グレー (停止中)
    STATUS_CONNECTING: "#F39C12",    # アンバー/オレンジ (接続中)
    STATUS_ONLINE: "#2ECC71",        # エメラルドグリーン (公開中/オンライン)
    STATUS_ERROR: "#E74C3C",         # レッド (エラー)
}

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
        # 自己署名証明書を許可
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
# WebAppState & WebSocket Manager
# =============================================================================

class WebAppManager:
    """
    Webアプリケーション用の状態管理およびWebSocketブロードキャスター。
    """
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.tunnel: Optional[E4mcTunnel] = None
        self.is_connected = False
        self.status = STATUS_DISCONNECTED
        self.status_color = STATUS_COLORS[STATUS_DISCONNECTED]
        self.domain: Optional[str] = None
        self.current_port: Optional[int] = None
        self.logs: List[str] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        # 初期状態の送信
        await websocket.send_json({
            "type": "init",
            "payload": {
                "is_connected": self.is_connected,
                "status": self.status,
                "color": self.status_color,
                "domain": self.domain,
                "port": self.current_port,
                "logs": self.logs
            }
        })

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)

    def _safe_broadcast(self, message: dict):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)
        except Exception as e:
            logger.debug(f"ブロードキャスト例外: {e}")

    # コールバック群
    def on_status_change(self, status: str, color: str):
        self.status = status
        self.status_color = color
        if status in (STATUS_ONLINE, STATUS_CONNECTING):
            self.is_connected = True
        elif status in (STATUS_DISCONNECTED, STATUS_ERROR):
            self.is_connected = False
            self.current_port = None
            if status == STATUS_DISCONNECTED:
                self.domain = None
        self._safe_broadcast({
            "type": "status_change",
            "payload": {"status": status, "color": color}
        })

    def on_domain_assigned(self, domain: str):
        self.domain = domain
        self._safe_broadcast({
            "type": "domain_assigned",
            "payload": {"domain": domain}
        })

    def on_error(self, message: str):
        self.append_log(f"❌ エラー: {message}")

    def on_log(self, message: str):
        self.append_log(message)

    def append_log(self, message: str):
        self.logs.append(message)
        # ログ保持上限 (最新500件)
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]
        self._safe_broadcast({
            "type": "log",
            "payload": {"message": message}
        })

    def start_tunnel(self, port: int):
        if self.is_connected and self.tunnel:
            return False, "すでにトンネルが起動しています"

        self.current_port = port
        self.is_connected = True
        self.append_log(f"ポート {port} でトンネルを開始します...")

        self.tunnel = E4mcTunnel(
            local_port=port,
            on_domain_assigned=self.on_domain_assigned,
            on_status_change=self.on_status_change,
            on_error=self.on_error,
            on_log=self.on_log,
        )
        self.tunnel.start()
        return True, "トンネルを開始しました"

    def stop_tunnel(self):
        self.append_log("トンネルを停止しています...")
        if self.tunnel:
            def do_stop():
                if self.tunnel:
                    self.tunnel.stop()
                    self.tunnel = None
                self.is_connected = False
                self.current_port = None
                self.domain = None
                self.on_status_change(STATUS_DISCONNECTED, STATUS_COLORS[STATUS_DISCONNECTED])
                self.append_log("トンネルを停止しました")
            
            threading.Thread(target=do_stop, daemon=True).start()
        else:
            self.is_connected = False
            self.current_port = None
            self.domain = None
            self.on_status_change(STATUS_DISCONNECTED, STATUS_COLORS[STATUS_DISCONNECTED])
            self.append_log("トンネルを停止しました")
        return True, "トンネル停止処理を開始しました"


# FastAPI アプリケーションの作成
app = FastAPI(title="AhiruServer Web")
manager = WebAppManager()

# 静的ファイルの配信
static_dir = resource_path("static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def get_index():
    index_path = resource_path(os.path.join("static", "index.html"))
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"error": "index.htmlが見つかりません"}, status_code=404)

class StartRequest(BaseModel):
    port: int

@app.post("/api/start")
async def api_start(req: StartRequest):
    if not (1 <= req.port <= 65535):
        return {"success": False, "message": "ポート番号は 1〜65535 の範囲で指定してください"}
    success, msg = manager.start_tunnel(req.port)
    return {"success": success, "message": msg}

@app.post("/api/stop")
async def api_stop():
    success, msg = manager.stop_tunnel()
    return {"success": success, "message": msg}

@app.get("/api/status")
async def api_status():
    return {
        "is_connected": manager.is_connected,
        "status": manager.status,
        "color": manager.status_color,
        "domain": manager.domain,
        "port": manager.current_port,
        "logs": manager.logs
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # WebSocket経由の受信は心拍確認等に使用
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


def open_browser():
    """ サーバー起動後にブラウザを自動オープンする """
    webbrowser.open("http://127.0.0.1:8000")


def cleanup_on_exit():
    """ 終了時のクリーンアップ """
    if manager.tunnel:
        try:
            manager.tunnel.stop()
        except Exception:
            pass


atexit.register(cleanup_on_exit)

if __name__ == "__main__":
    print("=" * 60)
    print("AhiruServer Web Application")
    print("Webサーバーを起動しています: http://127.0.0.1:8000")
    print("=" * 60)
    
    # 1.5秒後にブラウザを自動起動
    threading.Timer(1.5, open_browser).start()
    
    # Uvicornサーバーの起動
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
