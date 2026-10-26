# astrbot_task 冒烟测试套件(v0.6.1 起固化入库)
# 跑法: python astrbot_task/tests_smoke.py   (纯标准库, 服务器上也能跑: docker exec astrbot python /AstrBot/data/plugins/astrbot_task/tests_smoke.py)
# 每次改插件代码后必须全绿再打包发版。新增功能时在对应 section 里补用例。
import os, sys, json, sqlite3, tempfile, types, asyncio, importlib.util, urllib.request, urllib.parse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---- stub astrbot 宿主(测试不依赖真实 AstrBot) ----
for m in ("astrbot", "astrbot.api", "astrbot.api.star", "astrbot.api.event", "astrbot.api.message_chain"):
    sys.modules.setdefault(m, types.ModuleType(m))
sys.modules["astrbot.api.star"].Context = object
sys.modules["astrbot.api.star"].Star = object
sys.modules["astrbot.api.star"].register = lambda *a, **k: (lambda cls: cls)
sys.modules["astrbot.api.event"].AstrMessageEvent = object
class _FM(type):
    def __getattr__(cls, n): return lambda *a, **k: (lambda f: f)
sys.modules["astrbot.api.event"].filter = _FM("F", (), {"EventMessageType": type("E", (), {"ALL": "all"})})

spec = importlib.util.spec_from_file_location("at_main", os.path.join(HERE, "main.py"))
at_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at_main)

import tasks_core
import scheduler as scheduler_mod
import memory as memory_mod
import webui

ok = fail = 0
def must(name, cond):
    global ok, fail
    print(("  PASS " if cond else "  FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)

def tmpconn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, due_date TEXT, due_time TEXT, end_time TEXT,
      priority INTEGER DEFAULT 3, recurring TEXT, recurrence_end TEXT, category TEXT DEFAULT '',
      status TEXT DEFAULT 'pending', created_at TEXT);
    CREATE TABLE task_occurrence_completions (task_id INTEGER NOT NULL, date TEXT NOT NULL, PRIMARY KEY (task_id, date));
    CREATE TABLE task_occurrence_overrides (task_id INTEGER NOT NULL, orig_date TEXT NOT NULL, new_date TEXT NOT NULL,
      due_time TEXT, end_time TEXT, PRIMARY KEY (task_id, orig_date));
    """)
    return conn

# ================= 1. 任务: 校验/去重/冲突 =================
print("[1] tasks_core 校验与冲突")
conn = tmpconn()
tid, err = tasks_core.create_task(conn, {"title": "高数课 8:00-9:40", "due_date": "2026-10-26",
    "due_time": "8:00", "end_time": "9:40", "recurring": "weekly", "recurrence_end": "2027-01-18", "category": "课程"})
must("建循环课(时间归一化)", tid and not err)
row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
must("8:00/9:40 归一化", row["due_time"] == "08:00" and row["end_time"] == "09:40")
tid2, err2 = tasks_core.create_task(conn, {"title": "英语晨读", "due_date": "2026-10-26", "due_time": "8:30", "recurring": "weekly"})
must("循环vs循环 同时段拒绝", tid2 is None and "时间冲突" in (err2 or ""))
tid3, err3 = tasks_core.create_task(conn, {"title": "临时测验", "due_date": "2026-11-02", "due_time": "8:30"})
must("单次落循环窗口拒绝", tid3 is None and "时间冲突" in (err3 or ""))
tid4, err4 = tasks_core.create_task(conn, {"title": "下学期晨跑", "due_date": "2027-02-01", "due_time": "8:30", "recurring": "weekly"})
must("学期窗口外不冲突", tid4 is not None and err4 is None)
tid5, err5 = tasks_core.create_task(conn, {"title": "英语课 10:00-11:40", "due_date": "2026-10-26", "due_time": "10:00", "recurring": "weekly", "recurrence_end": "2027-01-18"})
must("不同时段放行", tid5 is not None and err5 is None)
tid6, err6 = tasks_core.create_task(conn, {"title": "紧急事项", "due_date": "2026-11-02", "due_time": "8:30"}, force=True)
must("force 越权", tid6 is not None and err6 is None)
tid7, err7 = tasks_core.create_task(conn, {"title": "交材料", "due_date": "2026-11-02"})
must("无时间不判冲突", tid7 is not None and err7 is None)
tid8, err8 = tasks_core.update_task(conn, tid5, {"due_time": "8:30"})
must("update 改出冲突拒绝", tid8 is None and "时间冲突" in (err8 or ""))
tid9, err9 = tasks_core.update_task(conn, tid5, {"due_time": "10:30"}, force=True)
must("update force 放行", tid9 is not None and err9 is None)
tid10, err10 = tasks_core.create_task(conn, {"title": "讲座", "due_date": "2026-10-26", "due_time": "10:30"})
must("区间相交拒绝(标题区间)", tid10 is None and "时间冲突" in (err10 or ""))
tid11, err11 = tasks_core.create_task(conn, {"title": "高数课 8:00-9:40", "due_date": "2026-10-27"})
must("同名去重", tid11 is None and "同名" in (err11 or ""))
tid12, err12 = tasks_core.create_task(conn, {"title": "反向", "due_date": "2026-11-03", "due_time": "10:00", "end_time": "9:00"})
must("end<=start 拒绝", tid12 is None and "end_time" in (err12 or ""))
c = conn.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]
must("拒绝项未入库", c == 5)  # 高数课/晨跑/英语课/紧急事项(force)/交材料(无时间)
conn.close()

# ================= 2. 记忆: upsert/金环/总结存档 =================
print("[2] memory 存取与总结")
tmpdb = os.path.join(tempfile.mkdtemp(), "m.db")
canned = json.dumps({"summary": "和主人聊了考试计划", "facts": [
    {"content": "用户在备考一门重要考试", "importance": 5, "keywords": ["考试", "复习"]}]}, ensure_ascii=False)
async def fake_llm(prompt, system=None, **kw):
    return canned
ms = memory_mod.MemoryStore(tmpdb, llm_call=fake_llm)
# 对话落库: 3 条未总结
cx0 = sqlite3.connect(tmpdb)
for role, text in [("user", "用户: 备考期末"), ("ai", "助手: 好"), ("user", "用户: 嗯")]:
    cx0.execute("INSERT INTO chats (umo, role, text) VALUES ('u1', ?, ?)", (role, text))
cx0.commit(); cx0.close()
stored = ms.periodic_summary("u1", "用户: 备考期末\n助手: 好", "身份")
must("facts 回灌", len(stored) == 1)
cx = sqlite3.connect(tmpdb); cx.row_factory = sqlite3.Row
must("总结存档", cx.execute("SELECT COUNT(*) n FROM summaries").fetchone()["n"] == 1)
must("对话已标记总结", cx.execute("SELECT COUNT(*) n FROM chats WHERE summarized=0").fetchone()["n"] == 0
     and cx.execute("SELECT COUNT(*) n FROM chats WHERE summarized=1").fetchone()["n"] == 3)
mem = cx.execute("SELECT * FROM memories").fetchone()
must("记忆 importance=5", mem["importance"] == 5)
cx.execute("UPDATE memories SET access_count=4 WHERE id=?", (mem["id"],)); cx.commit()
got = ms.associative_recall("考试", limit=3)
row = cx.execute("SELECT access_count, pinned FROM memories WHERE id=?", (mem["id"],)).fetchone()
must("召回+金环晋升(满5)", row["access_count"] == 5 and row["pinned"] == 1)
r2 = ms.upsert_similar("用户在备考一门重要考试, 目标已定", "fact", 5)
must("双向前缀升级覆盖", r2 and r2.get("upgraded"))
cx.close()

# ================= 3. 配置: 迁移与兼容读取 =================
print("[3] config 迁移与 _cfg")
inst = at_main.TaskPlugin.__new__(at_main.TaskPlugin)
inst._config = {"owner_umo": "qq:old", "vision_api_key": "k1", "proactive_random_base": 0.5}
saved = {}
inst.save_config = lambda c: saved.update(n=1)
inst._migrate_config()
must("扁平折叠进组", isinstance(inst._config.get("push"), dict) and inst._config["push"]["owner_umo"] == "qq:old")
must("旧值保留", inst._cfg("vision_api_key") == "k1")
must("_cfg 默认值", inst._cfg("webui_port", 6187) == 6187)

# ================= 4. 意图跳过 =================
print("[4] _skip_intent")
must("寒暄跳过", at_main.TaskPlugin._skip_intent("嗯") and at_main.TaskPlugin._skip_intent("早"))
must("正常句子不跳过", not at_main.TaskPlugin._skip_intent("帮我看看今天有什么任务"))

# ================= 5. _llm_call 管理模型分支 =================
print("[5] _llm_call 三分支")
class R:
    completion_text = "ok"
def make_ctx(admin_ok=True):
    calls = []
    class Ctx:
        async def llm_generate(self, chat_provider_id=None, prompt=None, system_prompt=None):
            calls.append(chat_provider_id)
            if chat_provider_id == "mgr" and not admin_ok:
                raise RuntimeError("x")
            return R()
        async def get_current_chat_provider_id(self, umo=None):
            calls.append("chat")
            return "chat"
    return Ctx(), calls
ctx, calls = make_ctx()
i2 = at_main.TaskPlugin.__new__(at_main.TaskPlugin)
i2._config = {"admin_provider_id": "mgr"}; i2.context = ctx; i2._last_umo = "u"
i2.db_path = os.path.join(tempfile.mkdtemp(), "l.db")
i2._init_db()
asyncio.run(i2._llm_call("p", kind="summary"))
must("管理模型优先", calls == ["mgr"])
i2._config = {}
asyncio.run(i2._llm_call("p"))
must("未设置跟随聊天", calls[-2:] == ["chat", "chat"])
ctx2, calls2 = make_ctx(admin_ok=False)
i2.context = ctx2; i2._config = {"admin_provider_id": "mgr"}
asyncio.run(i2._llm_call("p"))
must("故障回退聊天", calls2[0] == "mgr" and calls2[-1] == "chat")

# ================= 6. WebUI HTTP: 导出/监控/总结 =================
print("[6] webui HTTP")
tmpweb = os.path.join(tempfile.mkdtemp(), "w.db")
cx = sqlite3.connect(tmpweb)
cx.executescript("""
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, memory_type TEXT DEFAULT 'fact',
  importance INTEGER DEFAULT 3, intensity REAL DEFAULT 0, valence REAL DEFAULT 0, keywords TEXT DEFAULT '[]',
  access_count INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, created_at TEXT, last_accessed_at TEXT);
CREATE TABLE summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT, summary TEXT, facts_json TEXT DEFAULT '[]', created_at TEXT);
CREATE TABLE llm_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT DEFAULT 'misc', provider TEXT DEFAULT '', model TEXT DEFAULT '',
  prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, duration_ms INTEGER, ok INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, due_date TEXT, due_time TEXT, end_time TEXT,
  priority INTEGER DEFAULT 3, recurring TEXT, recurrence_end TEXT, category TEXT DEFAULT '', status TEXT DEFAULT 'pending', created_at TEXT);
CREATE TABLE emotion (id INTEGER PRIMARY KEY CHECK (id=1), relation_json TEXT, state_json TEXT);
CREATE TABLE ai_reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, remind_at TEXT, sent INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE notify_log (key TEXT PRIMARY KEY, created_at TEXT);
CREATE TABLE proactive_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, created_at TEXT);
CREATE TABLE chats (id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT, role TEXT, text TEXT, summarized INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE session_counter (umo TEXT PRIMARY KEY, rounds INTEGER DEFAULT 0);
""")
cx.execute("INSERT INTO memories (content, importance, keywords, pinned) VALUES ('测试记忆', 5, '[\"测试\"]', 1)")
cx.execute("INSERT INTO summaries (umo, summary) VALUES ('u', '测试总结')")
cx.execute("INSERT INTO llm_usage (kind, provider, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, ok) VALUES ('chat','p','m',700,300,1000,500,1)")
cx.execute("INSERT INTO chats (umo, role, text) VALUES ('u', 'user', '你好呀')")
cx.commit(); cx.close()

webui.start(tmpweb, 0, "127.0.0.1")
port = webui._get_server().server_port
def get(path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)

r = get("/api/memories/export?format=json")
d = json.loads(r.read().decode("utf-8"))
must("导出 json", r.status == 200 and d["count"] == 1 and len(d["summaries"]) == 1)
r = get("/api/memories/export?format=csv")
must("导出 csv", "测试记忆" in r.read().decode("utf-8-sig"))
r = get("/api/memories/export?format=md")
must("导出 md", "★金环" in r.read().decode("utf-8"))
d = json.loads(get("/api/usage?days=7").read())
must("监控聚合", d["total"]["n"] == 1 and d["total"]["t"] == 1000 and len(d["heat"]) == 1)
d = json.loads(get("/api/summaries?limit=10").read())
must("总结列表", len(d) == 1 and d[0]["summary"] == "测试总结")
d = json.loads(get("/api/chats?limit=50").read())
must("对话接口: 未总结1条", d["pending_count"] == 1 and len(d["pending"]) == 1 and d["pending"][0]["text"] == "你好呀")

# 导入: 1条新 + 1条与已有重复(跳过) + 1条更完整(升级) + 1条总结去重
payload = {"memories": [
    {"content": "导入的新记忆内容测试", "memory_type": "fact", "importance": 4, "keywords": ["导入"]},
    {"content": "测试记忆", "memory_type": "fact", "importance": 3},
    {"content": "测试记忆, 目标院校与方向都已确定下来", "memory_type": "fact", "importance": 5}],
    "summaries": [{"summary": "测试总结", "created_at": "2026-10-27 00:00:00"},
                  {"summary": "新导入的总结", "created_at": "2026-10-27 01:00:00"}]}
req = urllib.request.Request(f"http://127.0.0.1:{port}/api/memories/import", method="POST",
    data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
res = json.loads(urllib.request.urlopen(req, timeout=5).read())
must("导入统计", res["created"] == 1 and res["skipped"] == 1 and res["upgraded"] == 1)
must("总结去重导入", res["summaries"] == 1)
d = json.loads(get("/api/memories?kw=" + urllib.parse.quote("导入")).read())
must("导入记忆可检索", len(d) == 1)

# PUT 编辑记忆 + POST 新增(之前是404的路径)
req = urllib.request.Request(f"http://127.0.0.1:{port}/api/memories/{d[0]['id']}", method="PUT",
    data=json.dumps({"content": "导入的记忆已修改, 补充了细节", "memory_type": "preference", "importance": 2}).encode("utf-8"),
    headers={"Content-Type": "application/json"})
must("PUT 编辑记忆", json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"])
d = json.loads(get("/api/memories?kw=" + urllib.parse.quote("修改")).read())
must("编辑后类型/重要度生效", len(d) == 1 and d[0]["memory_type"] == "preference" and d[0]["importance"] == 2)
req = urllib.request.Request(f"http://127.0.0.1:{port}/api/memories", method="POST",
    data=json.dumps({"content": "POST 新增的记忆", "memory_type": "fact", "importance": 3}).encode("utf-8"),
    headers={"Content-Type": "application/json"})
must("POST 新增记忆", json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"])
webui.stop()

# ================= 7. 调度纯函数 =================
print("[7] scheduler 纯函数")
must("silence 小时", scheduler_mod.silence_desc(datetime.datetime(2026,10,27,15,0), datetime.datetime(2026,10,27,10,0)) == "距主人上次发言已有 5 小时")
must("silence 天", scheduler_mod.silence_desc(datetime.datetime(2026,10,27,15,0), datetime.datetime(2026,10,24,15,0)) == "距主人上次发言已有 3.0 天")
must("silence 刚聊过", scheduler_mod.silence_desc(datetime.datetime(2026,10,27,15,0), datetime.datetime(2026,10,27,15,0)) == "")
# 临近提醒: 循环课展开为今日实例
conn7 = tmpconn()
tasks_core.create_task(conn7, {"title": "高数课 8:00-9:40", "due_date": "2026-09-14",
    "due_time": "8:00", "end_time": "9:40", "recurring": "weekly", "recurrence_end": "2027-01-18"})
up = scheduler_mod.upcoming_tasks(conn7, datetime.datetime(2026, 10, 26, 7, 50), 45)
must("临近提醒含循环实例", any(t[0] and "高数" in t[1] and t[2] == "08:00" for t in up))
up2 = scheduler_mod.upcoming_tasks(conn7, datetime.datetime(2026, 10, 26, 9, 10), 45)
must("窗口外不提醒", not any("高数" in t[1] for t in up2))
conn7.close()

# ================= 8. 快照与撤销 + 单次改期 =================
print("[8] snapshot/undo/move_occurrence")
conn8 = tmpconn()
snap_payload = tasks_core.snapshot_tasks(conn8)
tasks_core.create_task(conn8, {"title": "错课", "due_date": "2026-10-26", "due_time": "8:00", "recurring": "weekly"})
must("变更后 1 个任务", conn8.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"] == 1)
tasks_core.restore_tasks(conn8, snap_payload)
must("撤销恢复 0 个", conn8.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"] == 0)
tasks_core.restore_tasks(conn8, snap_payload)
occ = conn8.execute("SELECT COUNT(*) n FROM task_occurrence_completions").fetchone()["n"]
must("重复恢复幂等", occ == 0)
conn8.close()

# 单次改期: 周一循环课挪一次到周五晚, 其他周不动; 目标撞车拒绝
conn9 = tmpconn()
tasks_core.create_task(conn9, {"title": "高数课", "due_date": "2026-10-26",
    "due_time": "8:00", "end_time": "9:40", "recurring": "weekly", "recurrence_end": "2027-01-18"})
tasks_core.create_task(conn9, {"title": "网络安全", "due_date": "2026-10-26",
    "due_time": "20:10", "end_time": "21:40", "recurring": "weekly", "recurrence_end": "2027-01-18"})
_, err = tasks_core.move_occurrence(conn9, 1, "2026-10-26", "2026-11-02", "20:10", "21:40")
must("单次改期撞车拒绝", err and "冲突" in err)
text, err = tasks_core.move_occurrence(conn9, 1, "2026-10-26", "2026-10-30", "20:10", "21:40")
must("单次改期成功", text and not err)
rows = [dict(r) for r in conn9.execute("SELECT * FROM tasks WHERE status='pending'").fetchall()]
in_range = tasks_core.expand_range(conn9, rows, "2026-10-26", "2026-11-01")
mon = [t for t in in_range if t["id"] == 1 and t["due_date"] == "2026-10-26"]
fri = [t for t in in_range if t["id"] == 1 and t["due_date"] == "2026-10-30"]
must("本周一消失/周五出现", not mon and len(fri) == 1 and fri[0]["due_time"] == "20:10" and fri[0]["end_time"] == "21:40")
in_next = tasks_core.expand_range(conn9, rows, "2026-11-02", "2026-11-08")
mon_next = [t for t in in_next if t["id"] == 1 and t["due_date"] == "2026-11-02"]
must("下周一照旧", len(mon_next) == 1)
snap = json.loads(tasks_core.snapshot_tasks(conn9))
must("快照含改期记录", len(snap.get("ovr", [])) == 1)
conn9.close()

# ================= 9. DDL 完整性(内存库能建全部表) =================
print("[9] DDL")
i3 = at_main.TaskPlugin.__new__(at_main.TaskPlugin)
i3._config = {}
i3.db_path = os.path.join(tempfile.mkdtemp(), "d.db")
at_main.DB_PATH = i3.db_path  # _conn 经实例属性, 但模块常量兜底一致
i3._init_db()
memory_mod.MemoryStore(i3.db_path)  # 记忆侧表(summaries/session_counter)由 memory 迁移建
cx3 = sqlite3.connect(i3.db_path)
tables = {r[0] for r in cx3.execute("SELECT name FROM sqlite_master WHERE type='table'")}
must("核心表齐全", {"tasks", "memories", "emotion", "ai_reminders", "notify_log",
                   "task_occurrence_completions", "task_occurrence_overrides", "proactive_logs", "llm_usage", "summaries",
                   "session_counter", "task_snapshots"} <= tables)
cols = {r[1] for r in cx3.execute("PRAGMA table_info(tasks)")}
must("tasks 含 end_time", "end_time" in cols)
cx3.close()

print(f"\n== {ok} pass / {fail} fail ==")
sys.exit(1 if fail else 0)
