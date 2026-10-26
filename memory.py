# astrbot_task 记忆系统(v0.2, 移植自 Teaskks memoryService):
# 启发式关键词 / 扩散激活召回(IDF+多跳+情绪一致性) / 同主题升级覆盖 / 情感标注 / AI精修 / 自动提取 / 周期总结
# 所有 LLM 调用通过注入的 llm_call 回调(走 AstrBot provider), 本模块不直接依赖网络。

import json
import re
import sqlite3
import threading
import datetime
import asyncio

STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "我们", "你们", "他们", "这个", "那个", "什么",
    "没有", "就是", "还是", "可以", "一个", "自己", "现在", "时候", "因为", "所以", "但是", "如果",
    "用户", "助手", "回复", "内容", "信息", "系统", "觉得", "知道", "感觉", "已经", "这样", "那样",
    "一下", "一些", "一点", "今天", "明天", "昨天", "然后", "不过", "其实", "可能", "应该", "需要",
}

DB_LOCK = threading.Lock()


def today_str() -> str:
    return datetime.date.today().isoformat()


def now_local() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 情感估算(关键词法, 与 Teaskks estimateAffect 同语义) ----------

def estimate_affect(text: str):
    s = str(text or "")
    intensity, valence = 0.0, 0.0
    if re.search(r"喜欢|爱你|开心|高兴|棒|厉害|谢谢", s):
        intensity, valence = 0.6, 0.7
    if re.search(r"难过|伤心|哭|崩溃|痛苦", s):
        intensity, valence = 0.8, -0.7
    if re.search(r"生气|讨厌|烦死|愤怒|垃圾", s):
        intensity, valence = 0.8, -0.8
    if re.search(r"焦虑|压力|紧张|害怕|担心", s):
        intensity, valence = max(intensity, 0.5), -0.4
    if re.search(r"考研|考试|比赛|面试|目标|梦想", s):
        intensity = max(intensity, 0.4)
    if intensity == 0 and len(s) > 80:
        intensity = 0.15
    return {"intensity": round(intensity, 2), "valence": round(valence, 2)}


# ---------- 启发式关键词(中文 2-4 字词频扫描, 无分词依赖) ----------

def heuristic_keywords(text: str, top: int = 6):
    s = str(text or "")
    segments = re.findall(r"[\u4e00-\u9fa5]{2,}", s)
    freq = {}
    for seg in segments:
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                w = seg[i:i + n]
                if w in STOPWORDS or re.search(r"[的了吗呢吧，。]", w):
                    continue
                freq[w] = freq.get(w, 0) + 1
    # 高频优先, 同频短词优先(更像主题词)
    words = sorted(freq.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    picked = []
    for w, _ in words:
        if any(w in p or p in w for p in picked):  # 去包含
            continue
        picked.append(w)
        if len(picked) >= top:
            break
    return picked


def sanitize_keywords(kws):
    out = []
    for k in kws or []:
        k = str(k).strip().lower()
        if len(k) < 2 or len(k) > 8 or k in STOPWORDS:
            continue
        if re.search(r"[的了吗呢吧，。]", k) or re.search(r"用户|助手|回复|内容|信息", k):
            continue
        out.append(k)
    return out[:8]


def parse_keywords(raw) -> list:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


class MemoryStore:
    """记忆系统: 绑定一个 sqlite 路径; LLM 调用由 llm_call 回调注入(async (prompt, system) -> str)。"""

    def __init__(self, db_path: str, llm_call=None, identity_prompt_fn=None):
        self.db_path = db_path
        self.llm_call = llm_call or (lambda prompt, system=None, **kw: _async_noop())
        self.identity_prompt_fn = identity_prompt_fn or (lambda: "")
        self._migrate()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    memory_type TEXT DEFAULT 'fact',
                    importance INTEGER DEFAULT 3,
                    intensity REAL DEFAULT 0,
                    valence REAL DEFAULT 0,
                    keywords TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    access_count INTEGER DEFAULT 0,
                    last_accessed_at TEXT,
                    pinned INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    facts_json TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                CREATE TABLE IF NOT EXISTS session_counter (
                    umo TEXT PRIMARY KEY,
                    rounds INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    summarized INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                """
            )
            for col in ("access_count INTEGER DEFAULT 0", "last_accessed_at TEXT", "pinned INTEGER DEFAULT 0"):
                try:
                    c.execute(f"ALTER TABLE memories ADD COLUMN {col}")
                except Exception:
                    pass

    # ---------- 存储 ----------

    def store(self, content, memory_type="fact", importance=3, intensity=0.0, valence=0.0, keywords=None):
        content = str(content or "").strip()
        if len(content) < 4:
            return None
        with self._conn() as c:
            dup = c.execute("SELECT id FROM memories WHERE content LIKE ?", (content[:30] + "%",)).fetchone()
            if dup:
                return {"id": dup["id"], "skipped": True}
            kws = keywords or heuristic_keywords(content)
            c.execute(
                "INSERT INTO memories (content, memory_type, importance, intensity, valence, keywords) VALUES (?,?,?,?,?,?)",
                (content, memory_type if memory_type in ("fact", "preference", "task_context", "conversation", "analysis") else "fact",
                 max(1, min(5, int(importance or 3))), float(intensity or 0), float(valence or 0),
                 json.dumps(kws[:8], ensure_ascii=False)),
            )
            return {"id": c.execute("SELECT last_insert_rowid() id").fetchone()["id"], "created": True}

    def upsert_similar(self, content, memory_type="fact", importance=3, intensity=0.0, valence=0.0, keywords=None, prefix_len=30):
        """双向前缀匹配: 命中且新内容更长则升级覆盖(旧模糊版自动变完整), 否则跳过; 无命中新建"""
        content = str(content or "").strip()
        if len(content) < 4:
            return None
        with self._conn() as c:
            dup = c.execute(
                "SELECT id, LENGTH(content) AS len FROM memories WHERE content LIKE ? OR ? LIKE content || '%' LIMIT 1",
                (content[:prefix_len] + "%", content),
            ).fetchone()
            if dup:
                if len(content) > dup["len"]:
                    c.execute(
                        "UPDATE memories SET content=?, importance=?, intensity=?, valence=? WHERE id=?",
                        (content, max(1, min(5, int(importance or 3))), float(intensity or 0), float(valence or 0), dup["id"]),
                    )
                    return {"id": dup["id"], "upgraded": True}
                return {"id": dup["id"], "skipped": True}
        return self.store(content, memory_type, importance, intensity, valence, keywords)

    # ---------- 扩散激活召回 ----------

    def associative_recall(self, query: str, mood: float = 0.0, limit: int = 5, max_hops: int = 2):
        q = str(query or "").strip()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM memories ORDER BY last_accessed_at DESC LIMIT 500"
            ).fetchall()
        if not rows or not q:
            return []
        mems = []
        for r in rows:
            d = dict(r)
            d["kws"] = parse_keywords(d.get("keywords"))
            if not d["kws"]:
                d["kws"] = heuristic_keywords(d["content"], 6)
            mems.append(d)

        # IDF
        df = {}
        for m in mems:
            for k in set(m["kws"]):
                df[k] = df.get(k, 0) + 1
        kw_weight = lambda k: 1 + __import__("math").log(len(mems) / max(1, df.get(k, 1)))

        # 种子: 记忆关键词出现在查询里; 兜底: 查询整体命中记忆正文(无AI精修时保证短查询可召回)
        activated = {}   # id -> (activation, hops, via)
        frontier = []
        for m in mems:
            score = sum(kw_weight(k) for k in m["kws"] if k and k in q)
            if score == 0 and len(q) >= 2 and q in m["content"]:
                score = 1.0
            if score > 0:
                activated[m["id"]] = (score, 0, [])
                frontier.append((m["id"], score, 0))

        # 多跳扩散(hop 之间 ×0.5)
        by_kw = {}
        for m in mems:
            for k in m["kws"]:
                by_kw.setdefault(k, []).append(m["id"])
        hop = 0
        while frontier and hop < max_hops:
            nxt = []
            for mid, score, h in frontier:
                m = next(x for x in mems if x["id"] == mid)
                for k in m["kws"]:
                    for nid in by_kw.get(k, []):
                        if nid == mid or nid in activated:
                            continue
                        na = score * 0.5
                        if na > 0.1:
                            activated[nid] = (na, h + 1, [k])
                            nxt.append((nid, na, h + 1))
            frontier = nxt
            hop += 1

        # 情绪一致性: 低落时同向记忆×1.2, 反向×0.85
        results = []
        for m in mems:
            if m["id"] not in activated:
                continue
            a, h, via = activated[m["id"]]
            val = m.get("valence") or 0
            if mood < -0.2 and val:
                a *= 1.2 if val < 0 else 0.85
            results.append({**m, "activation": round(a, 3), "hops": h, "via": via,
                            "association": "related" if h > 0 else "direct"})
        results.sort(key=lambda x: -x["activation"])
        top = results[:limit]

        # 再巩固: 被召回的记忆访问+1; 访问满 5 次晋升金环 pinned(逐出保护+召回加成)
        if top:
            with self._conn() as c:
                for m in top:
                    c.execute(
                        "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ?, "
                        "pinned = CASE WHEN access_count + 1 >= 5 THEN 1 ELSE pinned END WHERE id = ?",
                        (now_local(), m["id"]))
        return top

    def context_text(self, recalled) -> str:
        if not recalled:
            return ""
        lines = []
        for m in recalled:
            tag = f"联想({','.join(m['via'])})" if m["association"] == "related" else "直接相关"
            lines.append(f"[{m['memory_type']}|{tag}] {str(m['content'])[:200]}")
        return "[相关记忆]\n" + "\n".join(lines)

    # ---------- 关联图(v0.4: 有效权重 + IDF-Jaccard 边) ----------

    def effective_importance(self, m) -> float:
        """有效权重 = 存储重要度 × 时间衰减(45天半衰期, 下限0.25) × 访问加成(每次+5%上限+50%) × 情绪加成(1+强度×0.5)"""
        base = m.get("importance") or 3
        decay = 0.25
        la = m.get("last_accessed_at") or m.get("created_at")
        if la:
            try:
                dt = datetime.datetime.fromisoformat(str(la).replace("T", " ").replace("Z", ""))
                days = max(0.0, (datetime.datetime.now() - dt).total_seconds() / 86400)
                decay = max(0.25, 0.5 ** (days / 45))
            except Exception:
                pass
        access = min(1.5, 1 + (m.get("access_count") or 0) * 0.05)
        emo = 1 + (m.get("intensity") or 0) * 0.5
        return round(base * decay * access * emo, 2)

    def build_graph(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM memories ORDER BY last_accessed_at DESC LIMIT 200").fetchall()
        mems = []
        for r in rows:
            d = dict(r)
            d["kws"] = parse_keywords(d.get("keywords"))
            if not d["kws"]:
                d["kws"] = heuristic_keywords(d["content"], 6)
            mems.append(d)
        if not mems:
            return {"nodes": [], "edges": []}

        df = {}
        for m in mems:
            for k in set(m["kws"]):
                df[k] = df.get(k, 0) + 1
        import math
        kw_weight = lambda k: 1 + math.log(len(mems) / max(1, df.get(k, 1)))

        nodes = [{
            "id": m["id"],
            "label": re.sub(r"\s+", " ", str(m["content"]))[:20],
            "content": m["content"],
            "type": m["memory_type"],
            "importance": m["importance"],
            "eff": self.effective_importance(m),
            "keywords": m["kws"],
            "access_count": m.get("access_count") or 0,
            "intensity": m.get("intensity") or 0,
            "valence": m.get("valence") or 0,
            "pinned": m.get("pinned") or 0,
            "source": m.get("source", "") if "source" in m else "",
            "created_at": m.get("created_at", ""),
        } for m in mems]

        edges = []
        for i in range(len(mems)):
            for j in range(i + 1, len(mems)):
                ka, kb = mems[i]["kws"], mems[j]["kws"]
                shared = [k for k in kb if k in ka]
                if not shared:
                    continue
                union = set(ka) | set(kb)
                w = sum(kw_weight(k) for k in shared) / sum(kw_weight(k) for k in union)
                if w < 0.08:
                    continue
                edges.append({"a": mems[i]["id"], "b": mems[j]["id"], "w": round(w, 2), "shared": shared[:4]})
        return {"nodes": nodes, "edges": edges}

    # ---------- 同主题升级 + AI 精修 + 自动提取 + 周期总结(需要 LLM) ----------

    def bump_round(self, umo: str) -> int:
        with self._conn() as c:
            c.execute("INSERT INTO session_counter (umo, rounds) VALUES (?, 1) ON CONFLICT(umo) DO UPDATE SET rounds = rounds + 1", (umo,))
            return c.execute("SELECT rounds FROM session_counter WHERE umo = ?", (umo,)).fetchone()["rounds"]

    def enrich_keywords(self, mid: int, content: str):
        """后台 AI 精修关键词(阻塞, 由调用方放线程执行): 覆盖启发式结果"""
        async def _run():
            return await self.llm_call(
                f"为下面的内容提取3-6个用于联想检索的关键词(名词/主题词, 避免虚词和\u0022用户/助手\u0022)。只输出JSON数组, 如 [\"考研\",\"高数\"]:\n\n{str(content)[:600]}",
                kind="keywords")

        try:
            text = asyncio.run(_run())
            m = re.search(r"\[[\s\S]*\]", text or "")
            if not m:
                return
            kws = sanitize_keywords(json.loads(m[0]))
            if kws:
                with self._conn() as c:
                    c.execute("UPDATE memories SET keywords = ? WHERE id = ?", (json.dumps(kws, ensure_ascii=False), mid))
                print("[astrbot_task] AI关键词精修:", "、".join(kws))
        except Exception as e:
            print("[astrbot_task] 关键词精修失败(保留启发式):", e)

    def auto_extract(self, umo, user_message, ai_response, recent_context=""):
        """后台自动提取(仅 importance>=4): 由调用方放异步任务"""
        text = str(user_message or "").strip()
        if len(text) < 4 or re.match(r"^(确认|取消|好的|嗯|在吗|你好|哈哈|嘿嘿)", text):
            return []
        with self._conn() as c:
            rows = c.execute("SELECT content FROM memories ORDER BY created_at DESC LIMIT 20").fetchall()
        existing = "\n".join("- " + r["content"].replace("\n", " ")[:80] for r in rows) or "(无)"

        prompt = f"""分析用户与助手的对话, 提取值得长期记住的「用户个人信息」(目标/计划/偏好/习惯/重要事实)。没有就输出 []。

必须排除: 助手自己说的话或发现的问题、关于系统本身的讨论(用户是在使用系统不是开发系统)、系统能力说明、一次性事件与当天场景、寒暄与测试内容。

已有记忆(不要重复存, 同主题只在新信息更完整时输出):
{existing}

只输出JSON数组: [{{"content":"第一人称陈述(称呼与语气遵循身份设定)", "type":"fact|preference|task_context", "importance":1到5, "keywords":["3-6个关键词"]}}]
只提取 importance>=4 的重要信息; content 保留具体细节(时间/数量/名称/原因)。"""

        user_prompt = (f"近期对话(截尾):\n{str(recent_context)[-500:]}\n\n" if recent_context else "") + \
            f"用户: {text[:800]}\n助手: {str(ai_response or '')[:600]}"

        return self._parse_and_store(prompt, user_prompt, affect_source=text, identity=self.identity_prompt_fn())

    def periodic_summary(self, umo, rounds_text, identity):
        """周期总结: rounds_text 为本周期对话拼接文本; 总结存档 + 关键词 + 事实回灌"""
        prompt = f"""以助手的第一人称视角总结这段对话, 输出JSON: {{"summary":"250字内总结(第一人称。写清楚: 聊了哪些主题、主人说了什么重点、我做了什么回应或建议、有什么结论或待办)","keywords":["3-6个关键词"],"facts":[{{"content":"值得长期记住的用户信息(第一人称+保留细节)","importance":1到5,"keywords":["2-4个关键词"]}}]}}。facts 最多5条, 没有给空数组。只输出JSON。

facts 严禁记成: 助手自己说的话或发现的问题、关于系统本身的讨论、系统能力说明、某天某时刻的一次性场景。"""
        return self._parse_and_store(prompt, str(rounds_text)[:8000], is_summary=True, identity=identity, umo=umo)

    def _parse_and_store(self, system_prompt, user_prompt, affect_source="", is_summary=False, identity="", umo=""):
        """公共: 调 LLM → 解析 JSON → 回灌记忆(同步阻塞, 由调用方放线程/任务)"""
        sys_full = system_prompt + (("\n\n" + identity) if identity else "")

        async def _run():
            return await self.llm_call(user_prompt, sys_full,
                                       kind="summary" if is_summary else "extract")

        try:
            text = asyncio.run(_run())
        except Exception as e:
            print("[astrbot_task] 记忆 LLM 调用失败:", e)
            return []

        m = re.search(r"\{[\s\S]*\}", text or "") if is_summary else re.search(r"\[[\s\S]*\]", text or "")
        if not m:
            return []
        try:
            parsed = json.loads(m[0])
        except Exception:
            return []

        stored = []
        if is_summary:
            # 总结文本存档( summaries 表, WebUI「对话」页可见 ), facts 回灌记忆
            summary_text = str(parsed.get("summary") or "").strip()
            if summary_text and umo:
                with self._conn() as c:
                    c.execute("INSERT INTO summaries (umo, summary, facts_json) VALUES (?,?,?)",
                              (umo, summary_text, json.dumps(parsed.get("facts") or [], ensure_ascii=False)))
                    # teaskks 式标记: 本会话未总结对话打上已总结标记(不删除)
                    try:
                        c.execute("UPDATE chats SET summarized=1 WHERE umo=? AND summarized=0", (umo,))
                    except Exception:
                        pass
                print("[astrbot_task] 周期总结已存档:", summary_text[:40], "...")
            facts = parsed.get("facts") or []
            for f in facts[:5]:
                content = str(f.get("content") or "").strip()
                if len(content) < 4:
                    continue
                affect = estimate_affect(content)
                r = self.upsert_similar(content, "fact", f.get("importance", 3),
                                        affect["intensity"], affect["valence"],
                                        f.get("keywords"))
                if r and not r.get("skipped"):
                    stored.append(content[:40] + ("(升级)" if r.get("upgraded") else ""))
            return stored

        for it in (parsed if isinstance(parsed, list) else [])[:3]:
            content = str(it.get("content") or "").strip()
            if len(content) < 4:
                continue
            affect = estimate_affect(affect_source + " " + content)
            r = self.upsert_similar(content, it.get("type", "fact"), it.get("importance", 3),
                                    affect["intensity"], affect["valence"],
                                    sanitize_keywords(it.get("keywords")) or None)
            if r and not r.get("skipped"):
                stored.append(content[:40] + ("(升级)" if r.get("upgraded") else ""))
                if isinstance(it.get("keywords"), list) and it["keywords"]:
                    with self._conn() as c:
                        c.execute("UPDATE memories SET keywords=? WHERE id=?", (json.dumps(it["keywords"][:6], ensure_ascii=False), r["id"]))
        if stored:
            print("[astrbot_task] 自动提取:", " | ".join(stored))
        return stored


def affect_none():
    return {"intensity": 0.0, "valence": 0.0}


async def _async_noop(prompt, system=None, **kw):
    return ""
