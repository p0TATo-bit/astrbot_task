# astrbot_task: 任务管理 / 长期记忆 / 好感养成(AstrBot 插件, 零第三方依赖)
# 移植自 Teaskks(https://gitee.com/p67at0/teaskks)的核心能力: 任务工具/记忆/好感规则。
# 数据: 插件目录下 data.db(SQLite), 与 Teaskks 独立。

import os
import re
import sys
import json
import time
import shutil
import collections
import sqlite3
import datetime
import asyncio

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 数据库放插件目录之外: 插件更新/重装时插件目录可能被清空, 数据在 AstrBot data 根下才能幸存
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "astrbot_task_data")
_LEGACY_DB = os.path.join(_HERE, "data.db")  # 旧位置(插件目录内), 首次启动自动迁移
DB_PATH = os.path.join(_DATA_DIR, "data.db")

import emotion as emotion_mod
import memory as memory_mod
import scheduler as scheduler_mod
import skills as skills_mod
import tasks_core

WEEKDAY_CN = "日一二三四五六"


def today_str() -> str:
    return datetime.date.today().isoformat()


def now_str() -> str:
    d = datetime.datetime.now()
    return d.strftime(f"%Y-%m-%d %H:%M 星期{WEEKDAY_CN[d.weekday()]}")


class TaskPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._config = self.config if hasattr(self, "config") else {}
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB):
                shutil.copy2(_LEGACY_DB, DB_PATH)  # 旧位置数据自动迁移到新家
                print("[astrbot_task] data.db 已从插件目录迁移到", DB_PATH)
        except Exception as e:
            print("[astrbot_task] 数据目录准备失败:", e)
        self.db_path = DB_PATH  # scheduler/_conn 都经实例属性连库
        try:
            self._main_loop = asyncio.get_running_loop()  # 工作线程的 LLM 调用转投主循环
        except RuntimeError:
            self._main_loop = None
        self._migrate_config()
        self._init_db()
        print(f"[astrbot_task] 数据库: {self.db_path}")
        self.emotion = emotion_mod.EmotionEngine(DB_PATH)
        self.memory = memory_mod.MemoryStore(
            DB_PATH,
            llm_call=self._llm_call,
            identity_prompt_fn=self.emotion.build_context,
        )
        self._last_umo = None      # 最近会话(后台 LLM 任务用它的 provider)
        self._last_interaction_at = None  # 最近一次用户消息时间(主动关心"久未互动"判定, on_any_message 更新)
        self._user_hist = {}       # umo -> deque(用户消息, 20)
        self._ai_hist = {}         # umo -> deque(AI回复, 20)
        self._pending_confirmations = {}  # umo -> {"tool","task_id","title","at"} 破坏性操作二次确认(5分钟过期)
        # 内嵌 WebUI: 宿主按文件路径加载 main.py, 插件目录不在 sys.path——手动加入后才能 import 兄弟模块
        try:
            import webui
            webui.start(DB_PATH,
                        int(self._cfg("webui_port") or 6187),
                        (self._cfg("webui_host") or "").strip() or "0.0.0.0")
        except Exception as e:
            print("[astrbot_task] WebUI 启动失败(任务/记忆功能不受影响):", e)
        # 定时调度(提醒/过期汇总/主动关心/计划推送/备份)
        self.scheduler = scheduler_mod.Scheduler(self)
        self.scheduler.start()

    # ---------- 推送 ----------

    _CFG_GROUPS = {
        "push": ("owner_umo", "push_enabled", "upcoming_minutes"),
        "proactive": ("proactive_enabled", "proactive_random_base", "proactive_window_start",
                      "proactive_window_end", "proactive_max_per_day", "proactive_gap_hours"),
        "admin": ("admin_provider_id",),
        "vision": ("vision_api_url", "vision_api_key", "vision_model"),
        "webui": ("webui_host", "webui_port"),
    }

    def _migrate_config(self):
        """老版扁平配置一次性迁移到分组结构(_CFG_GROUPS): 旧值不丢, 面板按组显示"""
        cfg = self._config
        if not isinstance(cfg, dict):
            return
        known = {k for keys in self._CFG_GROUPS.values() for k in keys}
        if not any(k in cfg for k in known):
            return
        for gkey, keys in self._CFG_GROUPS.items():
            g = cfg.get(gkey)
            g = dict(g) if isinstance(g, dict) else {}
            for k in keys:
                if k in cfg:
                    g[k] = cfg.pop(k)
            cfg[gkey] = g
        try:
            self.save_config(self._config)
            print("[astrbot_task] 配置已迁移为分组结构(大项套小项)")
        except Exception as e:
            print("[astrbot_task] 配置迁移保存失败(运行时仍生效):", e)

    def _cfg(self, key: str, default=None):
        """读配置: 兼容迁移前的扁平键与迁移后的分组键"""
        v = self._config.get(key)
        if v not in (None, ""):
            return v
        for g in self._config.values():
            if isinstance(g, dict) and key in g:
                return g[key]
        return default

    def _target_umo(self) -> str:
        return (self._cfg("owner_umo") or "").strip() or self._last_umo or ""

    async def push(self, text: str):
        """主动推送消息到配置/最近会话"""
        umo = self._target_umo()
        if not umo:
            print("[astrbot_task] 推送跳过: 没有可用的会话目标(先和机器人聊一句即可)")
            return
        try:
            from astrbot.api.message_chain import MessageChain
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            print("[astrbot_task] 推送失败:", e)

    async def persona_notify(self, kind: str, facts: str, fallback: str) -> str:
        """人格口吻生成(失败回退模板)——提醒/主动关心共用"""
        try:
            identity = self.emotion.build_context()
            text = await self._llm_call(
                f"[系统通知] {kind}: {facts}\n\n请以你的口吻把这个信息自然地告诉主人(一两句话, 不要复述本提示)。",
                f"{identity}\n\n提醒类消息: 简短、自然、带一点关心, 不要用列表。",
                kind="notify")
            return text.strip() or fallback
        except Exception as e:
            print("[astrbot_task]", kind, "口吻生成失败, 用模板:", e)
            return fallback

    # ---------- LLM 调用(走 AstrBot provider) ----------

    async def _llm_call(self, prompt: str, system: str = None, kind: str = "misc") -> str:
        """供 memory/emotion/scheduler 回调: 插件内部任务(总结/提取/关键词/意图/口吻/计划)
        统一走管理模型(admin_provider_id, 可配); 未设置或调用失败则跟随当前聊天模型。
        工作线程调用时自动转投主事件循环(修复 SharedPreferences 跨循环绑定错误)。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        main = getattr(self, "_main_loop", None)
        if main is not None and loop is not main:
            fut = asyncio.run_coroutine_threadsafe(self._llm_async(prompt, system, kind), main)
            return await asyncio.wrap_future(fut)
        return await self._llm_async(prompt, system, kind)

    async def _llm_async(self, prompt: str, system: str = None, kind: str = "misc") -> str:
        admin = (self._cfg("admin_provider_id") or "").strip()
        t0 = time.time()
        if admin:
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=admin, prompt=prompt, system_prompt=system or "")
                self._record_usage(kind, admin, resp, int((time.time() - t0) * 1000))
                return resp.completion_text or ""
            except Exception as e:
                print(f"[astrbot_task] 管理模型 {admin} 调用失败, 回退聊天模型:", e)
                self._record_usage(kind, admin, None, int((time.time() - t0) * 1000), ok=0)
        umo = self._last_umo
        provider_id = await self.context.get_current_chat_provider_id(umo=umo) if umo else None
        try:
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt, system_prompt=system or "")
            self._record_usage(kind, provider_id or "", resp, int((time.time() - t0) * 1000))
            return resp.completion_text or ""
        except Exception:
            self._record_usage(kind, provider_id or "", None, int((time.time() - t0) * 1000), ok=0)
            raise

    def _extract_usage(self, resp):
        """从 ProviderResponse 尽力取 token 用量(字段位置随版本/后端不定, 全部防御式)"""
        for cand in (resp, getattr(resp, "raw_completion", None)):
            if cand is None:
                continue
            u = getattr(cand, "usage", None)
            if u is None and isinstance(cand, dict):
                u = cand.get("usage")
            if isinstance(u, dict):
                if u.get("total_tokens") or u.get("prompt_tokens") or u.get("completion_tokens"):
                    return u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens")
            elif u is not None and hasattr(u, "prompt_tokens"):
                return getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None), getattr(u, "total_tokens", None)
        return None, None, None

    def _record_usage(self, kind: str, provider: str = "", resp=None, duration_ms=None, ok: int = 1):
        """调用统计入 llm_usage; 任何失败都不影响主流程"""
        try:
            pt, ct, tt = self._extract_usage(resp)
            model = ""
            try:
                if provider:
                    model = self.context.get_provider_by_id(provider).get_model()
            except Exception:
                pass
            with self._conn() as c:
                c.execute(
                    "INSERT INTO llm_usage (kind, provider, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, ok) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (kind, provider or "", str(model or "")[:80], pt, ct, tt, duration_ms, ok))
        except Exception as e:
            print("[astrbot_task] 记录调用统计失败:", e)

    async def _voice_result(self, raw: str, scene: str = "任务结果") -> str:
        """工具结果转人格口吻短句(走管理模型); 关闭或失败回退原文"""
        if not self._cfg("tool_voice_enabled", True):
            return raw
        return await self.persona_notify(scene, raw[:600], raw)

    def _snap(self, kind: str):
        """任务变更前快照(供 /任务 撤销 与 undo_task_change), 保留最近 30 份"""
        try:
            with self._conn() as c:
                c.execute("INSERT INTO task_snapshots (kind, payload) VALUES (?,?)",
                          (kind, tasks_core.snapshot_tasks(c)))
                c.execute("DELETE FROM task_snapshots WHERE id NOT IN "
                          "(SELECT id FROM task_snapshots ORDER BY id DESC LIMIT 30)")
        except Exception as e:
            print("[astrbot_task] 快照失败:", e)

    def _do_undo(self, steps: int) -> str:
        """回退最近 n 步任务变更, 返回给用户看的结果文本"""
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, kind, payload, created_at FROM task_snapshots ORDER BY id DESC").fetchall()]
            if not rows:
                return "没有可撤销的历史。"
            n = max(1, min(len(rows), int(steps or 1)))
            snap = rows[n - 1]
            tasks_core.restore_tasks(c, snap["payload"])
            cnt = len(json.loads(snap["payload"]).get("tasks", []))
        return (f"已回退 {n} 步(回到 {snap['created_at']} 的「{snap['kind']}」之前)。"
                f"当前任务 {cnt} 个, 还可继续撤销 {len(rows) - n} 步。")

    def _spawn(self, coro):
        """安全投递后台异步任务(记录引用防GC)"""
        try:
            task = asyncio.get_event_loop().create_task(coro)
            self._background = getattr(self, "_background", [])
            self._background.append(task)
            self._background = [t for t in self._background if not t.done()][-20:]
        except Exception as e:
            print("[astrbot_task] 后台任务投递失败:", e)

    async def terminate(self):
        """插件卸载/重载时由 AstrBot 调用(await): 停调度循环与 WebUI 线程"""
        try:
            if getattr(self, "scheduler", None) and self.scheduler.task:
                self.scheduler.task.cancel()
        except Exception:
            pass
        try:
            import webui
            webui.stop()
        except Exception:
            pass

    # ---------- 数据层 ----------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    due_date TEXT,
                    due_time TEXT,
                    end_time TEXT,
                    priority INTEGER DEFAULT 3,
                    recurring TEXT,
                    recurrence_end TEXT,
                    category TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    memory_type TEXT DEFAULT 'fact',
                    importance INTEGER DEFAULT 3,
                    intensity REAL DEFAULT 0,
                    valence REAL DEFAULT 0,
                    keywords TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS emotion (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    relation_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    sent INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS notify_log (
                    key TEXT PRIMARY KEY,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS task_occurrence_completions (
                    task_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    PRIMARY KEY (task_id, date)
                );
                CREATE TABLE IF NOT EXISTS task_occurrence_overrides (
                    task_id INTEGER NOT NULL,
                    orig_date TEXT NOT NULL,
                    new_date TEXT NOT NULL,
                    due_time TEXT,
                    end_time TEXT,
                    PRIMARY KEY (task_id, orig_date)
                );
                CREATE TABLE IF NOT EXISTS proactive_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT DEFAULT 'misc',
                    provider TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    duration_ms INTEGER,
                    ok INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS task_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT DEFAULT 'change',
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS quiz_bank (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    ok_count INTEGER DEFAULT 0,
                    no_count INTEGER DEFAULT 0,
                    last_asked TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                """
            )
            # 迁移: 老库 tasks 表补循环截止列与分类列
            try:
                c.execute("ALTER TABLE tasks ADD COLUMN recurrence_end TEXT")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE tasks ADD COLUMN category TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE tasks ADD COLUMN end_time TEXT")
            except Exception:
                pass

    # ---------- 指令 ----------

    @filter.command("任务")
    async def task_cmd(self, event: AstrMessageEvent, action: str = "帮助", *args):
        """/任务 指令入口"""
        action = (action or "帮助").strip()
        args = [a for a in args if a]

        if action in ("帮助", "help", ""):
            yield event.plain_result(
                "任务指令:\n"
                "/任务 列表 — 查看未完成任务\n"
                "/任务 添加 标题 [YYYY-MM-DD] [HH:mm] [P1-5]\n"
                "/任务 完成 编号\n"
                "/任务 删除 编号\n"
                "/任务 撤销 [步数] — 回退最近的任务变更\n"
                "/任务 清空 — 清空全部任务(可撤销)\n"
                "/任务 好感 — 查看好感数值\n"
                "/任务 模型 — 查看插件管理模型"
            )
            return

        if action == "列表":
            with self._conn() as c:
                rows = c.execute(
                    "SELECT id, title, due_date, due_time, priority FROM tasks "
                    "WHERE status='pending' ORDER BY due_date IS NULL, due_date, due_time, priority LIMIT 20"
                ).fetchall()
            if not rows:
                yield event.plain_result("当前没有未完成任务")
                return
            lines = [f"{r['id']}. {r['title']}  [{r['due_date'] or '无日期'}{' ' + r['due_time'] if r['due_time'] else ''} P{r['priority']}]" for r in rows]
            yield event.plain_result("未完成任务:\n" + "\n".join(lines))
            return

        if action == "添加":
            if not args:
                yield event.plain_result("用法: /任务 添加 标题 [YYYY-MM-DD] [HH:mm] [P1-5]")
                return
            title = args[0]
            due_date, due_time, priority = "", "", 3
            for a in args[1:]:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
                    due_date = a
                elif re.fullmatch(r"\d{1,2}:\d{2}", a):
                    due_time = a
                elif re.fullmatch(r"P[1-5]", a.upper()):
                    priority = int(a[1])
            with self._conn() as c:
                cur = c.execute(
                    "INSERT INTO tasks (title, due_date, due_time, priority) VALUES (?,?,?,?)",
                    (title, due_date, due_time, priority),
                )
                tid = cur.lastrowid
            yield event.plain_result(f"已创建任务 #{tid}: {title}" + (f" 截止 {due_date} {due_time}" if due_date else ""))
            return

        if action in ("完成", "删除"):
            if not args or not args[0].isdigit():
                yield event.plain_result(f"用法: /任务 {action} 编号(见 /任务 列表)")
                return
            tid = int(args[0])
            with self._conn() as c:
                row = c.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
                if not row:
                    yield event.plain_result(f"找不到任务 #{tid}")
                    return
                if action == "完成":
                    c.execute("UPDATE tasks SET status='completed' WHERE id=?", (tid,))
                else:
                    c.execute("DELETE FROM tasks WHERE id=?", (tid,))
            yield event.plain_result(f"已{action}任务 #{tid}: {row['title']}")
            return

        if action == "撤销":
            steps = int(args[0]) if args and args[0].isdigit() else 1
            yield event.plain_result(self._do_undo(steps))
            return

        if action == "清空":
            if not args or args[0] != "确认":
                yield event.plain_result("将清空全部任务与打卡记录(记忆/好感不受影响, 且可 /任务 撤销 找回)。\n确认请发: /任务 清空 确认")
                return
            self._snap("清空")
            with self._conn() as c:
                n = c.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]
                c.execute("DELETE FROM tasks")
                c.execute("DELETE FROM task_occurrence_completions")
            yield event.plain_result(f"已清空 {n} 个任务。反悔可发 /任务 撤销。")
            return

        if action == "好感":
            _, rel = self.emotion.get()
            yield event.plain_result(
                f"信任 {rel['trust']:.2f} · 任务好感 {rel['task_liking']:.2f} · 社交好感 {rel['social_liking']:.2f}"
                "(无上限, 满1=羁绊, 继续累积)"
            )
            return

        if action == "模型":
            admin = (self._cfg("admin_provider_id") or "").strip()
            lines = [f"插件管理模型: {admin if admin else '(未设置, 跟随当前聊天模型)'}",
                     "内部任务(记忆总结/提取/关键词/意图分析/情绪微调/推送口吻/计划生成)都用它;在插件配置里改。"]
            try:
                provs = self.context.get_all_providers() or []
                ids = [str(getattr(p, "id", "?")) for p in provs]
                if ids:
                    lines.append("可用 Provider ID: " + "、".join(ids))
            except Exception:
                lines.append("(无法枚举 Provider, 请到面板-服务提供商页查看 ID)")
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result("未知子命令, 用 /任务 帮助 查看")

    # ---------- 全消息监听: 情绪规则 + 轮数计数 + 周期总结触发(不产出回复) ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        text = event.message_str or ""
        umo = event.unified_msg_origin
        self._last_umo = umo
        self._last_interaction_at = datetime.datetime.now()
        if not text:
            return
        self.emotion.on_message(text)  # 情绪规则+衰减+好感
        state, _ = self.emotion.get()
        self._user_hist.setdefault(umo, collections.deque(maxlen=20)).append(text)
        try:
            with self._conn() as c:
                c.execute("INSERT INTO chats (umo, role, text) VALUES (?,?,?)", (umo, "user", text[:2000]))
        except Exception as e:
            print("[astrbot_task] 对话记录失败:", e)
        rounds = self.memory.bump_round(umo)
        if rounds > 0 and rounds % 20 == 0:
            # 满20轮: 后台生成周期总结(对话文本=双方最近各20条 zip)
            users = list(self._user_hist.get(umo, []))
            ais = list(self._ai_hist.get(umo, []))
            pairs = list(zip(users[-20:], (ais + [""] * 20)[-len(users[-20:]):]))
            convo = "\n\n".join(f"用户: {u}\n助手: {a}" for u, a in pairs)
            identity = self.emotion.build_context()
            self._spawn(asyncio.to_thread(self.memory.periodic_summary, umo, convo, identity))
            print(f"[astrbot_task] 满{rounds}轮, 后台生成周期总结")

    @filter.on_llm_response()
    async def on_llm_response(self, event, resp):
        """AI 回复落地: 配对历史 + 情绪微调已在召回阶段做 + 后台自动提取记忆"""
        try:
            umo = event.unified_msg_origin
            self._last_umo = umo
            reply = getattr(resp, "completion_text", "") or (resp.result_chain if hasattr(resp, "result_chain") else "")
            if reply:
                self._ai_hist.setdefault(umo, collections.deque(maxlen=20)).append(str(reply))
                self._record_usage("chat", "", resp, None)
                try:
                    with self._conn() as c:
                        c.execute("INSERT INTO chats (umo, role, text) VALUES (?,?,?)", (umo, "ai", str(reply)[:2000]))
                except Exception as e:
                    print("[astrbot_task] 对话记录失败:", e)
                if self._cfg("auto_extract_enabled", False):  # 默认关: 记忆节点只来自 20 轮总结 + 「记住X」
                    users = self._user_hist.get(umo, [])
                    last_user = users[-1] if users else ""
                    self._spawn(asyncio.to_thread(
                        self.memory.auto_extract, umo, last_user, str(reply),
                        self._history_text(umo)))
        except Exception as e:
            print("[astrbot_task] 回复后处理失败:", e)

    # ---------- LLM 请求钩子: 两阶段召回(意图+情绪微调)+情境注入 ----------

    @filter.on_llm_request()
    async def on_llm_request(self, event, req):
        try:
            umo = event.unified_msg_origin
            self._last_umo = umo
            text = event.message_str or getattr(req, "prompt", "") or ""
            state, _ = self.emotion.get()

            # 识图: 消息链里有图片且配置了识图模型 → 描述注入(每张独立, 失败单张降级)
            try:
                cands = [c for c in (getattr(req, "contexts", None) or [])
                         if type(c).__name__ == "Image"]
                cands += [c for c in (getattr(event, "message", None) or [])
                          if type(c).__name__ == "Image"]
                imgs, seen = [], set()
                for c in cands:
                    u = getattr(c, "url", None) or getattr(c, "file", None)
                    if u and u not in seen:
                        seen.add(u)
                        imgs.append(u)
                vkey = (self._cfg("vision_api_key") or "").strip()
                vurl = (self._cfg("vision_api_url") or "").strip()
                vmodel = (self._cfg("vision_model") or "").strip()
                if imgs and vkey and vurl and vmodel:
                    import vision as vision_mod
                    descs = []
                    for u in imgs[:3]:
                        try:
                            descs.append(await asyncio.to_thread(
                                vision_mod.describe_image, u, vurl, vkey, vmodel))
                        except Exception as ie:
                            descs.append("(该图片识别失败)")
                    req.system_prompt = (req.system_prompt or "") + "\n\n" + \
                        f"【用户发来{len(imgs)}张图片, 识图结果】\n" + "\n---\n".join(descs)
            except Exception as e:
                print("[astrbot_task] 识图流程失败:", e)

            # 两阶段: 先让快模型判断需要回忆什么+情绪微调(9s超时, 失败降级单路); 寒暄/超短消息直接跳过省一次调用
            ctx = ""
            if not self._skip_intent(text):
                try:
                    intent = await asyncio.wait_for(
                        self._intent_call(umo, text, self._history_text(umo)), timeout=9)
                    intent_words = intent.get("recall") or []
                    if isinstance(intent.get("emotion"), dict) and intent["emotion"]:
                        self.emotion.apply_adjust(intent["emotion"], "情绪微调", 0.08)
                    state, _ = self.emotion.get()  # 微调后取最新
                    recalled, seen = [], set()
                    for w in (intent_words or [text]):
                        for m in self.memory.associative_recall(w, mood=state["P"], limit=3):
                            if m["id"] not in seen:
                                seen.add(m["id"])
                                recalled.append(m)
                    recalled.sort(key=lambda x: -x["activation"])
                    ctx = self.memory.context_text(recalled[:6])
                except Exception as e:
                    print("[astrbot_task] 召回意图降级单路:", e)
            if not ctx:
                state, _ = self.emotion.get()
                ctx = self.memory.context_text(self.memory.associative_recall(text, mood=state["P"], limit=5))

            parts = [self.emotion.build_context()]
            if ctx:
                parts.append(ctx)
            parts.append(f"【当前时间】{now_str()} —— 涉及今天/明天/几点等时间推理以此为准, 不要猜测")
            inject = "\n\n".join(parts)
            req.system_prompt = (req.system_prompt or "") + "\n\n" + inject
        except Exception as e:
            print("[astrbot_task] LLM 请求注入失败:", e)

    _SKIP_INTENT_RE = re.compile(
        r"^(确认|取消|好的|嗯+|哦+|噢+|在吗|你好|您好|早安|晚安|早上好|下午好|晚上好|谢谢|感谢|辛苦|哈哈+|嘿嘿+|嘻嘻|嗨|hi|hello|ok|okay|拜拜|再见)"
        r"[\s,。!！?？~～]*$", re.I)

    @staticmethod
    def _skip_intent(text: str) -> bool:
        """寒暄/超短消息跳过意图分析(省一次 LLM 调用与延迟), 直接用原文召回"""
        t = str(text or "").strip()
        return len(t) < 6 or bool(TaskPlugin._SKIP_INTENT_RE.match(t))

    async def _intent_call(self, umo, text, history):
        prompt = ((("近期对话(截尾):\n" + str(history)[-500:] + "\n\n") if history else "") +
                  "主人最新消息: " + str(text)[:500])
        system = ('你是内部检索规划器, 不直接回复主人。判断: 为了更好地回复主人最新这条消息, 需要回忆哪些记忆?'
                  '只输出JSON: {"recall":["检索词",...(3-5个)],"emotion":{可选:"P","A","fatigue","frustration","trust","social_liking" 每项-0.08~0.08}}。'
                  '无明显情绪信号 emotion 给 {}。只输出JSON。' + self.emotion.build_context())
        raw = await asyncio.wait_for(self._llm_call(prompt, system, kind="intent"), timeout=8.5)
        m = re.search(r"\{[\s\S]*\}", raw or "")
        return json.loads(m[0]) if m else {}

    def _history_text(self, umo) -> str:
        users = list(self._user_hist.get(umo, []))[-4:]
        ais = list(self._ai_hist.get(umo, []))[-4:]
        return "\n".join(f"用户: {u}\n助手: {a}" for u, a in zip(users, ais))

    # ---------- LLM 工具(自然语言自动调用) ----------

    @filter.llm_tool(name="current_time")
    async def tool_current_time(self, event: AstrMessageEvent):
        """获取当前的日期、星期和时间。涉及"今天/明天/几点/现在"等时间推理时必须先调用本工具。

        Args:
        """
        return (now_str())

    @filter.llm_tool(name="create_task")
    async def tool_create_task(self, event: AstrMessageEvent, title: str, due_date: str = "", due_time: str = "", end_time: str = "", priority: int = 3, recurring: str = "", recurrence_end: str = "", category: str = "", force: str = ""):
        """为用户创建一个任务/提醒事项。创建前应先用 list_tasks 对照避免重复。与已有任务时段冲突会被拒绝并把冲突任务报给你。

        Args:
            title(string): 任务标题
            due_date(string): 截止/第一次发生日期 YYYY-MM-DD(相对日期先调用 current_time 换算, 不确定可留空)
            due_time(string): 开始时间 HH:mm(可选, 如上课/开会时间)
            end_time(string): 结束时间 HH:mm(可选)。课程/会议等占用时间段的活动务必提供, 时间冲突检测靠它
            priority(number): 优先级 1-5, 1 最高, 默认 3
            recurring(string): 重复类型 daily/weekly/monthly/yearly 或留空(一次性)。课程表等长期固定日程必须用循环(如每周一的课填 weekly), 严禁为每周单独建任务
            recurrence_end(string): 重复到此日为止 YYYY-MM-DD(如学期最后一节课的日期, 留空=一直重复)
            category(string): 分类(如 课程/学习/考研/生活, 可留空)
            force(string): 仅当时间冲突被拒绝且主人明确表示要同时保留时才填 "true"; 默认留空
        """
        self._snap("create")
        with self._conn() as c:
            tid, err = tasks_core.create_task(c, {
                "title": title, "due_date": due_date, "due_time": due_time, "end_time": end_time,
                "priority": priority, "recurring": recurring,
                "recurrence_end": recurrence_end, "category": category,
            }, force=self._is_confirmed(force))
        if err:
            return await self._voice_result(err, "创建失败")
        rec_label = {"daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年"}.get(recurring, "")
        desc = f" 截止 {due_date} {due_time}".rstrip() if due_date else ""
        if rec_label:
            desc = f" {rec_label}{(' 至 ' + recurrence_end) if recurrence_end else ''} 首次 {due_date} {due_time}".rstrip()
        if category:
            desc += f" [{category}]"
        return await self._voice_result(f"任务已创建 #{tid}: {title}{desc}", "创建任务")

    @filter.llm_tool(name="create_tasks")
    async def tool_create_tasks(self, event: AstrMessageEvent, tasks_json: str, force: str = ""):
        """批量创建任务(一次导入课程表/多行清单, 最多40项)。每项字段与 create_task 相同。时段冲突的项会被拒绝并报出第几项。

        Args:
            tasks_json(string): JSON数组字符串, 每项形如 {"title":"高数课","due_date":"2026-10-26","due_time":"08:30","end_time":"10:10","priority":3,"recurring":"weekly","recurrence_end":"2027-02-15","category":"课程"}。课程表导入必须用它, 循环课程 recurring 填 weekly 并给 recurrence_end; 占用时间段的活动每项都带 end_time
            force(string): 仅当有项因时间冲突被拒绝且主人明确表示要同时保留时才填 "true"; 默认留空
        """
        try:
            items = json.loads((tasks_json or "").strip())
        except Exception:
            return ("tasks_json 不是合法的 JSON 数组")
            return
        if not isinstance(items, list) or not items:
            return ("tasks_json 必须是非空数组")
            return
        if len(items) > 40:
            return ("单次最多创建 40 个任务")
            return
        created, first_err = [], None
        skip_conflict = self._is_confirmed(force)
        self._snap("create_tasks")
        with self._conn() as c:
            for i, item in enumerate(items):
                tid, err = tasks_core.create_task(c, item, force=skip_conflict)
                if err:
                    first_err = f"第 {i + 1} 项未创建: {err}"
                    break
                created.append(tid)
        if first_err:
            return await self._voice_result(
                f"批量导入中断: 已创建 {len(created)} 个, 第 {i + 1} 项「{str(items[i].get('title') or '')[:30]}」未创建({first_err.split(': ', 1)[-1]})。"
                "告知主人, 询问是调整后继续还是强制保留", "批量导入中断")
        return await self._voice_result(f"批量导入完成, 共创建 {len(created)} 个任务", "批量导入完成")

    @filter.llm_tool(name="update_task")
    async def tool_update_task(self, event: AstrMessageEvent, task_id: int, title: str = "", due_date: str = "", due_time: str = "", end_time: str = "", priority: int = 0, recurring: str = "", recurrence_end: str = "", category: str = "", force: str = ""):
        """修改任务(只改需要改的字段, 其余留空=保持不变)。task_id 从 list_tasks 获取。改出时段冲突会被拒绝并报出冲突任务。

        Args:
            task_id(number): 任务编号(先用 list_tasks 查到)
            title(string): 新标题(留空=不改)
            due_date(string): 新日期 YYYY-MM-DD(留空=不改)
            due_time(string): 新开始时间 HH:mm(留空=不改)
            end_time(string): 新结束时间 HH:mm(留空=不改)
            priority(number): 新优先级 1-5(填 0=不改)
            recurring(string): 新重复类型 daily/weekly/monthly/yearly; 填 none 取消循环; 留空=不改
            recurrence_end(string): 新循环截止日 YYYY-MM-DD(留空=不改)
            category(string): 新分类(留空=不改)
            force(string): 仅当时间冲突被拒绝且主人明确表示要同时保留时才填 "true"; 默认留空
        """
        self._snap("update")
        task, err = None, None
        with self._conn() as c:
            task, err = tasks_core.update_task(c, task_id, {
                "title": title, "due_date": due_date, "due_time": due_time, "end_time": end_time,
                "priority": priority, "recurring": recurring,
                "recurrence_end": recurrence_end, "category": category,
            }, force=self._is_confirmed(force))
        if err:
            return await self._voice_result(err, "修改失败")
        return await self._voice_result(
            f"任务 #{task_id} 已更新: 「{task['title']}」"
            + (f" {task.get('recurring') or ''}" or "")
            + (f" [{task.get('category')}]" if task.get("category") else ""), "修改任务")

    @filter.llm_tool(name="list_tasks")
    async def tool_list_tasks(self, event: AstrMessageEvent):
        """列出用户当前所有未完成任务(标题/截止/优先级/循环/分类)。结果仅供你参考,
        回复主人时用你的口吻总结重点(数量/今天明天有什么/冲突提醒), 不要原样复述整个列表。

        Args:
        """
        rec_label = {"daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年"}
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, title, due_date, due_time, priority, recurring, recurrence_end, category FROM tasks "
                "WHERE status='pending' ORDER BY due_date IS NULL, due_date, due_time, priority LIMIT 30"
            ).fetchall()
        if not rows:
            return ("(没有未完成任务)")
            return
        lines = []
        for r in rows:
            bits = [f"· [#{r['id']}] {r['title']}", f"[{r['due_date'] or '无日期'}{' ' + r['due_time'] if r['due_time'] else ''} P{r['priority']}"]
            if r["recurring"]:
                bits[1] += f" {rec_label.get(r['recurring'], r['recurring'])}{'至' + r['recurrence_end'] if r['recurrence_end'] else ''}"
            if r["category"]:
                bits[1] += f" {r['category']}"
            bits[1] += "]"
            lines.append(" ".join(bits))
        return ("\n".join(lines))

    # 破坏性操作二次确认: 首次调用只登记+问话, 主人明确确认后 LLM 带 confirmed=true 再来才执行
    def _pend_confirm(self, umo, tool, task_id, title):
        self._pending_confirmations[umo] = {"tool": tool, "task_id": int(task_id), "title": title, "at": time.time()}

    def _pop_confirm(self, umo, tool, task_id):
        p = self._pending_confirmations.get(umo)
        if not p or p["tool"] != tool or p["task_id"] != int(task_id) or (time.time() - p["at"]) > 300:
            self._pending_confirmations.pop(umo, None)
            return None
        del self._pending_confirmations[umo]
        return p

    @staticmethod
    def _is_confirmed(v) -> bool:
        return str(v or "").strip().lower() in ("true", "yes", "1", "是", "确认")

    @filter.llm_tool(name="complete_task")
    async def tool_complete_task(self, event: AstrMessageEvent, task_id: int, confirmed: str = "", all: str = ""):
        """完成任务。循环任务默认只打卡今天这一次(可逆免确认, 循环继续); 整个循环结束才加 all=true(需确认)。

        Args:
            task_id(number): 任务编号(先用 list_tasks 查到)
            all(string): 留空=循环任务只勾今天这次(打卡), 一次性任务直接完成; 填 "true"=把整个循环/任务标记完成(不再重复出现)
            confirmed(string): 仅整体完成需要: 主人第一次要求时留空(系统会向主人确认), 主人明确回复确认后再次调用才填 "true"
        """
        umo = event.unified_msg_origin
        with self._conn() as c:
            row = c.execute(
                "SELECT id, title, due_date, recurring FROM tasks WHERE id=? AND status='pending'",
                (int(task_id),)).fetchone()
        if not row:
            return (f"找不到未完成任务 #{task_id}")
            return
        want_all = self._is_confirmed(all)
        if row["recurring"] and not want_all:
            today = datetime.date.today().isoformat()
            occ = tasks_core.latest_occurrence_date(dict(row), today)
            if not occ:
                return (f"「{row['title']}」首次发生是 {row['due_date']}, 今天还没有可打卡的这一次。")
                return
            with self._conn() as c:
                already = c.execute(
                    "SELECT 1 FROM task_occurrence_completions WHERE task_id=? AND date=?",
                    (int(task_id), occ)).fetchone()
                if already:
                    return (f"「{row['title']}」({occ})已经打过卡了, 循环继续。")
                self._snap("打卡")
                tasks_core.toggle_occurrence(c, int(task_id), occ, True)
            return await self._voice_result(
                f"已打卡「{row['title']}」({occ} 这一次), 循环继续。整个循环结束时告诉我「全部完成 {task_id}」。", "课程打卡")
        if not want_all or not self._is_confirmed(confirmed):
            self._pend_confirm(umo, "complete", task_id, row["title"] + ("(整个循环)" if row["recurring"] else ""))
            tip = "整个循环将被标记完成, 之后不再重复出现" if row["recurring"] else "任务将标记为已完成"
            return (f"任务「{row['title']}」确认完成吗? {tip}。回复确认后执行, 5分钟内有效。")
        if not self._pop_confirm(umo, "complete", task_id):
            return ("没有待确认的完成操作, 请先向主人确认后再执行。")
        self._snap("complete")
        with self._conn() as c:
            title, err = tasks_core.complete_task(c, task_id)
        return await self._voice_result(
            f"已完成。任务「{title}」已标记完成。" if not err else f"操作失败: {err}", "完成任务")

    @filter.llm_tool(name="delete_task")
    async def tool_delete_task(self, event: AstrMessageEvent, task_id: int, confirmed: str = ""):
        """删除任务(破坏性操作, 需主人二次确认)。

        Args:
            task_id(number): 任务编号(先用 list_tasks 查到)
            confirmed(string): 严格遵守: 主人第一次要求删除时留空(系统会向主人确认); 只有主人明确回复确认后再次调用才填 "true"。主人未确认时绝不能填 true
        """
        umo = event.unified_msg_origin
        if not self._is_confirmed(confirmed):
            with self._conn() as c:
                row = c.execute("SELECT title FROM tasks WHERE id=?", (int(task_id),)).fetchone()
            if not row:
                return (f"找不到任务 #{task_id}")
                return
            self._pend_confirm(umo, "delete", task_id, row["title"])
            return (f"确定要删除任务「{row['title']}」吗? 回复确认后执行, 5分钟内有效(回复取消则放弃)。")
            return
        if not self._pop_confirm(umo, "delete", task_id):
            return ("没有待确认的删除操作, 请先向主人确认后再执行。")
        self._snap("delete")
        with self._conn() as c:
            title, err = tasks_core.delete_task(c, task_id)
        return await self._voice_result(
            f"已删除任务「{title}」。" if not err else f"删除失败: {err}", "删除任务")

    @filter.llm_tool(name="store_memory")
    async def tool_store_memory(self, event: AstrMessageEvent, content: str, memory_type: str = "fact", importance: int = 3):
        """把内容存入长期记忆。仅当主人明确说"记住X/帮我记一下"时才调用;
        对话中的重要信息由每 20 轮的周期总结自动处理, 不要主动存储。

        Args:
            content(string): 第一人称陈述, 如"主人每周三下午有实验室组会"
            memory_type(string): fact(事实) / preference(偏好) / task_context(任务背景)
            importance(number): 重要性 1-5, 核心目标给 4-5
        """
        content = content.strip()
        if len(content) < 4:
            return ("content 过短, 不存储")
            return
        import memory as memory_mod
        affect = memory_mod.estimate_affect(content)
        r = self.memory.upsert_similar(
            content, memory_type if memory_type in ("fact", "preference", "task_context") else "fact",
            importance, affect["intensity"], affect["valence"])
        if r and r.get("skipped"):
            return ("该记忆已存在, 跳过")
        elif r and r.get("upgraded"):
            self._spawn(asyncio.to_thread(self.memory.enrich_keywords, r["id"], content))
            return (f"记忆已升级: {content[:60]}")
        else:
            self._spawn(asyncio.to_thread(self.memory.enrich_keywords, r["id"], content))
            return (f"已记住: {content[:60]}")

    @filter.llm_tool(name="search_memory")
    async def tool_search_memory(self, event: AstrMessageEvent, keyword: str):
        """按关键词扩散搜索关于用户的长期记忆(回忆不起用户信息时调用)。结果仅供你参考,
        回复主人时用你的口吻自然引用, 不要原样复述列表。

        Args:
            keyword(string): 检索关键词
        """
        keyword = keyword.strip()
        if not keyword:
            return ("(无关键词)")
            return
        _, rel = self.emotion.get()
        recalled = self.memory.associative_recall(keyword, mood=0.0, limit=8)
        if not recalled:
            return ("(没有相关记忆)")
            return
        return ("\n".join(
            f"[{m['memory_type']}|重要度{m['importance']}] {str(m['content'])[:120]}" for m in recalled))

    @filter.llm_tool(name="web_search")
    async def tool_web_search(self, event: AstrMessageEvent, query: str):
        """联网搜索(Bing): 查新闻/资料/实时信息等需要外部信息时用; 结果含链接, 需要详情再用 open_url。

        Args:
            query(string): 搜索词(可空格组合关键词)
        """
        try:
            results = await asyncio.to_thread(skills_mod.web_search, query, 6)
        except Exception as e:
            return (f"搜索失败: {e}")
        lines = [f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results)]
        return ("\n".join(lines) + "\n(需要某条详情时用 open_url 打开链接)")

    @filter.llm_tool(name="open_url")
    async def tool_open_url(self, event: AstrMessageEvent, url: str):
        """打开一个网页并读取正文文字(配合 web_search 的链接使用)。

        Args:
            url(string): 完整 http/https 网址
        """
        import urllib.request
        url = url.strip()
        if not re.match(r"^https?://", url):
            return ("url 必须是 http/https 完整网址")
            return
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9"})
            html = urllib.request.urlopen(req, timeout=15).read(2 * 1024 * 1024).decode("utf-8", "ignore")
            text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", "", html)
            text = re.sub(r"</(p|div|li|h[1-6]|tr|br)>", "\n", text, flags=re.I)
            text = re.sub(r"<[^>]+>", "", text)
            for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
                text = text.replace(ent, ch)
            text = "\n".join(s.strip() for s in text.split("\n") if s.strip())[:4000]
            if not text:
                return ("(页面没有可读文本)")
                return
            return (text + ("\n(内容过长已截断)" if len(text) >= 4000 else ""))
        except Exception as e:
            return (f"打开网页失败: {e}")

    @filter.llm_tool(name="set_reminder")
    async def tool_set_reminder(self, event: AstrMessageEvent, remind_at: str, content: str):
        """设置一次性定时提醒, 到点后机器人会主动推送。用户说"X点提醒我Y"时调用。

        Args:
            remind_at(string): 提醒时间 YYYY-MM-DD HH:mm(相对时间先调用 current_time 换算)
            content(string): 提醒内容
        """
        content = content.strip()
        if not content:
            return ("content 不能为空")
            return
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", remind_at.strip()):
            return ("remind_at 格式必须是 YYYY-MM-DD HH:mm(相对时间先调用 current_time 换算)")
            return
        if remind_at.strip() <= datetime.datetime.now().strftime("%Y-%m-%d %H:%M"):
            return ("提醒时间必须在未来")
            return
        with self._conn() as c:
            c.execute("INSERT INTO ai_reminders (content, remind_at) VALUES (?, ?)", (content, remind_at.strip()))
        return (f"提醒已设置: {remind_at.strip()} — {content}")

    @filter.llm_tool(name="move_occurrence")
    async def tool_move_occurrence(self, event: AstrMessageEvent, task_id: int, orig_date: str, new_date: str, due_time: str = "", end_time: str = "", force: str = ""):
        """把循环任务的某一次单独改期/挪动(只影响那一天, 其他周照旧)。适合"这周的课调到下周X"这类调课。

        Args:
            task_id(number): 任务编号(先用 list_tasks 查到)
            orig_date(string): 原来那一次的日期 YYYY-MM-DD
            new_date(string): 挪到哪一天 YYYY-MM-DD(相对日期先调用 current_time 换算)
            due_time(string): 新开始时间 HH:mm(留空=沿用原来的)
            end_time(string): 新结束时间 HH:mm(留空=沿用原来的)
            force(string): 仅当目标时段冲突且主人明确要同时保留时才填 "true"; 默认留空
        """
        self._snap("调课")
        with self._conn() as c:
            text, err = tasks_core.move_occurrence(
                c, task_id, orig_date, new_date, due_time, end_time, force=self._is_confirmed(force))
        if err:
            return await self._voice_result(err, "调课失败")
        return await self._voice_result(text, "单次调课")

    @filter.llm_tool(name="undo_task_change")
    async def tool_undo_task_change(self, event: AstrMessageEvent, steps: int = 1):
        """撤销最近的任务变更(创建/批量导入/修改/删除/完成/打卡), 可连续多步回退。建错课表或误操作时用。

        Args:
            steps(number): 回退几步, 默认 1(一步=一次变更之前的状态)
        """
        return await self._voice_result(self._do_undo(int(steps or 1)), "撤销变更")

    # ---------- 技能: 笔记文档 / 精确计算 / 天气 ----------

    @filter.llm_tool(name="write_note")
    async def tool_write_note(self, event: AstrMessageEvent, title: str, content: str, overwrite: str = ""):
        """把整理好的内容存成 markdown 笔记文档(复习提纲/错题整理/总结模板等), WebUI「文档」页可查看下载。主人说"存成笔记/写个文档保存"时用。

        Args:
            title(string): 笔记标题(同时是文件名)
            content(string): markdown 正文
            overwrite(string): 已存在同名笔记且主人确认覆盖时才填 "true", 默认留空
        """
        try:
            existed = None
            try:
                existed = os.path.exists(skills_mod.note_path(self.db_path, title))
            except Exception:
                pass
            skills_mod.write_note(self.db_path, title, content)
            if existed:
                return await self._voice_result(
                    f"已有同名笔记「{title}」, 已按新内容覆盖。要保留两份的话换个标题另存", "覆盖笔记")
            return await self._voice_result(
                f"笔记「{title}」已保存({len(str(content or ''))} 字), WebUI 文档页可以查看和下载", "写笔记")
        except Exception as e:
            return await self._voice_result(f"写笔记失败: {e}", "写笔记失败")

    @filter.llm_tool(name="list_notes")
    async def tool_list_notes(self, event: AstrMessageEvent):
        """列出所有已保存的笔记标题。主人问"我有哪些笔记/文档"时用。结果仅供你参考, 用口吻总结, 不要原样复述。

        Args:
        """
        notes = skills_mod.list_notes(self.db_path)
        if not notes:
            return ("还没有保存过笔记")
        return (f"共 {len(notes)} 篇: " + "、".join(n["title"] for n in notes))

    @filter.llm_tool(name="read_note")
    async def tool_read_note(self, event: AstrMessageEvent, title: str):
        """读取一篇笔记的完整内容。主人想看、引用或基于某篇笔记继续写时用。

        Args:
            title(string): 笔记标题
        """
        try:
            return skills_mod.read_note(self.db_path, title)
        except Exception as e:
            return (f"读取失败: {e}")

    @filter.llm_tool(name="calculate")
    async def tool_calculate(self, event: AstrMessageEvent, expression: str):
        """精确计算数学表达式。任何需要数值计算(加减乘除/乘方/开方/对数/阶乘/三角)的场景都必须用它, 绝不要心算, 多位数运算尤其如此。

        Args:
            expression(string): 数学表达式, 支持 + - * / // % ** 括号、sqrt/sin/cos/tan/log/log2/log10/exp/abs/round/floor/ceil/factorial、pi/e。如 (1+2)*3/sqrt(2)、2**10、factorial(10)
        """
        try:
            r = skills_mod.calculate(expression)
        except Exception as e:
            return (f"计算失败: {e}")
        return (f"{expression.strip()} = {r}")

    @filter.llm_tool(name="weather")
    async def tool_weather(self, event: AstrMessageEvent, city: str):
        """查询城市今明两天天气(温度/天气现象/降水概率)。主人问天气、降温、下不下雨、要不要带伞时用。

        Args:
            city(string): 城市名, 如 北京 / 上海 / 长沙
        """
        try:
            return await asyncio.to_thread(skills_mod.weather, city)
        except Exception as e:
            return (f"天气查询失败: {e}")

    # ---------- 技能: 背诵抽查 / 每日内容推送 ----------

    @filter.llm_tool(name="add_quiz")
    async def tool_add_quiz(self, event: AstrMessageEvent, items_json: str):
        """往题库里加要被抽查的内容(单词/知识点/概念问答)。主人说"记住这些单词, 之后考我"时用。

        Args:
            items_json(string): JSON数组, 每项 {"question":"问题或单词", "answer":"答案", "category":"分类(如 单词)"}
        """
        try:
            items = json.loads((items_json or "").strip())
        except Exception:
            return ("items_json 不是合法的 JSON 数组")
        if not isinstance(items, list) or not items:
            return ("items_json 必须是非空数组")
        n = 0
        with self._conn() as c:
            for it in items[:100]:
                if not isinstance(it, dict):
                    continue
                q = str(it.get("question") or "").strip()
                a = str(it.get("answer") or "").strip()
                if not q or not a:
                    continue
                c.execute("INSERT INTO quiz_bank (question, answer, category) VALUES (?,?,?)",
                          (q[:200], a[:500], str(it.get("category") or "")[:40]))
                n += 1
        total = self._conn().execute("SELECT COUNT(*) n FROM quiz_bank").fetchone()["n"]
        return (f"题库已加入 {n} 项, 目前共 {total} 项。主人说「考我」就开始抽查")

    @filter.llm_tool(name="quiz_me")
    async def tool_quiz_me(self, event: AstrMessageEvent, count: int = 3, category: str = ""):
        """从题库抽题考主人: 优先抽答错过的, 其次没考过的, 最后最久没考的。把返回的题目用口吻逐个问主人,
        先不要公布答案; 主人回答后用 record_quiz_result 记录对错, 再公布正确答案。

        Args:
            count(number): 抽几个, 默认 3
            category(string): 只抽某分类(如 单词), 留空=全库
        """
        sql = ("SELECT id, question, answer FROM quiz_bank "
               "WHERE (? = '' OR category = ?) "
               "ORDER BY no_count DESC, last_asked IS NULL DESC, last_asked ASC LIMIT ?")
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(sql, (category, category, max(1, min(10, int(count or 3))))).fetchall()]
        if not rows:
            return ("题库是空的 — 主人可以先说「记住这些单词」让我存一批")
        with self._conn() as c:
            for r in rows:
                c.execute("UPDATE quiz_bank SET last_asked=? WHERE id=?",
                          (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
        return json.dumps(rows, ensure_ascii=False)

    @filter.llm_tool(name="record_quiz_result")
    async def tool_record_quiz_result(self, event: AstrMessageEvent, results_json: str):
        """记录抽查结果(主人每答完一题, 你判对错后调这个)。

        Args:
            results_json(string): JSON数组, 每项 {"id":题目id, "correct":true或false}
        """
        try:
            items = json.loads((results_json or "").strip())
        except Exception:
            return ("results_json 不是合法的 JSON 数组")
        right = wrong = 0
        with self._conn() as c:
            for it in items if isinstance(items, list) else []:
                try:
                    tid = int(it.get("id"))
                except Exception:
                    continue
                if it.get("correct"):
                    c.execute("UPDATE quiz_bank SET ok_count=ok_count+1, no_count=0 WHERE id=?", (tid,))
                    right += 1
                else:
                    c.execute("UPDATE quiz_bank SET no_count=no_count+1 WHERE id=?", (tid,))
                    wrong += 1
        return (f"已记录: 答对 {right} · 答错 {wrong}。答错的题之后会优先再考")

    @filter.llm_tool(name="set_daily_content")
    async def tool_set_daily_content(self, event: AstrMessageEvent, enabled: str = "", topics: str = ""):
        """开启/关闭每日内容推送, 或修改感兴趣的话题。主人说"每天推点XX给我"或"别推了"时用。

        Args:
            enabled(string): "true"=开启, "false"=关闭, 留空=不变
            topics(string): 感兴趣的话题(逗号分隔, 如: AI, 考研资讯, 科技趣闻), 留空=不变
        """
        cfg = self._config
        g = cfg.get("content")
        g = dict(g) if isinstance(g, dict) else {}
        changed = []
        if enabled.strip():
            on = self._is_confirmed(enabled)
            g["content_push_enabled"] = on
            changed.append("推送已" + ("开启" if on else "关闭"))
        if topics.strip():
            g["content_topics"] = topics.strip()[:200]
            changed.append("话题已更新: " + topics.strip()[:100])
        if not changed:
            return ("没有变化。可以说: 每天推点AI和考研资讯 / 别推了")
        cfg["content"] = g
        try:
            self.save_config(cfg)
        except Exception as e:
            print("[astrbot_task] 保存内容推送配置失败:", e)
        return await self._voice_result("、".join(changed) + "。每天 " + str(int(self._cfg("content_push_hour", 21))) + " 点为你找", "内容推送设置")
