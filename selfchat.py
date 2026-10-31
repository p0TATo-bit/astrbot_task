# astrbot_task 自建手机聊天(selfchat): 注册进 AstrBot 的平台适配器 + 内置 HTTP 服务 + 手机 PWA 页。
# 手机浏览器(安卓澎湃/iOS)打开 http://服务器IP:端口 → 填一次访问令牌(存本地) → 和她聊天。
# 走 AstrBot 完整管线: 人格/情绪/记忆/工具全在; 手机聊天记录自动进插件对话表参与周期总结;
# 定时提醒/主动关心推送经 owner_umo(自动跟随最近会话)直达手机; 关屏时的消息留在历史里, 打开即见。
# 历史独立存 selfchat_msg 表(含推送), 不与 chats 表(总结用)混写。无第三方依赖。

import asyncio
import http.server
import json
import secrets
import sqlite3
import threading
import time
import urllib.parse

from astrbot.api import logger
from astrbot.api.platform import register_platform_adapter
from astrbot.core.message.components import Plain
from astrbot.core.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)

DB_PATH = ""  # main.py 导入后注入(插件主库, selfchat_msg 表随数据目录走, 更新免疫)
SESSION_ID = "phone"  # 单主人设计: 固定一个会话; UMO = selfchat:FriendMessage:phone
UMO = f"selfchat:{MessageType.FRIEND_MESSAGE.value}:{SESSION_ID}"

_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<meta name="theme-color" content="#5b8c8f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>Teaskks</title>
<style>
:root { --primary:#5b8c8f; --bg:#f4f2ee; --card:#ffffff; --text:#2c3e50; --sub:#8a9ba0; --border:#e5e2db; --mine:#dce9e7; }
* { box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
html,body { height:100%; }
body { font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif; background:var(--bg); color:var(--text);
  display:flex; flex-direction:column; height:100dvh; }
header { padding:calc(10px + env(safe-area-inset-top)) 16px 10px; background:var(--card); border-bottom:1px solid var(--border);
  font-size:15px; font-weight:700; color:var(--primary); display:flex; align-items:center; gap:8px; }
header .dot { width:8px; height:8px; border-radius:50%; background:#9fc0a8; }
header .dot.off { background:#c8c4ba; }
header small { font-weight:400; color:var(--sub); margin-left:auto; font-size:11px; }
#chat { flex:1; overflow-y:auto; padding:14px 12px 18px; -webkit-overflow-scrolling:touch; }
.row { display:flex; margin:6px 0; align-items:flex-end; }
.row.me { justify-content:flex-end; }
.bubble { max-width:78%; padding:9px 12px; border-radius:14px; font-size:15px; line-height:1.55; white-space:pre-wrap;
  word-break:break-word; box-shadow:0 1px 2px rgba(60,80,80,.06); }
.row.ai .bubble { background:var(--card); border-bottom-left-radius:4px; }
.row.me .bubble { background:var(--mine); border-bottom-right-radius:4px; }
.meta { font-size:10.5px; color:var(--sub); margin:0 8px 2px; }
footer { display:flex; gap:8px; padding:8px 10px calc(10px + env(safe-area-inset-bottom)); background:var(--card);
  border-top:1px solid var(--border); }
#inp { flex:1; border:1px solid var(--border); border-radius:18px; padding:10px 14px; font-size:15px; background:var(--bg);
  color:var(--text); outline:none; resize:none; max-height:110px; line-height:1.4; font-family:inherit; }
#send { border:none; border-radius:18px; padding:0 18px; background:var(--primary); color:#fff; font-size:15px; }
#send:disabled { opacity:.5; }
#gate { position:fixed; inset:0; background:var(--bg); display:flex; flex-direction:column; align-items:center;
  justify-content:center; gap:14px; z-index:9; }
#gate h1 { font-size:18px; color:var(--primary); letter-spacing:2px; }
#gate input { width:min(280px,72vw); border:1px solid var(--border); border-radius:10px; padding:12px 14px; font-size:15px;
  text-align:center; background:var(--card); color:var(--text); outline:none; }
#gate button { width:min(280px,72vw); border:none; border-radius:10px; padding:12px; background:var(--primary);
  color:#fff; font-size:15px; }
#gate p { font-size:12px; color:var(--sub); }
.err { color:#c05050; font-size:12px; min-height:16px; }
</style>
</head>
<body>
<div id="gate">
  <h1>Teaskks</h1>
  <input id="tk" placeholder="访问令牌" autocomplete="off">
  <div class="err" id="gerr"></div>
  <button onclick="enter()">进入</button>
  <p>令牌在 AstrBot 面板·平台适配器·selfchat 配置里</p>
</div>
<header><span class="dot" id="dot"></span>Teaskks<small id="stat">连接中</small></header>
<div id="chat"></div>
<footer>
  <textarea id="inp" rows="1" placeholder="说点什么…"></textarea>
  <button id="send" onclick="send()">发送</button>
</footer>
<script>
let TK = localStorage.getItem('sc_token') || '';
let afterId = 0, pollTimer = null, box = document.getElementById('chat');
const seen = new Set();
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmt(ts) { try { return ts.slice(5, 16); } catch (e) { return ''; } }
function render(m) {
  if (seen.has(m.id)) return;
  seen.add(m.id);
  const row = document.createElement('div');
  row.className = 'row ' + (m.role === 'user' ? 'me' : 'ai');
  row.innerHTML = '<div>' + (m.role === 'user' ? '' : '<div class="meta">' + esc(fmt(m.created_at)) + '</div>') +
    '<div class="bubble">' + esc(m.text) + '</div>' +
    (m.role === 'user' ? '<div class="meta">' + esc(fmt(m.created_at)) + '</div>' : '') + '</div>';
  box.appendChild(row);
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 160) box.scrollTop = box.scrollHeight;
}
async function api(path, opt) {
  const r = await fetch(path + (path.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TK), opt);
  if (r.status === 401) { localStorage.removeItem('sc_token'); location.reload(); throw new Error('auth'); }
  if (!r.ok) throw new Error('http ' + r.status);
  return r.json();
}
async function loadHistory() {
  const d = await api('/api/history?limit=60');
  (d.msgs || []).forEach(render);
  box.scrollTop = box.scrollHeight;
}
async function poll() {
  try {
    document.getElementById('dot').className = 'dot';
    document.getElementById('stat').textContent = '';
    const d = await api('/api/poll?after=' + afterId);
    (d.msgs || []).forEach(render);
    if ((d.msgs || []).length) box.scrollTop = box.scrollHeight;
  } catch (e) {
    document.getElementById('dot').className = 'dot off';
    document.getElementById('stat').textContent = '重连中';
    await new Promise(r => setTimeout(r, 3000));
  }
  pollTimer = setTimeout(poll, 100);
}
async function send() {
  const el = document.getElementById('inp'), text = el.value.trim();
  if (!text) return;
  document.getElementById('send').disabled = true;
  try {
    await api('/api/send', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    el.value = ''; el.style.height = 'auto';
    const d = await api('/api/history?limit=1');
    (d.msgs || []).forEach(render);
  } catch (e) {}
  document.getElementById('send').disabled = false;
  el.focus();
}
document.getElementById('inp').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
function enter() {
  const v = document.getElementById('tk').value.trim();
  if (!v) return;
  fetch('/api/history?limit=1&t=' + encodeURIComponent(v)).then(r => {
    if (!r.ok) throw 0;
    TK = v; localStorage.setItem('sc_token', v);
    document.getElementById('gate').style.display = 'none';
    start();
  }).catch(() => { document.getElementById('gerr').textContent = '令牌不对, 再试试'; });
}
async function start() {
  await loadHistory();
  poll();
}
if (TK) {
  document.getElementById('gate').style.display = 'none';
  start();
}
</script>
</body>
</html>"""


class ChatStore:
    """selfchat 历史存取(独立表, 线程内每请求短连接)"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        if not self.db_path:
            return
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS selfchat_msg (
                id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, text TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')))""")

    def add(self, role: str, text: str) -> int:
        if not self.db_path:
            return 0
        with self._conn() as c:
            cur = c.execute("INSERT INTO selfchat_msg (role, text) VALUES (?,?)", (role, text[:4000]))
            return int(cur.lastrowid or 0)

    def after(self, after_id: int, limit: int = 50) -> list[dict]:
        if not self.db_path:
            return []
        with self._conn() as c:
            rows = c.execute("SELECT id, role, text, created_at FROM selfchat_msg WHERE id > ? ORDER BY id LIMIT ?",
                             (after_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def latest(self, limit: int = 60) -> list[dict]:
        if not self.db_path:
            return []
        with self._conn() as c:
            rows = c.execute("SELECT id, role, text, created_at FROM selfchat_msg ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
            return [dict(r) for r in reversed(rows)]


@register_platform_adapter(
    "selfchat", "astrbot_task 手机聊天(浏览器 PWA)",
    adapter_display_name="手机聊天",
    support_streaming_message=False,
    default_config_tmpl={
        "id": "selfchat",
        "type": "selfchat",
        "enable": False,
        "host": "0.0.0.0",
        "port": 6188,
        "token": "",
    },
)
class SelfChatAdapter(Platform):
    """手机聊天平台: HTTP 收发, 走 AstrBot 完整管线"""

    def __init__(self, platform_config: dict, platform_settings: dict, event_queue) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.host = str(platform_config.get("host") or "0.0.0.0")
        self.port = int(platform_config.get("port") or 6188)
        self.token = str(platform_config.get("token") or "").strip()
        if not self.token:
            self.token = secrets.token_hex(4)
            logger.warning("[selfchat] 未设置访问令牌, 本次随机生成: %s (建议在平台配置里固定一个)", self.token)
        self.store = ChatStore(DB_PATH)
        self._http = None
        self._http_thread = None
        self._loop = None
        self.metadata = PlatformMetadata(
            name="selfchat", description="astrbot_task 手机聊天", id=str(platform_config.get("id") or "selfchat"),
            support_proactive_message=True,
        )

    # ---- Platform 协议 ----

    def meta(self) -> PlatformMetadata:
        return self.metadata

    def run(self):
        return self._run()

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        try:
            self._http = _make_server(self)
            self._http_thread = threading.Thread(
                target=self._http.serve_forever, kwargs={"poll_interval": 0.5},
                daemon=True, name="selfchat-http")
            self._http_thread.start()
            logger.info("[selfchat] 手机聊天已启动: http://%s:%s (令牌 %s)",
                        self.host if self.host != "0.0.0.0" else "服务器IP", self.port, self.token)
        except Exception as e:
            logger.error("[selfchat] HTTP 服务启动失败(端口被占?): %s", e)
            self.record_error(f"HTTP 启动失败: {e}")
            return
        try:
            await asyncio.Event().wait()  # 常驻; terminate 里 shutdown
        except asyncio.CancelledError:
            raise
        finally:
            try:
                self._http.shutdown()
            except Exception:
                pass

    async def terminate(self) -> None:
        try:
            if self._http:
                self._http.shutdown()
        except Exception:
            pass

    async def send_by_session(self, session, message_chain) -> None:
        """管线的回复/推送出口: 记历史(推送不经过 on_llm_response, 必须在这里落库)"""
        try:
            text = "".join(getattr(c, "text", "") or "" for c in (getattr(message_chain, "chain", None) or []))
            if text.strip():
                self.store.add("ai", text.strip())
        except Exception as e:
            logger.error("[selfchat] 回复记录失败: %s", e)

    # ---- 入站 ----

    def inbound(self, text: str):
        """HTTP 线程调用: 落历史 + 投递进管线"""
        text = (text or "").strip()
        if not text:
            return
        self.store.add("user", text)
        abm = AstrBotMessage()
        abm.self_id = "selfchat"
        abm.sender = MessageMember("主人", SESSION_ID)
        abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = SESSION_ID
        abm.message_id = f"sc{int(time.time() * 1000)}"
        abm.message = [Plain(text)]
        abm.message_str = text
        abm.raw_message = {"text": text}
        abm.timestamp = int(time.time())
        event = self.create_event(abm)
        self._loop.call_soon_threadsafe(self.commit_event, event)


def _make_server(adapter: SelfChatAdapter) -> http.server.ThreadingHTTPServer:
    store = adapter.store
    token = adapter.token

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默访问日志
            pass

        def _auth(self) -> bool:
            q = urllib.parse.urlparse(self.path).query
            qs = urllib.parse.parse_qs(q)
            return (qs.get("t", [""])[0] == token
                    or self.headers.get("X-Token", "") == token)

        def _json(self, data, code=200):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._auth():
                return self._json({"error": "unauthorized"}, 401)
            qs = urllib.parse.parse_qs(u.query)
            if u.path == "/api/history":
                limit = min(200, max(1, int(qs.get("limit", ["60"])[0] or 60)))
                return self._json({"msgs": store.latest(limit)})
            if u.path == "/api/poll":
                after = int(qs.get("after", ["0"])[0] or 0)
                deadline = time.time() + 20
                msgs = store.after(after)
                while not msgs and time.time() < deadline:
                    time.sleep(0.4)
                    msgs = store.after(after)
                return self._json({"msgs": msgs})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/api/send":
                return self._json({"error": "not found"}, 404)
            if not self._auth():
                return self._json({"error": "unauthorized"}, 401)
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
                data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                text = str(data.get("text") or "").strip()
                if not text:
                    return self._json({"error": "text 必填"}, 400)
                adapter.inbound(text)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

    return http.server.ThreadingHTTPServer((adapter.host, adapter.port), Handler)
