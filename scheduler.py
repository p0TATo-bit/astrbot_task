# astrbot_task 定时调度(v0.3, 移植自 Teaskks schedulerService):
# AI定时提醒(每分钟) / 临近开始提醒(窗口+去重) / 过期任务汇总(4h冷却) /
# 主动关心(30min检查+时段/上限/间隔/同主题每日一次) / 每日每周计划推送 / 数据库自动备份(凌晨3点+启动)
# 判定逻辑全部纯函数化(便于测试); 推送走注入的 send 回调(封装 AstrBot send_message)。

import os
import re
import random
import sqlite3
import datetime
import asyncio

import skills as skills_mod
import tasks_core

BACKUP_KEEP = 7
LOG_RETENTION_DAYS = 90


def silence_desc(now: datetime.datetime, last_interaction) -> str:
    """距主人上次发言的沉默时长描述(给 bot 的提示用); 没有记录返回空串"""
    if not last_interaction:
        return ""
    hrs = (now - last_interaction).total_seconds() / 3600
    if hrs < 1:
        return ""
    return f"距主人上次发言已有 {hrs/24:.1f} 天" if hrs >= 48 else f"距主人上次发言已有 {hrs:.0f} 小时"


def now_dt():
    return datetime.datetime.now()


def today_str() -> str:
    return datetime.date.today().isoformat()


# ---------- 纯判定函数 ----------

def due_reminders(conn, now: datetime.datetime):
    """到点的 AI 定时提醒"""
    rows = conn.execute("SELECT id, content, remind_at FROM ai_reminders WHERE sent = 0 AND remind_at <= ? LIMIT 5",
                        (now.strftime("%Y-%m-%d %H:%M"),)).fetchall()
    return [(r["id"], r["content"], r["remind_at"]) for r in rows]


def upcoming_tasks(conn, now: datetime.datetime, window_min: int = 45):
    """今天、有具体时间、window_min 分钟内开始、未完成的任务(循环任务展开为今日实例)"""
    today = now.strftime("%Y-%m-%d")
    now_hm = now.strftime("%H:%M")
    limit_hm = (now + datetime.timedelta(minutes=window_min)).strftime("%H:%M")
    rows = [dict(r) for r in conn.execute(
        "SELECT id, title, due_date, due_time, priority, recurring, recurrence_end FROM tasks WHERE status='pending'").fetchall()]
    expanded = tasks_core.expand_range(conn, rows, today, today)
    out = []
    for t in expanded:
        tm = t.get("due_time")
        if t.get("due_date") != today or not tm:
            continue
        if now_hm <= tm <= limit_hm:
            out.append((t["id"], t["title"], tm))
    return out


def overdue_tasks(conn, now: datetime.datetime):
    """逾期任务(截止日早于今天的 pending 非循环任务; 循环母任务不算——起始日语义永远在过去)"""
    today = now.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT id, title, due_date, due_time FROM tasks "
        "WHERE status='pending' AND (recurring IS NULL OR recurring='') AND due_date != '' AND due_date < ? "
        "ORDER BY due_date LIMIT 10",
        (today,),
    ).fetchall()
    return [(r["id"], r["title"], r["due_date"]) for r in rows]


def proactive_topic(conn, now, last_interaction: datetime.datetime | None, today_done: int,
                    window_start: int, window_end: int, max_per_day: int, gap_hours: float,
                    rand_p: float = 1.0):
    """主动关心: 返回 topic 或 None(所有防打扰条件在此判定)。
    防打扰(时段/每日上限/最小间隔)通过后再掷骰: 实际概率 rand_p 随三条好感提升。"""
    hour = now.hour
    if not (window_start <= hour <= window_end):
        return None
    # 每日上限/最小间隔
    day = now.strftime("%Y-%m-%d")
    sent_today = conn.execute(
        "SELECT COUNT(*) n FROM proactive_logs WHERE substr(created_at,1,10)=?", (day,)).fetchone()["n"]
    if sent_today >= max_per_day:
        return None
    last = conn.execute("SELECT MAX(created_at) t FROM proactive_logs").fetchone()["t"]
    if last:
        last_dt = datetime.datetime.fromisoformat(last)
        if (now - last_dt).total_seconds() < gap_hours * 3600:
            return None
    # 随机触发: 好感越高概率越高(基础概率在配置里)
    if random.random() > max(0.0, min(1.0, rand_p)):
        return None
    # 同主题当日一次
    sent_of = lambda topic: conn.execute(
        "SELECT COUNT(*) n FROM proactive_logs WHERE substr(created_at,1,10)=? AND topic=?",
        (day, topic)).fetchone()["n"]
    overdue = overdue_tasks(conn, now)
    if len(overdue) >= 3 and sent_of("overdue") == 0:
        return ("overdue", f"主人有 {len(overdue)} 项逾期任务还没处理: " + "、".join(t[1] for t in overdue[:3]))
    if hour >= 19 and today_done >= 2 and sent_of("praise") == 0:
        return ("praise", f"主人今天完成了 {today_done} 项任务, 值得夸一夸")
    if sent_of("greeting") == 0:
        if last_interaction and (now - last_interaction).total_seconds() > 48 * 3600:
            return ("greeting", "超过两天没和主人说话了, 问候一下")
    return None


# ---------- 调度循环 ----------

class Scheduler:
    def __init__(self, plugin):
        self.plugin = plugin
        self.task = None
        self._tick_lock = asyncio.Lock()
        self._last_overdue_hour = None
        self._last_proactive = None
        self._last_content_hour = None
        self._boot_backed_up = False

    def _conn(self):
        conn = sqlite3.connect(self.plugin.db_path)
        conn.row_factory = sqlite3.Row  # 判定函数全用 r["列名"] 取值, 缺这个会让 tick 每轮崩溃
        return conn

    def start(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中的事件循环(如测试环境): 不启动循环
            print("[astrbot_task] 调度循环未启动(无事件循环)")
            return
        self.task = asyncio.create_task(self._loop())
        self._boot_backup()

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _loop(self):
        # 启动 30 秒后先跑一轮(等服务就绪), 之后每分钟
        await asyncio.sleep(30)
        while True:
            try:
                async with self._tick_lock:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("[astrbot_task] 调度 tick 失败:", e)
            await asyncio.sleep(60)

    async def tick(self):
        p = self.plugin
        if not p._cfg("push_enabled", True):
            return
        now = now_dt()
        with self._conn() as c:
            # 1) AI 定时提醒
            for rid, content, at in due_reminders(c, now):
                text = await p.persona_notify("定时提醒", f"提醒内容: {content}", f"⏰ 你设置的提醒\n{content}")
                await p.push(text)
                c.execute("UPDATE ai_reminders SET sent = 1 WHERE id = ?", (rid,))
                print("[astrbot_task] 定时提醒已发送:", content[:40])

            # 2) 临近开始提醒
            window_min = int(p._cfg("upcoming_minutes", 45))
            for tid, title, hm in upcoming_tasks(c, now, window_min):
                key = f"upcoming|{tid}|{today_str()}|{hm}"
                if self._notified(c, key):
                    continue
                left = ""
                try:
                    dt = datetime.datetime.strptime(f"{today_str()} {hm}", "%Y-%m-%d %H:%M")
                    mins = int((dt - now).total_seconds() // 60)
                    left = f"还有 {mins} 分钟" if mins > 0 else "现在"
                except Exception:
                    pass
                text = await p.persona_notify("临近开始提醒", f"{title} 将于 {hm} 开始({left})",
                                              f"⏰ {title} 将于 {hm} 开始{('(' + left + ')') if left else ''}")
                await p.push(text)
                self._mark(c, key)

            # 3) 过期任务汇总(每小时一次, 4小时冷却)
            if self._last_overdue_hour != (now.strftime("%Y%m%d%H")):
                self._last_overdue_hour = now.strftime("%Y%m%d%H")
                od = overdue_tasks(c, now)
                if od:
                    day4 = now.strftime("%Y%m%d") + str(now.hour // 4)
                    if not self._notified(c, f"overdue|{day4}"):
                        facts = "；".join(f"{t[1]}(截止 {t[2]})" for t in od[:5])
                        text = await p.persona_notify("过期任务提醒",
                                                      f"主人共有 {len(od)} 项逾期任务: {facts}",
                                                      f"⏰ 有 {len(od)} 项逾期任务\n" + "\n".join(t[1] for t in od[:5]))
                        await p.push(text)
                        self._mark(c, f"overdue|{day4}")

            # 4) 主动关心(每30分钟检查一次; 通过防打扰条件后按概率随机触发, 好感越高概率越高)
            if p._cfg("proactive_enabled", True):
                slot = now.strftime("%Y%m%d%H") + ("a" if now.minute < 30 else "b")
                if self._last_proactive != slot:
                    self._last_proactive = slot
                    _, rel = p.emotion.get()
                    rel_sum = rel.get("trust", 0) + rel.get("social_liking", 0) + rel.get("task_liking", 0)
                    base = float(p._cfg("proactive_random_base", 0.3))
                    rand_p = base * (1 + min(2.0, rel_sum) / 2.0)
                    topic = proactive_topic(
                        c, now,
                        last_interaction=p._last_interaction_at,
                        today_done=c.execute(
                            "SELECT COUNT(*) n FROM tasks WHERE status='completed' AND due_date=?",
                            (today_str(),)).fetchone()["n"],
                        window_start=int(p._cfg("proactive_window_start", 10)),
                        window_end=int(p._cfg("proactive_window_end", 21)),
                        max_per_day=int(p._cfg("proactive_max_per_day", 2)),
                        gap_hours=float(p._cfg("proactive_gap_hours", 4)),
                        rand_p=rand_p,
                    )
                    if topic:
                        tname, hint = topic
                        silence = silence_desc(now, p._last_interaction_at)
                        facts = hint + ((";" + silence) if silence else "")
                        text = await p.persona_notify("主动关心", facts,
                                                      {"overdue": "有逾期任务啦, 记得处理哦",
                                                       "praise": "今天完成得不错, 值得表扬",
                                                       "greeting": "好久没聊了, 想你了"}.get(tname, ""))
                        await p.push(text)
                        c.execute("INSERT INTO proactive_logs (topic) VALUES (?)", (tname,))

            # 5) 计划推送: 每日 08:00 / 周一 09:00
            hm = now.strftime("%H:%M")
            if hm == "08:00":
                await self._push_plan(c, now, "daily", f"plan|daily|{today_str()}")
            if hm == "09:00" and now.weekday() == 0:
                await self._push_plan(c, now, "weekly", f"plan|weekly|{today_str()}")

            # 6) 备份 + 过期日志清理: 每天 03:00
            if now.strftime("%H:%M") == "03:00":
                self._backup()
                cutoff = (now - datetime.timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                for tbl in ("notify_log", "emotion_log", "proactive_logs", "llm_usage", "chats"):
                    c.execute(f"DELETE FROM {tbl} WHERE created_at < ?", (cutoff,))

            # 7) 每日有意思内容推送: 按话题搜索 → 口吻播报
            if p._cfg("content_push_enabled", True):
                chour = int(p._cfg("content_push_hour", 21))
                if now.hour == chour and self._last_content_hour != now.strftime("%Y%m%d%H"):
                    self._last_content_hour = now.strftime("%Y%m%d%H")
                    key = f"content|{today_str()}"
                    if not self._notified(c, key):
                        topics = [t.strip() for t in str(p._cfg("content_topics", "科技趣闻, 有趣的新闻"))
                                  .replace("，", ",").split(",") if t.strip()][:3]

                        def gather():
                            digest = []
                            for t in topics:
                                try:
                                    res = skills_mod.web_search(t, 3)
                                    digest.append("【" + t + "】" + "；".join(
                                        r["title"] + "(" + r["url"] + ")" for r in res[:2]))
                                except Exception as te:
                                    digest.append("【" + t + "】搜索失败")
                            return digest

                        digest = await asyncio.to_thread(gather)
                        text = await p.persona_notify("每日内容推送", "\n".join(digest),
                                                      "今天网上也有点有意思的东西, 不过我这边搜索抽风了, 明天再给你补上")
                        await p.push(text)
                        self._mark(c, key)

    async def _push_plan(self, c, now, kind, key):
        if self._notified(c, key):
            return
        p = self.plugin
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE status='pending'").fetchall()]
        # 循环任务展开成本期实例(母任务原始 due_date 是首次发生日, 直接列会显示成过期)
        start = now.date()
        end = start if kind == "daily" else start + datetime.timedelta(days=6)
        items = tasks_core.expand_range(c, rows, start.isoformat(), end.isoformat())
        items.sort(key=lambda t: (t.get("due_date") or "", t.get("due_time") or ""))
        items = items[:40]
        if not items:
            return

        def fmt(t):
            done = " 已打卡" if t.get("occurrence_done") else ""
            recur = " [循环]" if t.get("is_occurrence") else ""
            return f"- {t.get('due_date', '')[5:]} {t.get('due_time') or ''} {t['title']} P{t.get('priority', 3)}{recur}{done}"

        listing = "\n".join(fmt(t) for t in items)
        scope = "今天" if kind == "daily" else f"本周({start.isoformat()[5:]}~{end.isoformat()[5:]})"
        try:
            text = await p._llm_call(
                f"根据以下任务实例, 生成{scope}的行动计划(简洁分条, 按时间排序, 已打卡的略过不提):\n{listing}",
                "你是任务规划助手。直接输出计划内容, 不要输出其他解释。",
                kind="plan")
        except Exception as e:
            print("[astrbot_task] 计划生成失败:", e)
            return
        await p.push(f"【{scope}计划】\n{text}")
        self._mark(c, key)

    def _boot_backup(self):
        if not self._boot_backed_up:
            self._backup()
            self._boot_backed_up = True

    def _backup(self):
        try:
            p = self.plugin
            bdir = os.path.join(_dir_of(p.db_path), "backups")
            os.makedirs(bdir, exist_ok=True)
            day = now_dt().strftime("%Y%m%d-%H%M%S")  # 带时间戳: 同日多次备份不互相覆盖
            dest = os.path.join(bdir, f"data-{day}.db")
            # sqlite backup API: 拷贝的是一致性问题快照, 直接 copyfile 可能拷到写一半的页
            src = sqlite3.connect(p.db_path)
            dst = sqlite3.connect(dest)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            files = sorted(f for f in os.listdir(bdir) if f.startswith("data-") and f.endswith(".db"))
            for f in files[:-BACKUP_KEEP]:
                os.remove(os.path.join(bdir, f))
            print("[astrbot_task] 数据库备份完成:", dest)
        except Exception as e:
            print("[astrbot_task] 备份失败:", e)

    def _notified(self, c, key):
        return c.execute("SELECT COUNT(*) n FROM notify_log WHERE key=?", (key,)).fetchone()["n"] > 0

    def _mark(self, c, key):
        c.execute("INSERT OR IGNORE INTO notify_log (key) VALUES (?)", (key,))


def _dir_of(path):
    return os.path.dirname(os.path.abspath(path))
