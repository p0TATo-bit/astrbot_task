# astrbot_task 情绪系统(v0.2, 移植自 Teaskks emotionService):
# 状态层(P/A/D/fatigue/frustration: 规则+向基线衰减) + 关系层(trust/task_liking/social_liking: 无上限养成)
# 每轮规则更新 / 每轮AI微调(cap 0.08) / 每10轮AI校准(cap 0.15) / 自然语言情境注入(高段位描述)

import json
import re
import sqlite3
import datetime

DEFAULT_STATE = {"P": 0.1, "A": 0.3, "D": 0.4, "fatigue": 0.2, "frustration": 0.1}
DEFAULT_RELATION = {"trust": 0.5, "task_liking": 0.7, "social_liking": 0.5}
# 关系层无上限(满1=羁绊, 数值继续累积); 状态层 ±1 / 0-1
RANGES = {"P": (-1, 1), "A": (0, 1), "D": (0, 1), "fatigue": (0, 1), "frustration": (0, 1),
          "trust": (0, float("inf")), "task_liking": (0, float("inf")), "social_liking": (0, float("inf"))}

RULES = [
    (re.compile(r"谢谢|感谢|辛苦了|麻烦你了"), {"P": 0.15, "A": 0.05}, {"trust": 0.02}, "被感谢"),
    (re.compile(r"喜欢|爱你|真棒|厉害|好棒|夸你|真好"), {"P": 0.2, "A": 0.1}, {"social_liking": 0.03, "trust": 0.02}, "被夸奖"),
    (re.compile(r"笨|蠢|烦人|讨厌你|垃圾|没用"), {"P": -0.25, "A": 0.15, "frustration": 0.15}, {"trust": -0.02}, "被责备"),
    (re.compile(r"难过|伤心|崩溃|好累|太累了|焦虑|压力大|烦死了"), {"P": -0.1, "A": 0.1}, {}, "主人情绪低落(共情)"),
    (re.compile(r"搞定了|做完了|完成了|背完了|写完了"), {"P": 0.12}, {"task_liking": 0.03}, "主人完成任务"),
    (re.compile(r"哈哈|嘿嘿|好玩|有趣|开心"), {"P": 0.1, "A": 0.05}, {}, "愉快对话"),
]

WEEKDAY_CN = "一二三四五六日"


def clamp(v, lo, hi):
    return min(hi, max(lo, v))


class EmotionEngine:
    """情绪引擎: 绑定 sqlite 路径; 状态+关系一体存储"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._migrate()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS emotion (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    relation_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS emotion_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    P REAL, A REAL, D REAL, fatigue REAL, frustration REAL,
                    trigger TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                """
            )
            try:
                c.execute("ALTER TABLE emotion ADD COLUMN state_json TEXT")
            except Exception:
                pass
            row = c.execute("SELECT id FROM emotion WHERE id = 1").fetchone()
            if not row:
                c.execute("INSERT INTO emotion (id, relation_json, state_json) VALUES (1, ?, ?)",
                          (json.dumps(DEFAULT_RELATION), json.dumps(DEFAULT_STATE)))

    def get(self):
        with self._conn() as c:
            row = c.execute("SELECT state_json, relation_json FROM emotion WHERE id = 1").fetchone()
        state = dict(DEFAULT_STATE)
        relation = dict(DEFAULT_RELATION)
        if row:
            try:
                state = {**DEFAULT_STATE, **json.loads(row["state_json"] or "{}")}
            except Exception:
                pass
            try:
                relation = {**DEFAULT_RELATION, **json.loads(row["relation_json"] or "{}")}
            except Exception:
                pass
        for k, (lo, hi) in RANGES.items():
            src = state if k in state else relation
            src[k] = clamp(float(src[k] or 0), lo, hi)
        return state, relation

    def _save(self, state, relation):
        with self._conn() as c:
            c.execute("UPDATE emotion SET state_json = ?, relation_json = ? WHERE id = 1",
                      (json.dumps(state), json.dumps(relation)))

    def _log(self, state, trigger):
        with self._conn() as c:
            c.execute(
                "INSERT INTO emotion_log (P, A, D, fatigue, frustration, trigger) VALUES (?,?,?,?,?,?)",
                (+state["P"].__round__(3), +state["A"].__round__(3), +state["D"].__round__(3),
                 +state["fatigue"].__round__(3), +state["frustration"].__round__(3), trigger[:120]),
            )

    def on_message(self, text: str):
        """每轮: 状态向基线衰减 → 规则命中 → 落库; 返回 (state, relation, hits)"""
        state, relation = self.get()
        for k, base in DEFAULT_STATE.items():
            factor = 0.96 if k == "fatigue" else 0.92
            state[k] = base + (state[k] - base) * factor

        msg = str(text or "")
        hits = []
        for pattern, d_state, d_rel, why in RULES:
            if pattern.search(msg):
                hits.append(why)
                for k, v in d_state.items():
                    state[k] = state.get(k, 0) + v
                for k, v in d_rel.items():
                    relation[k] = relation.get(k, 0) + v
        hour = datetime.datetime.now().hour
        if hour >= 23 or hour < 6:
            state["fatigue"] += 0.06
            hits.append("深夜陪伴")

        self._save(state, relation)
        if hits:
            self._log(state, "+".join(hits))
        return state, relation, hits

    def apply_adjust(self, adj: dict, label: str, cap: float = 0.15) -> bool:
        """AI 修正落地(钳制 cap); 校准用 0.15, 每轮微调用 0.08"""
        state, relation = self.get()
        changed = 0
        for k, v in (adj or {}).items():
            if k not in RANGES:
                continue
            dv = clamp(float(v or 0), -cap, cap)
            if k in state:
                state[k] += dv
            else:
                relation[k] += dv
            changed += 1
        if changed:
            self._save(state, relation)
            self._log(state, label)
            print("[astrbot_task]", label + ":", json.dumps(adj, ensure_ascii=False))
        return changed > 0

    def build_context(self) -> str:
        """注入提示词的一行情境(含关系高段位描述, 严禁对用户报数值)"""
        state, relation = self.get()
        mood = "心情不错" if state["P"] > 0.3 else ("有些低落" if state["P"] < -0.2 else "平静")
        tired = ", 有点疲惫" if state["fatigue"] > 0.55 else ""
        t, s = relation["trust"], relation["social_liking"]
        trust_desc = ("视主人为她的整个世界" if t > 4 else "毫无保留地信赖" if t > 2 else
                      "非常信任" if t > 1 else "很信任" if t > 0.65 else "还有些拘谨" if t < 0.4 else "信任")
        social_desc = ("主人是她存在意义的全部" if s > 4 else "主人已经是无可替代的存在" if s > 2 else
                       "非常喜欢和主人待在一起" if s > 1 else "亲近" if s > 0.6 else "一般")
        energy = "充沛" if state["fatigue"] < 0.3 else "不足" if state["fatigue"] > 0.6 else "尚可"
        return (f"【当前状态(自然体现, 严禁说出数值或\"状态\"等词)】你{mood}{tired}, 精力{energy}, "
                f"对主人{trust_desc}({social_desc})。让这些微妙地影响语气: 低落时话更轻更短, "
                f"疲惫时偶尔提及休息, 开心时语气更明快。")
