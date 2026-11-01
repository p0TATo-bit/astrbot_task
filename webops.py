# astrbot_task 网页操作(webops): 无头浏览器代理 — 她可以打开网页、点击、填写、提交、读结果。
# 可选能力: 依赖 playwright(容器里 pip install playwright && playwright install chromium)。
# 未安装时 browser_task 返回启用指引, 插件其他功能完全不受影响。
# 线程模型: playwright 的 sync API 对象绑定创建线程 — 所有浏览器操作统一投递到一个
# 常驻专用线程执行(_BrowserThread), LLM 决策留在事件循环, 两边只传数据不传对象。
# browser_task 工具内部跑"看页面→决策→执行"循环(LLM 决策由注入的 llm_call 提供,
# 不占 AstrBot 对话工具轮次); 单例浏览器常驻复用, 单主人串行使用。

import json
import os
import queue
import re
import threading

from astrbot.api import logger

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except Exception:
    sync_playwright = None
    PLAYWRIGHT_OK = False

STATE_FILE = ""  # main.py 注入: 插件数据目录/browser_state.json(Cookies 持久化, 登录一次长期免登)
DATA_DIR = ""    # main.py 注入: 插件数据目录(二维码截图等临时文件)

ENABLE_HINT = ("(网页操作未启用: 需要无头浏览器。请在 AstrBot 容器里执行两行命令后重启容器:\n"
               "pip install playwright\n"
               "playwright install chromium\n"
               "约 400MB, 只需装一次)")

_MAX_STEPS = 25
_SNAP_JS = """(() => {
  const els = [...document.querySelectorAll('button, a, input, select, textarea, [role="button"], [onclick]')];
  const out = [];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const label = ((el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText
      || el.value || el.getAttribute('aria-label') || el.placeholder || '');
    el.setAttribute('data-widx', String(out.length));
    out.push({tag: el.tagName.toLowerCase(), type: el.type || '',
      text: String(label).trim().replace(/\\s+/g, ' ').slice(0, 40),
      value: String(el.value || '').slice(0, 30), checked: !!el.checked});
    if (out.length >= 80) break;
  }
  return {title: document.title, url: location.href,
    text: String(document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n').slice(0, 1500),
    els: out};
})()"""


class _BrowserThread:
    """常驻浏览器专用线程: playwright sync 对象只能在创建线程上用, 所有操作排队到这里执行"""

    def __init__(self):
        self._q = queue.Queue()
        self._st = {"pw": None, "browser": None, "page": None}
        self._lock = threading.Lock()  # 串行化: 单主人一次一个任务
        threading.Thread(target=self._worker, daemon=True, name="webops-browser").start()

    def _worker(self):
        while True:
            fn, fut = self._q.get()
            if fn is None:
                try:
                    if self._st["browser"]:
                        self._st["browser"].close()
                    if self._st["pw"]:
                        self._st["pw"].stop()
                except Exception:
                    pass
                fut["done"].set()
                return
            try:
                fut["r"] = fn()
            except Exception as e:
                fut["e"] = e
            fut["done"].set()

    def run(self, fn, timeout=90):
        """在浏览器线程上同步执行 fn; 异步侧经 asyncio.to_thread 调用, 不阻塞事件循环"""
        with self._lock:
            fut = {"done": threading.Event(), "r": None, "e": None}
            self._q.put((fn, fut))
            if not fut["done"].wait(timeout):
                raise TimeoutError("浏览器操作超时")
            if fut["e"]:
                raise fut["e"]
            return fut["r"]

    def stop(self):
        try:
            self._q.put((None, {"done": threading.Event()}))
        except Exception:
            pass


_BT = None  # 惰性创建: 只在真正用到时起线程


def _bt():
    global _BT
    if _BT is None:
        _BT = _BrowserThread()
    return _BT


def _ensure_page(st) -> object:
    if st["page"] is None:
        if st["pw"] is None:
            st["pw"] = sync_playwright().start()
        try:
            st["browser"] = st["pw"].chromium.launch(headless=True, args=["--no-sandbox"])
        except Exception as e:
            st["pw"].stop()
            st["pw"] = None
            raise RuntimeError(f"浏览器启动失败(容器里缺 chromium? 执行 playwright install chromium): {e}")
        ctx = st["browser"].new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            **({"storage_state": STATE_FILE} if STATE_FILE and os.path.exists(STATE_FILE) else {}))
        st["context"] = ctx
        st["page"] = ctx.new_page()
    return st["page"]


def _save_state_locked(st):
    """把当前登录态(Cookies/localStorage)存盘, 下次启动自动带上"""
    ctx = st.get("context")
    if ctx and STATE_FILE:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            ctx.storage_state(path=STATE_FILE)
            return True
        except Exception as e:
            logger.warning("[webops] 登录态保存失败: %s", e)
    return False


def _op_save_state() -> bool:
    bt = _bt()
    return bt.run(lambda: _save_state_locked(bt._st))


# ---- 扫码登录 ----

_QR_FIND_JS = """(() => {
  let best = null;
  for (const el of document.querySelectorAll('img, canvas')) {
    const r = el.getBoundingClientRect();
    const s = Math.min(r.width, r.height);
    if (s < 110) continue;
    const hint = String(el.className + ' ' + el.id + ' ' + (el.src || '')).toLowerCase();
    const score = (/qr|qrcode|code/.test(hint) ? 1000 : 0) + s;
    if (!best || score > best.score) best = {el, score};
  }
  if (best) { best.el.setAttribute('data-qr', '1'); return true; }
  return false;
})()"""


def _op_find_qr() -> str | None:
    """找页面上最大的二维码元素, 截图存盘并返回路径; 没有返回 None"""
    bt = _bt()
    def job():
        page = _ensure_page(bt._st)
        if not page.evaluate(_QR_FIND_JS):
            return None
        path = os.path.join(DATA_DIR or ".", "qr_latest.png")
        try:
            page.locator('[data-qr="1"]').first.screenshot(path=path, timeout=8000)
        finally:
            page.evaluate("() => { const e = document.querySelector('[data-qr]'); if (e) e.removeAttribute('data-qr'); }")
        return path if os.path.exists(path) else None
    try:
        return bt.run(job)
    except Exception as e:
        logger.warning("[webops] 二维码截图失败: %s", e)
        return None


def login_judge_prompt(snap_fmt: str) -> str:
    return ("用户正在网页上扫码登录。下面是当前页面状态, 判断登录是否已经成功:\n" + snap_fmt +
            "\n\n只输出一个 JSON 对象: {\"login\": true 或 false, \"note\": \"一句中文说明\"}\n"
            "已登录的标志: 页面出现用户昵称/头像/个人中心/退出按钮, 或已离开登录页; 出现二维码或登录表单=未登录。")


async def run_login(url: str, llm_call, notify_qr, wait_s: int = 150):
    """扫码登录流程: 打开登录页 → 二维码截图发给主人 → 轮询判定登录成功 → 存登录态。
    notify_qr: async (图片路径) -> None, 由宿主发到主人会话。"""
    import asyncio
    import time as _time
    try:
        snap = await asyncio.to_thread(_op_goto, url)
    except Exception as e:
        return f"(打开登录页失败: {e})"
    sent = False
    deadline = _time.time() + wait_s
    while _time.time() < deadline:
        qr_path = await asyncio.to_thread(_op_find_qr)
        if qr_path and not sent:
            try:
                await notify_qr(qr_path)
                sent = True
                logger.info("[webops] 二维码已发送给主人, 等待扫码")
            except Exception as e:
                logger.warning("[webops] 二维码发送失败: %s", e)
        try:
            raw = await llm_call(login_judge_prompt(_fmt_snapshot(snap)),
                                 system="你是登录状态判定器, 只输出 JSON。", kind="webops")
        except Exception as e:
            return f"(判定调用失败: {e})"
        d = parse_decision(raw)
        if d and d.get("login"):
            saved = await asyncio.to_thread(_op_save_state)
            return (f"登录成功({d.get('note', '')})。登录态已{'保存' if saved else '暂存'}, "
                    f"以后操作这个网站会自动带上, 不用再登。")
        await asyncio.sleep(5)
        try:
            snap = await asyncio.to_thread(_op_snapshot)
        except Exception as e:
            return f"(页面快照失败: {e})"
    return ("(等待扫码超时(2.5分钟)。备选: 把账号密码写进 browser_task 的任务描述里让它代填, "
            "如「用账号 abc 密码 123 登录并做完测试」; 或换无需登录的站点。)")


def stop():
    """插件卸载/重载时关闭浏览器(下次用时自动重开)"""
    global _BT
    if _BT is not None:
        _BT.stop()
        _BT = None


# ---- 浏览器线程上执行的具体操作 ----

def _op_goto(url: str) -> dict:
    bt = _bt()
    def job():
        page = _ensure_page(bt._st)
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)  # SPA 渲染/首屏网络
        return _snapshot_locked(bt._st)
    return bt.run(job)


def _op_snapshot() -> dict:
    bt = _bt()
    return bt.run(lambda: _snapshot_locked(bt._st))


def _snapshot_locked(st) -> dict:
    page = _ensure_page(st)
    snap = page.evaluate(_SNAP_JS)
    st["els"] = snap.get("els", [])
    return snap


def _op_execute(action: dict) -> str:
    bt = _bt()
    def job():
        page = _ensure_page(bt._st)
        t = (action.get("type") or "").lower()
        idx = action.get("index")
        val = str(action.get("value") or "")
        if t == "press":
            page.keyboard.press(val or "Enter")
            page.wait_for_timeout(1000)
            return "pressed"
        if t == "scroll":
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(600)
            return "scrolled"
        el = page.locator(f'[data-widx="{int(idx)}"]')
        if t == "click":
            el.first.click(timeout=8000)
        elif t == "fill":
            el.first.fill(val, timeout=8000)
        elif t == "select":
            try:
                el.first.select_option(val, timeout=8000)
            except Exception:
                el.first.select_option(label=val, timeout=8000)
        elif t == "check":
            el.first.check(timeout=8000)
        else:
            return f"unknown action: {t}"
        page.wait_for_timeout(1200)  # 提交/跳转/渲染
        return f"{t} ok"
    return bt.run(job)


def _fmt_snapshot(snap) -> str:
    els = []
    for i, e in enumerate(snap.get("els", [])):
        flags = f" type={e['type']}" if e.get("type") else ""
        if e.get("checked"):
            flags += " [已选]"
        if e.get("value"):
            flags += f" 值={e['value']}"
        els.append(f"{i}. <{e['tag']}{flags}> {e['text']}")
    return (f"标题: {snap.get('title', '')}\n网址: {snap.get('url', '')}\n"
            + ("\n可操作元素:\n" + "\n".join(els) if els else "\n(本页没有可操作元素, 可用 scroll/press 或 done)\n")
            + f"\n页面文本(截断):\n{snap.get('text', '')}")


def decide_prompt(goal: str, snap_fmt: str, step: int) -> str:
    return (f"你是网页操作代理, 替主人在浏览器里完成任务。第 {step} 步。\n"
            f"【任务】{goal}\n【当前页面】\n{snap_fmt}\n\n"
            "只输出一个 JSON 对象, 不要输出任何其他文字:\n"
            '{"type": "click|fill|select|check|press|scroll|done", "index": 元素编号, "value": "fill/select/press 的内容", '
            '"result": "type=done 时必填: 任务最终结果(比如测试结果/答案内容), 用中文写清楚"}\n'
            "要点: 单选题用 click 点选项; 下拉用 select; 一页多题就逐个选完再点提交; "
            "出现最终结果(成绩/结果描述)就用 done 并把结果原样整理进 result; 页面没变就换一种动作试。")


def parse_decision(raw: str):
    """从 LLM 输出里抠 JSON: 先整段解析, 再 ```代码块``, 再平衡括号扫描(容忍值里含 { } 和废话)"""
    if not raw:
        return None
    s = raw.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if m:
        for cand in (m.group(1), m.group(1).replace("'", '"')):
            try:
                return json.loads(cand)
            except Exception:
                pass
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                frag = raw[start:i + 1]
                for cand in (frag, frag.replace("'", '"')):
                    try:
                        return json.loads(cand)
                    except Exception:
                        pass
                return None
    return None


async def run_task(url: str, goal: str, llm_call, max_steps: int = _MAX_STEPS):
    """完整任务循环: 打开 → (快照→LLM决策→执行)*N → done 返回结果。llm_call: async (prompt, system=, kind=)"""
    import asyncio
    try:
        snap = await asyncio.to_thread(_op_goto, url)
    except RuntimeError as e:
        return str(e)
    except TimeoutError:
        return "(打开网页超时)"
    except Exception as e:
        return f"(打开网页失败: {e})"
    for step in range(1, max_steps + 1):
        try:
            raw = await llm_call(decide_prompt(goal, _fmt_snapshot(snap), step),
                                 system="你是精准的网页自动化代理, 只输出 JSON。",
                                 kind="webops")
        except Exception as e:
            return f"(决策调用失败: {e})"
        act = parse_decision(raw)
        if not act:
            continue
        if (act.get("type") or "").lower() == "done":
            await asyncio.to_thread(_op_save_state)  # 顺手刷新登录态/Cookies
            return ((act.get("result") or "(任务完成, 但代理没给结果描述)")
                    + f"\n(共 {step} 步, 结束于 {snap.get('url', '')})")
        try:
            await asyncio.to_thread(_op_execute, act)
        except Exception as e:
            logger.warning("[webops] 动作执行失败: %s", e)
        try:
            snap = await asyncio.to_thread(_op_snapshot)
        except Exception as e:
            return f"(页面快照失败: {e})"
    return (f"(已达最大步数 {max_steps}, 任务未确认完成。最后页面: {snap.get('url', '')}\n"
            f"标题: {snap.get('title', '')}\n{snap.get('text', '')[:600]})")
