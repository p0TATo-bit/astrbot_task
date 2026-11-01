# astrbot_task 网页操作(webops): 无头浏览器代理 — 她可以打开网页、点击、填写、提交、读结果。
# 可选能力: 依赖 playwright(容器里 pip install playwright && playwright install chromium)。
# 未安装时 browser_task 返回启用指引, 插件其他功能完全不受影响。
# 线程模型: playwright 的 sync API 对象绑定创建线程 — 所有浏览器操作统一投递到一个
# 常驻专用线程执行(_BrowserThread), LLM 决策留在事件循环, 两边只传数据不传对象。
# browser_task 工具内部跑"看页面→决策→执行"循环(LLM 决策由注入的 llm_call 提供,
# 不占 AstrBot 对话工具轮次); 单例浏览器常驻复用, 单主人串行使用。

import json
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
        st["page"] = st["browser"].new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"))
    return st["page"]


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
