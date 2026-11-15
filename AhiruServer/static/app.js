document.addEventListener('DOMContentLoaded', () => {
  const portInput = document.getElementById('port-input');
  const toggleBtn = document.getElementById('toggle-btn');
  const domainDisplay = document.getElementById('domain-display');
  const copyBtn = document.getElementById('copy-btn');
  const statusIndicator = document.getElementById('status-indicator');
  const statusDot = document.getElementById('status-dot');
  const statusText = document.getElementById('status-text');
  const logTerminal = document.getElementById('log-terminal');
  const backendToggleBtn = document.getElementById('backend-toggle-btn');
  const backendPanel = document.getElementById('backend-panel');
  const backendConnBadge = document.getElementById('backend-conn-badge');
  const apiBaseInput = document.getElementById('api-base-input');
  const apiSaveBtn = document.getElementById('api-save-btn');
  const apiResetBtn = document.getElementById('api-reset-btn');

  let isConnected = false;
  let assignedDomain = null;
  let ws = null;
  let pingInterval = null;
  let reconnectTimer = null;
  let reconnectDelay = 1000;
  let wsFailCount = 0;

  // 初期読み込み時は必ずポート番号入力欄を空欄にする
  portInput.value = '';
  setTimeout(() => {
    if (!isConnected) portInput.value = '';
  }, 100);

  // ステータスと色のマッピング
  const STATUS_COLORS = {
    "停止中": "#888888",
    "接続中...": "#F39C12",
    "公開中（オンライン）": "#2ECC71",
    "エラー": "#E74C3C"
  };

  // APIおよびWebSocketのベースURL解決
  // Cloudflare Pagesや外部から静的ファイルのみホストされるケースに対応
  const urlParams = new URLSearchParams(window.location.search);
  const queryApi = urlParams.get('api');
  if (queryApi) {
    localStorage.setItem('ahiru_api_base', queryApi.trim().replace(/\/+$/, ''));
  }

  const isStaticHost = window.location.hostname.endsWith('.pages.dev') || window.location.protocol === 'file:';
  let apiBase = (localStorage.getItem('ahiru_api_base') || '').replace(/\/+$/, '');
  if (!apiBase && isStaticHost) {
    apiBase = 'http://127.0.0.1:8000';
    backendPanel.classList.remove('hidden');
  }
  apiBaseInput.value = apiBase;

  function setBackendBadge(state, text) {
    backendConnBadge.textContent = text;
    backendConnBadge.classList.remove('connected', 'error');
    if (state === 'connected') backendConnBadge.classList.add('connected');
    if (state === 'error') backendConnBadge.classList.add('error');
  }

  backendToggleBtn.addEventListener('click', () => {
    backendPanel.classList.toggle('hidden');
  });

  apiSaveBtn.addEventListener('click', () => {
    let val = apiBaseInput.value.trim().replace(/\/+$/, '');
    if (val && !/^https?:\/\//i.test(val)) {
      val = `http://${val}`;
    }
    apiBase = val;
    apiBaseInput.value = apiBase;
    if (apiBase) {
      localStorage.setItem('ahiru_api_base', apiBase);
    } else {
      localStorage.removeItem('ahiru_api_base');
    }
    reconnectToBackend();
  });

  apiResetBtn.addEventListener('click', () => {
    localStorage.removeItem('ahiru_api_base');
    apiBase = '';
    apiBaseInput.value = '';
    reconnectToBackend();
  });

  function reconnectToBackend() {
    wsFailCount = 0;
    reconnectDelay = 1000;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (pingInterval) {
      clearInterval(pingInterval);
      pingInterval = null;
    }
    if (ws) {
      ws.onclose = null;
      ws.onerror = null;
      ws.close();
      ws = null;
    }
    setBackendBadge('idle', '接続中...');
    initWebSocket();
  }

  function getWsUrl() {
    if (apiBase) {
      const wsProto = apiBase.startsWith('https:') ? 'wss:' : 'ws:';
      const hostPart = apiBase.replace(/^https?:\/\//, '');
      return `${wsProto}//${hostPart}/ws`;
    }
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws`;
  }

  // WebSocket接続の初期化
  function initWebSocket() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
      return;
    }

    const wsUrl = getWsUrl();
    console.log(`[AhiruServer] WebSocket接続試行: ${wsUrl}`);

    try {
      ws = new WebSocket(wsUrl);
    } catch (err) {
      console.error('WebSocket作成失敗:', err);
      setBackendBadge('error', 'URL形式エラー');
      backendPanel.classList.remove('hidden');
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      console.log('WebSocket 接続成功');
      wsFailCount = 0;
      reconnectDelay = 1000;
      setBackendBadge('connected', '接続済み');
      // Cloudflareの100秒アイドル切断を防ぐための25秒ハートビートPing
      if (pingInterval) clearInterval(pingInterval);
      pingInterval = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping', payload: { timestamp: Date.now() } }));
        }
      }, 25000);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'pong') {
          // ハートビート応答
          return;
        }
        handleWsMessage(data);
      } catch (e) {
        console.error('メッセージ解析エラー:', e);
      }
    };

    ws.onclose = () => {
      if (pingInterval) clearInterval(pingInterval);
      wsFailCount += 1;
      setBackendBadge('error', '未接続');
      // 外部ホスティング等で接続できない場合は設定パネルを自動表示
      if (wsFailCount >= 2) {
        backendPanel.classList.remove('hidden');
      }
      console.warn(`WebSocket 切断。${reconnectDelay / 1000}秒後に再接続を試みます...`);
      scheduleReconnect();
    };

    ws.onerror = (err) => {
      console.error('WebSocket エラー:', err);
      setBackendBadge('error', '接続失敗');
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      initWebSocket();
      reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
    }, reconnectDelay);
  }

  // 受信メッセージの処理
  function handleWsMessage(data) {
    const { type, payload } = data;

    if (type === 'init') {
      // 初期状態の設定
      isConnected = payload.is_connected;
      assignedDomain = payload.domain;
      updateStatus(payload.status, payload.color);
      updateUiState(isConnected);
      if (isConnected && payload.port) {
        portInput.value = payload.port;
      } else {
        portInput.value = '';
      }
      if (payload.domain) domainDisplay.textContent = payload.domain;
      if (payload.logs && Array.isArray(payload.logs)) {
        logTerminal.textContent = '';
        payload.logs.forEach(msg => appendLog(msg));
      }
    } else if (type === 'status_change') {
      updateStatus(payload.status, payload.color);
      if (payload.status === "停止中" || payload.status === "エラー") {
        isConnected = false;
        updateUiState(false);
      } else if (payload.status === "公開中（オンライン）") {
        isConnected = true;
        updateUiState(true);
      }
    } else if (type === 'domain_assigned') {
      assignedDomain = payload.domain;
      domainDisplay.textContent = assignedDomain;
      copyBtn.disabled = false;
    } else if (type === 'log') {
      appendLog(payload.message);
    }
  }

  // 画面表示更新
  function updateStatus(status, color) {
    statusText.textContent = status;
    const finalColor = color || STATUS_COLORS[status] || "#888888";
    statusDot.style.color = finalColor;
    statusText.style.color = finalColor;
    statusIndicator.style.color = finalColor;
  }

  function updateUiState(connected) {
    isConnected = connected;
    if (isConnected) {
      toggleBtn.textContent = '停止する';
      toggleBtn.classList.add('active');
      toggleBtn.disabled = false;
      portInput.disabled = true;
      if (assignedDomain) {
        copyBtn.disabled = false;
      }
    } else {
      toggleBtn.textContent = '起動';
      toggleBtn.classList.remove('active');
      toggleBtn.disabled = false;
      portInput.disabled = false;
      portInput.value = '';
      domainDisplay.textContent = '---';
      assignedDomain = null;
      copyBtn.disabled = true;
    }
  }

  function appendLog(message) {
    logTerminal.textContent += `${message}\n`;
    logTerminal.scrollTop = logTerminal.scrollHeight;
  }

  // 起動/停止トグル処理
  toggleBtn.addEventListener('click', async () => {
    if (isConnected) {
      // 停止処理
      toggleBtn.disabled = true;
      toggleBtn.textContent = '停止中...';
      try {
        const resp = await fetch(`${apiBase}/api/stop`, { method: 'POST' });
        const res = await resp.json();
        if (!res.success) {
          alert(`エラー: ${res.message}`);
          toggleBtn.disabled = false;
        }
      } catch (e) {
        console.error('停止リクエストエラー:', e);
        alert(`停止リクエストに失敗しました。\n詳細: ${e.message}\n(サーバーが起動していないか、セキュリティ制限により通信がブロックされた可能性があります)`);
        toggleBtn.disabled = false;
      }
    } else {
      // 起動処理
      const portVal = portInput.value.trim();
      if (!portVal) {
        alert('ポート番号を入力してください');
        return;
      }
      const port = parseInt(portVal, 10);
      if (isNaN(port) || port < 1 || port > 65535) {
        alert('ポート番号は 1〜65535 の範囲で入力してください');
        return;
      }

      toggleBtn.disabled = true;
      toggleBtn.textContent = '起動中...';

      try {
        const resp = await fetch(`${apiBase}/api/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ port })
        });
        const res = await resp.json();
        if (!res.success) {
          alert(`エラー: ${res.message}`);
          toggleBtn.disabled = false;
          toggleBtn.textContent = '起動';
        } else {
          isConnected = true;
          updateUiState(true);
        }
      } catch (e) {
        console.error('起動リクエストエラー:', e);
        alert(`起動リクエストに失敗しました。\n詳細: ${e.message}\n(サーバーが起動していないか、ブラウザのセキュリティ制限(Mixed Content / PNA)により遮断された可能性があります)`);
        toggleBtn.disabled = false;
        toggleBtn.textContent = '起動';
      }
    }
  });

  // クリップボードコピー処理
  copyBtn.addEventListener('click', async () => {
    if (!assignedDomain) return;
    try {
      await navigator.clipboard.writeText(assignedDomain);
      const originalText = copyBtn.textContent;
      copyBtn.textContent = '✅ コピーしました！';
      setTimeout(() => {
        copyBtn.textContent = originalText;
      }, 1500);
    } catch (e) {
      console.error('コピー失敗:', e);
      // フォールバック
      const tempInput = document.createElement('input');
      tempInput.value = assignedDomain;
      document.body.appendChild(tempInput);
      tempInput.select();
      document.execCommand('copy');
      document.body.removeChild(tempInput);

      const originalText = copyBtn.textContent;
      copyBtn.textContent = '✅ コピーしました！';
      setTimeout(() => {
        copyBtn.textContent = originalText;
      }, 1500);
    }
  });

  // 初期化実行
  initWebSocket();
});
