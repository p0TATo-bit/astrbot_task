# astrbot_task 内嵌 WebUI(v0.5 完全体): teaskks UI 与功能 1:1 复刻
# 页面: 仪表盘(周视图+今日重点) / 日历(月视图) / 任务(分组折叠+筛选) / 记忆(列表+关联图+导出) / 对话(未总结标记+存档) / 情绪 / 监控(调用+token)
# 纯标准库; 默认绑定 127.0.0.1(仅本机); 想局域网手机访问改成 0.0.0.0(注意安全)

import json
import sqlite3
import threading
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tasks_core
import memory as memory_mod
import skills as skills_mod
import builtins
import sys

from memory import MemoryStore

_state = {"db_path": None, "port": 6187, "memory": None}

# 服务对象挂进程级注册表(builtins): 插件热重载时模块会被删除重建,
# 模块内全局变量随新模块重置——旧服务线程必须能被 terminate 跨模块找到并停掉,
# 否则残留僵尸监听(端口被占但不响应), 新实例也永远绑定失败。
_REG_KEY = "_astrbot_task_webui_server"


def _get_server():
    return getattr(builtins, _REG_KEY, None)


def _set_server(s):
    setattr(builtins, _REG_KEY, s)


def _conn():
    conn = sqlite3.connect(_state["db_path"])
    conn.row_factory = sqlite3.Row
    return conn


def _memory() -> MemoryStore:
    if not _state["memory"]:
        _state["memory"] = MemoryStore(_state["db_path"])
    return _state["memory"]


def _emotion_data():
    with _conn() as c:
        row = c.execute("SELECT state_json, relation_json FROM emotion WHERE id = 1").fetchone()
    state = {"P": 0.1, "A": 0.3, "D": 0.4, "fatigue": 0.2, "frustration": 0.1}
    relation = {"trust": 0.5, "task_liking": 0.7, "social_liking": 0.5}
    if row:
        try:
            state.update(json.loads(row["state_json"] or "{}"))
        except Exception:
            pass
        try:
            relation.update(json.loads(row["relation_json"] or "{}"))
        except Exception:
            pass
    return {**state, **relation}


def _parse_end(t) -> str:
    """结束时间: 从标题/描述解析 '8:30-10:00' 的后半段(任务无独立结束时间字段)"""
    import re as _re
    m = _re.search(r"(\d{1,2}:\d{2})\s*[-–~至]\s*(\d{1,2}:\d{2})", str(t.get("title", "")) + " " + str(t.get("description", "")))
    return m.group(2) if m else ""


def _week_data(start: str, days: int = 7):
    end = (datetime.date.fromisoformat(start) + datetime.timedelta(days=max(1, days) - 1)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status != 'cancelled' AND "
            "(due_date BETWEEN ? AND ? OR (recurring IS NOT NULL AND recurring != '' AND status='pending')) "
            "ORDER BY due_date, due_time", (start, end)).fetchall()
        rows = [dict(r) for r in rows]
        in_range = tasks_core.expand_range(c, rows, start, end)
    return in_range


def _calendar_data(year: int, month: int):
    first = datetime.date(year, month, 1)
    last = datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status != 'cancelled' AND "
            "(due_date BETWEEN ? AND ? OR (recurring IS NOT NULL AND recurring != '' AND status='pending'))",
            (first.isoformat(), last.isoformat())).fetchall()
        rows = [dict(r) for r in rows]
        in_range = tasks_core.expand_range(c, rows, first.isoformat(), last.isoformat())
    return in_range


class Handler(BaseHTTPRequestHandler):
    # ---- 防护包装: 任何异常返回 500 JSON, 不裸断连 ----
    def do_GET(self):
        try:
            return self._get()
        except Exception as e:
            return self._safe(e)

    def do_POST(self):
        try:
            return self._post()
        except Exception as e:
            return self._safe(e)

    def do_PUT(self):
        try:
            return self._put()
        except Exception as e:
            return self._safe(e)

    def do_DELETE(self):
        try:
            return self._delete()
        except Exception as e:
            return self._safe(e)

    def _safe(self, e):
        print("[astrbot_task] WebUI 请求处理失败:", e)
        try:
            return self._json({"error": str(e)}, 500)
        except Exception:
            pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, body: bytes, filename: str, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _export_memories(self):
        """导出全部记忆: json(全字段+总结) / csv(Excel, 带BOM) / md(人类可读笔记)"""
        fmt = "json"
        if "format=csv" in self.path:
            fmt = "csv"
        elif "format=md" in self.path:
            fmt = "md"
        day = datetime.date.today().strftime("%Y%m%d")
        with _conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, content, memory_type, importance, intensity, valence, keywords, "
                "access_count, pinned, created_at, last_accessed_at FROM memories ORDER BY id").fetchall()]
            sums = [dict(r) for r in c.execute(
                "SELECT created_at, summary FROM summaries ORDER BY id").fetchall()]
        for r in rows:
            try:
                r["keywords"] = json.loads(r.get("keywords") or "[]")
            except Exception:
                r["keywords"] = []

        if fmt == "json":
            payload = {"exported_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "count": len(rows), "memories": rows, "summaries": sums}
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            return self._download(body, f"memories_{day}.json", "application/json; charset=utf-8")

        if fmt == "csv":
            import csv
            import io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "内容", "类型", "重要度", "强度", "极性", "关键词", "被想起次数", "金环", "创建时间", "最近想起"])
            for r in rows:
                w.writerow([r["id"], r["content"], r["memory_type"], r["importance"], r["intensity"],
                            r["valence"], "、".join(r["keywords"]), r.get("access_count") or 0,
                            "是" if r.get("pinned") else "", r.get("created_at") or "",
                            r.get("last_accessed_at") or ""])
            body = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")  # BOM: Excel 直接打开不乱码
            return self._download(body, f"memories_{day}.csv", "text/csv; charset=utf-8")

        lines = [f"# 记忆导出({datetime.date.today().isoformat()})", "",
                 f"共 {len(rows)} 条" + (f",另有对话总结 {len(sums)} 份。" if sums else "。"), ""]
        for r in rows:
            pin = " ★金环" if r.get("pinned") else ""
            lines.append(f"## [{r['memory_type']}|重要度{r['importance']}] {r['content']}{pin}")
            kws = "、".join(r["keywords"])
            if kws:
                lines.append(f"- 关键词:{kws}")
            lines.append(f"- 创建:{r.get('created_at') or '—'} · 被想起 {r.get('access_count') or 0} 次"
                         f" · 强度 {r.get('intensity')} / 极性 {r.get('valence')}")
            lines.append("")
        if sums:
            lines.append("---")
            lines.append("# 对话总结")
            lines.append("")
            for s in sums:
                lines.append(f"## {s.get('created_at') or ''}")
                lines.append(str(s.get("summary") or "").strip())
                lines.append("")
        body = "\n".join(lines).encode("utf-8")
        return self._download(body, f"memories_{day}.md", "text/markdown; charset=utf-8")

    def _html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE.encode("utf-8"))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    # ---- GET ----
    def _get(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._html()
        if self.path.startswith("/api/week"):
            start = self.path.split("start=")[-1].split("&")[0] if "start=" in self.path else datetime.date.today().isoformat()
            start = start[:10]
            days = 7
            try:
                days = max(1, min(31, int(self.path.split("days=")[1].split("&")[0])))
            except Exception:
                pass
            return self._json(_week_data(start, days))
        if self.path.startswith("/api/calendar"):
            qs = dict(p.split("=") for p in self.path.split("?")[-1].split("&") if "=" in p)
            now = datetime.date.today()
            year = int(qs.get("year", now.year))
            month = int(qs.get("month", now.month))
            return self._json(_calendar_data(year, month))
        if self.path.startswith("/api/tasks"):
            qs = dict(p.split("=") for p in self.path.split("?")[-1].split("&") if "=" in p) if "?" in self.path else {}
            sql = "SELECT * FROM tasks WHERE 1=1"
            params = []
            if qs.get("status"):
                sql += " AND status = ?"
                params.append(qs["status"])
            if qs.get("type"):
                sql += " AND (CASE WHEN recurring IS NOT NULL AND recurring != '' THEN 'course' ELSE 'task' END) = ?"
                params.append(qs["type"])
            if qs.get("search"):
                sql += " AND title LIKE ?"
                params.append(f"%{qs['search']}%")
            sql += " ORDER BY due_date IS NULL, due_date, due_time, priority LIMIT 300"
            with _conn() as c:
                rows = c.execute(sql, params).fetchall()
            return self._json([dict(r) for r in rows])
        if self.path.startswith("/api/memories/export"):
            return self._export_memories()
        if self.path.startswith("/api/memories"):
            kw = self.path.split("kw=")[-1].split("&")[0] if "kw=" in self.path else ""
            import urllib.parse
            kw = urllib.parse.unquote(kw)
            with _conn() as c:
                rows = c.execute(
                    "SELECT id, content, memory_type, importance, intensity, valence, keywords, access_count, created_at FROM memories "
                    "WHERE content LIKE ? OR keywords LIKE ? ORDER BY importance DESC, created_at DESC LIMIT 50",
                    (f"%{kw}%", f"%{kw}%"),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["keywords"] = json.loads(d["keywords"] or "[]")
                out.append(d)
            return self._json(out)
        if self.path == "/api/emotion":
            return self._json(_emotion_data())
        if self.path.startswith("/api/emotion_log"):
            with _conn() as c:
                rows = c.execute("SELECT created_at, trigger FROM emotion_log ORDER BY id DESC LIMIT 6").fetchall()
            return self._json([dict(r) for r in rows])
        if self.path.startswith("/api/notes"):
            import urllib.parse
            qs = dict(p.split("=", 1) for p in self.path.split("?")[-1].split("&") if "=" in p)
            action = "list"
            if "/api/notes/get" in self.path: action = "get"
            if "/api/notes/download" in self.path: action = "download"
            title = urllib.parse.unquote(qs.get("title", ""))
            if action == "list":
                return self._json(skills_mod.list_notes(_state["db_path"]))
            try:
                if action == "get":
                    return self._json({"title": title, "content": skills_mod.read_note(_state["db_path"], title)})
                content = skills_mod.read_note(_state["db_path"], title)
            except Exception as e:
                return self._json({"error": str(e)}, 404)
            body = content.encode("utf-8")
            day = datetime.date.today().strftime("%Y%m%d")
            import urllib.parse as _up
            fn = f"{skills_mod.safe_title(title)}_{day}.md"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"note_{day}.md\"; filename*=UTF-8''{_up.quote(fn)}")
            self.end_headers()
            self.wfile.write(body)
        if self.path.startswith("/api/chats"):
            limit = 80
            try:
                limit = max(10, min(400, int(self.path.split("limit=")[1].split("&")[0])))
            except Exception:
                pass
            with _conn() as c:
                pend = [dict(r) for r in c.execute(
                    "SELECT id, role, text, created_at FROM chats WHERE summarized=0 "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
                cnt = c.execute("SELECT COUNT(*) n FROM chats WHERE summarized=0").fetchone()["n"]
                rd = c.execute("SELECT MAX(rounds) r FROM session_counter").fetchone()["r"]
            pend.reverse()  # 时间正序展示
            return self._json({"pending_count": cnt, "rounds": rd or 0, "pending": pend})
        if self.path.startswith("/api/summaries"):
            limit = 30
            try:
                limit = max(1, min(100, int(self.path.split("limit=")[1].split("&")[0])))
            except Exception:
                pass
            with _conn() as c:
                rows = c.execute(
                    "SELECT id, umo, summary, facts_json, created_at FROM summaries ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            return self._json([dict(r) for r in rows])
        if self.path.startswith("/api/usage"):
            days = 7
            try:
                days = max(1, min(90, int(self.path.split("days=")[1].split("&")[0])))
            except Exception:
                pass
            since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            since90 = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
            with _conn() as c:
                tot = dict(c.execute(
                    "SELECT COUNT(*) n, SUM(total_tokens) t FROM llm_usage WHERE created_at >= ?", (since,)).fetchone())
                today = dict(c.execute(
                    "SELECT COUNT(*) n, SUM(total_tokens) t FROM llm_usage WHERE substr(created_at,1,10)=date('now','localtime')").fetchone())
                by_kind = [dict(r) for r in c.execute(
                    "SELECT kind, COUNT(*) n, SUM(total_tokens) t, SUM(prompt_tokens) p, SUM(completion_tokens) c "
                    "FROM llm_usage WHERE created_at >= ? GROUP BY kind ORDER BY t DESC",
                    (since,)).fetchall()]
                by_day = [dict(r) for r in c.execute(
                    "SELECT substr(created_at,1,10) d, COUNT(*) n, SUM(total_tokens) t FROM llm_usage "
                    "WHERE created_at >= ? GROUP BY d ORDER BY d", (since,)).fetchall()]
                heat = [dict(r) for r in c.execute(
                    "SELECT substr(created_at,1,10) d, COUNT(*) n, SUM(total_tokens) t FROM llm_usage "
                    "WHERE created_at >= ? GROUP BY d ORDER BY d", (since90,)).fetchall()]
                recent = [dict(r) for r in c.execute(
                    "SELECT kind, provider, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, ok, created_at "
                    "FROM llm_usage ORDER BY id DESC LIMIT 30").fetchall()]
            return self._json({"days": days, "total": tot, "today": today, "by_kind": by_kind,
                               "by_day": by_day, "heat": heat, "recent": recent})
        if self.path == "/api/memory/graph":
            return self._json(_memory().build_graph())
        return self._json({"error": "not found"}, 404)

    # ---- POST ----
    def _post(self):
        if self.path == "/api/notes/save":
            data = self._body()
            try:
                skills_mod.write_note(_state["db_path"], str(data.get("title") or ""), str(data.get("content") or ""))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if self.path == "/api/tasks":
            data = self._body()
            title = str(data.get("title") or "").strip()
            if not title:
                return self._json({"error": "title 必填"}, 400)
            recurring = str(data.get("recurring") or "")
            if recurring not in ("daily", "weekly", "monthly", "yearly"):
                recurring = None
            with _conn() as c:
                c.execute(
                    "INSERT INTO tasks (title, due_date, due_time, end_time, priority, recurring, recurrence_end, category) VALUES (?,?,?,?,?,?,?,?)",
                    (title, str(data.get("due_date") or ""), str(data.get("due_time") or ""), str(data.get("end_time") or ""),
                     max(1, min(5, int(data.get("priority") or 3))), recurring,
                     str(data.get("recurrence_end") or "") or None,
                     str(data.get("category") or "").strip()[:40]),
                )
            return self._json({"ok": True})
        if self.path == "/api/memories":
            data = self._body()
            content = str(data.get("content") or "").strip()
            if len(content) < 4:
                return self._json({"error": "内容太短(至少4字)"}, 400)
            mtype = data.get("memory_type") if data.get("memory_type") in ("fact", "preference", "task_context") else "fact"
            imp = max(1, min(5, int(data.get("importance") or 3)))
            affect = memory_mod.estimate_affect(content)
            with _conn() as c:
                c.execute(
                    "INSERT INTO memories (content, memory_type, importance, intensity, valence, keywords) VALUES (?,?,?,?,?,?)",
                    (content, mtype, imp, affect["intensity"], affect["valence"],
                     json.dumps(memory_mod.heuristic_keywords(content), ensure_ascii=False)))
            return self._json({"ok": True})
        if self.path == "/api/memories/import":
            data = self._body()
            items = data.get("memories") if isinstance(data, dict) else data
            if not isinstance(items, list):
                return self._json({"error": "memories 必须是数组"}, 400)
            m = _memory()
            created = upgraded = skipped = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                content = str(it.get("content") or "").strip()
                if len(content) < 4:
                    continue
                kws = it.get("keywords")
                if isinstance(kws, str):
                    try:
                        kws = json.loads(kws)
                    except Exception:
                        kws = None
                r = m.upsert_similar(
                    content,
                    it.get("memory_type") if it.get("memory_type") in ("fact", "preference", "task_context") else "fact",
                    it.get("importance") or 3, it.get("intensity") or 0.0, it.get("valence") or 0.0, kws)
                if not r:
                    continue
                if r.get("upgraded"):
                    upgraded += 1
                elif r.get("skipped"):
                    skipped += 1
                else:
                    created += 1
            nsum = 0
            if isinstance(data, dict):
                for s in data.get("summaries") or []:
                    text = str(s.get("summary") or "").strip()
                    if not text:
                        continue
                    with _conn() as c:
                        dup = c.execute("SELECT 1 FROM summaries WHERE summary=?", (text,)).fetchone()
                        if not dup:
                            c.execute("INSERT INTO summaries (umo, summary, created_at) VALUES (?,?,?)",
                                      ("import", text, str(s.get("created_at") or "") or None))
                            nsum += 1
            return self._json({"ok": True, "created": created, "upgraded": upgraded,
                               "skipped": skipped, "summaries": nsum})
        return self._json({"error": "not found"}, 404)

    # ---- PUT ----
    def _put(self):
        if self.path.startswith("/api/memories/"):
            mid = int(self.path[len("/api/memories/"):])
            data = self._body()
            content = str(data.get("content") or "").strip()
            if len(content) < 4:
                return self._json({"error": "内容太短(至少4字)"}, 400)
            mtype = data.get("memory_type") if data.get("memory_type") in ("fact", "preference", "task_context") else "fact"
            imp = max(1, min(5, int(data.get("importance") or 3)))
            with _conn() as c:
                c.execute(
                    "UPDATE memories SET content=?, memory_type=?, importance=?, keywords=? WHERE id=?",
                    (content, mtype, imp,
                     json.dumps(memory_mod.heuristic_keywords(content), ensure_ascii=False), mid))
            return self._json({"ok": True})
        if self.path.endswith("/complete") and self.path.startswith("/api/tasks/"):
            tid = self.path[len("/api/tasks/"):-len("/complete")]
            with _conn() as c:
                c.execute("UPDATE tasks SET status='completed' WHERE id=?", (int(tid),))
            return self._json({"ok": True})
        if self.path.endswith("/occurrence") and self.path.startswith("/api/tasks/"):
            tid = int(self.path[len("/api/tasks/"):-len("/occurrence")])
            data = self._body()
            tasks_core.toggle_occurrence(_conn(), tid, str(data.get("date")), bool(data.get("completed")))
            return self._json({"ok": True})
        if self.path.startswith("/api/tasks/"):
            tid = int(self.path[len("/api/tasks/"):])
            data = self._body()
            fields, params = [], []
            for k in ("title", "due_date", "due_time", "end_time", "priority", "recurring", "recurrence_end", "status", "category"):
                if k in data:
                    fields.append(f"{k} = ?")
                    v = data[k]
                    if k == "recurring" and v not in ("daily", "weekly", "monthly", "yearly"):
                        v = None
                    if k == "priority":
                        v = max(1, min(5, int(v or 3)))
                    params.append(v)
            if fields:
                with _conn() as c:
                    c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", (*params, tid))
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    # ---- DELETE ----
    def _delete(self):
        if self.path.startswith("/api/notes/del"):
            import urllib.parse
            qs = dict(p.split("=", 1) for p in self.path.split("?")[-1].split("&") if "=" in p)
            try:
                skills_mod.delete_note(_state["db_path"], urllib.parse.unquote(qs.get("title", "")))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if self.path.startswith("/api/tasks/"):
            with _conn() as c:
                c.execute("DELETE FROM tasks WHERE id=?", (int(self.path[len("/api/tasks/"):]),))
            return self._json({"ok": True})
        if self.path.startswith("/api/memories/"):
            with _conn() as c:
                c.execute("DELETE FROM memories WHERE id=?", (int(self.path[len("/api/memories/"):]),))
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>astrbot_task</title>
<style>
:root {
  --primary: #5B8C8F; --primary-dark: #4A7C7F; --success: #7BA89C; --warning: #D4A574;
  --danger: #C67B7B; --bg: #F5F7F8; --card: #FFFFFF; --text: #2C3E50;
  --text-light: #7B9FA8; --border: #B8CFD3; --accent: #8BA8B5;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans SC', sans-serif; background:var(--bg); color:var(--text); line-height:1.6; }
#app { display:flex; min-height:100vh; }
.sidebar { width:190px; background:var(--card); border-right:1px solid var(--border); padding:18px 10px; flex-shrink:0; }
.brand { font-size:16px; font-weight:800; color:var(--primary); padding:0 10px 14px; border-bottom:1px solid var(--border); margin-bottom:10px; }
.brand small { display:block; font-size:10.5px; font-weight:400; color:var(--text-light); }
.nav-item { padding:9px 12px; border-radius:8px; cursor:pointer; font-size:14px; margin-bottom:2px; }
.nav-item:hover { background:var(--bg); }
.nav-item.on { background:rgba(91,140,143,0.10); color:var(--primary); font-weight:700; }
.main { flex:1; padding:18px 22px; max-width:1200px; margin:0 auto; }
h2 { font-size:16px; margin:0 0 10px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:14px; }
.meta { color:var(--text-light); font-size:12px; }
.btn { padding:5px 12px; border:1px solid var(--border); background:var(--card); border-radius:7px; cursor:pointer; font-size:12.5px; }
.btn.primary { background:var(--primary); color:#fff; border-color:var(--primary); }
.btn.danger { color:var(--danger); border-color:var(--danger); }
input, select, textarea { padding:6px 8px; border:1px solid var(--border); border-radius:6px; font-size:13px; background:var(--card); color:var(--text); font-family:inherit; }
.tag { display:inline-block; padding:1px 8px; border-radius:9px; font-size:11px; background:var(--bg); color:var(--text-light); border:1px solid var(--border); }
.tag.course { background:#e05a5a22; color:#e05a5a; border-color:#e05a5a55; }
.row { display:flex; align-items:center; gap:10px; padding:8px 4px; border-bottom:1px dashed var(--border); font-size:13.5px; }
.row:last-child { border-bottom:none; }
.bar { flex:1; height:8px; background:var(--bg); border-radius:4px; overflow:hidden; }
.fill { height:100%; background:var(--primary); border-radius:4px; }
.fill.gold { background:linear-gradient(90deg,var(--primary),#c9971c); }
.bar.rel { position:relative; }
.rel-ramp { position:absolute; inset:0; background:linear-gradient(90deg,#7BA89C,#D3A445); }
.rel-cover { position:absolute; top:0; bottom:0; right:0; background:var(--bg); }
.rel-fill { height:100%; border-radius:4px; }
.val { width:46px; text-align:right; font-size:12px; }
.empty { color:var(--text-light); text-align:center; padding:18px 0; font-size:13px; }
.fold { border:1px solid var(--border); border-radius:10px; background:var(--bg); margin-bottom:10px; }
.fold-h { display:flex; align-items:center; justify-content:space-between; padding:9px 12px; cursor:pointer; }
.fold-b { padding:0 10px 8px; }
.arrow { transition:transform .15s; color:var(--text-light); }
.fold.on .arrow { transform:rotate(90deg); }
.sec { font-weight:700; font-size:12.5px; color:var(--text-light); margin:10px 0 4px; }
.done { color:var(--text-light); text-decoration:line-through; }
.next-big { font-size:16px; font-weight:800; }
.next-when { color:var(--primary); font-weight:700; font-size:13px; }
/* 周视图 */
.week-grid { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:8px; }
.week-col { min-width:0; }
.week-col { background:transparent; border-radius:8px; padding:8px; min-height:120px; }
.week-col.today { background:#eef1f2; }
.week-band-h { font-weight:700; font-size:13.5px; margin:16px 0 8px; }
.week-band-h:first-child { margin-top:0; }
.week-col-header { text-align:center; margin-bottom:8px; }
.week-col-header .wd { font-weight:700; font-size:13px; }
.wk-card { background:var(--card); border:1px solid var(--border); border-left:3px solid var(--primary); border-radius:7px; padding:6px 8px; margin-bottom:6px; font-size:12px; cursor:pointer; }
.wk-card .t { font-weight:700; }
.wk-card .tm { color:var(--primary); font-weight:800; font-size:11.5px; }
.wk-card .meta2 { color:var(--text-light); font-size:10.5px; display:flex; justify-content:space-between; }
/* 日历 */
.cal-grid { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:6px; }
.cal-cell { background:var(--bg); border-radius:8px; min-height:78px; padding:5px; cursor:pointer; border:1px solid transparent; min-width:0; overflow:hidden; }
.cal-cell:hover { border-color:var(--primary); }
.cal-cell.dim { opacity:0.45; }
.cal-cell.today-cell { border-color:var(--primary); }
.cal-day { font-size:12px; font-weight:700; }
.cal-ev { font-size:10.5px; color:var(--primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
/* 弹窗 */
.modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:100; align-items:center; justify-content:center; }
.modal.on { display:flex; }
.modal-c { background:var(--card); border-radius:12px; padding:20px; width:min(560px, 92vw); max-height:86vh; overflow-y:auto; position:relative; }
.modal-x { position:absolute; top:10px; right:12px; border:none; background:none; font-size:22px; cursor:pointer; color:var(--text-light); }
.form-g { margin-bottom:10px; } .form-g label { font-size:12px; color:var(--text-light); display:block; }
.form-g input, .form-g select, .form-g textarea { width:100%; }
@media (max-width:800px) { #app { flex-direction:column; } .sidebar { width:100%; border-right:none; border-bottom:1px solid var(--border); } .nav { display:flex; gap:6px; flex-wrap:wrap; } .week-grid { grid-template-columns:repeat(2,1fr); } }
.tbl { width:100%; font-size:13px; border-collapse:collapse; }
.tbl th { text-align:left; font-weight:400; color:var(--text-light); font-size:12px; padding:5px 8px; border-bottom:1px solid var(--border); }
.tbl td { padding:5px 8px; border-bottom:1px solid var(--border); }
.tbl tr:last-child td { border-bottom:none; }
.u-stats { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
.u-stat { background:var(--bg); border-radius:10px; padding:12px; }
.u-stat .v { font-size:22px; font-weight:700; }
.u-stat .k { font-size:12px; color:var(--text-light); margin-top:2px; }
.tabular { font-variant-numeric: tabular-nums; }
.u-cols { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
@media (max-width:800px) { .u-cols { grid-template-columns:1fr; } }
.donut-flex { display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
.lg { display:grid; grid-template-columns:1fr 1fr; gap:5px 16px; flex:1; min-width:200px; }
.lg-row { display:flex; align-items:center; gap:6px; font-size:12.5px; }
.lg-chip { width:10px; height:10px; border-radius:3px; flex-shrink:0; }
.lg-pct { margin-left:auto; font-weight:700; }
.hm-row { display:flex; gap:3px; margin-bottom:3px; }
.hm-cell { width:13px; height:13px; border-radius:3px; background:var(--bg); }
.hm-none { background:transparent; }
.hm-l1 { background:#dfe9e8; } .hm-l2 { background:#bcd6d4; } .hm-l3 { background:#8dbcb8; } .hm-l4 { background:#5B8C8F; }
.hm-lab { width:34px; font-size:10px; color:var(--text-light); align-self:center; flex-shrink:0; }
.wk-card.ev { border:none; color:#fff; }
.wk-card.ev .t, .wk-card.ev .tm { color:#fff; }
.wk-card.ev .meta2 { color:rgba(255,255,255,.85); }
.wk-card.ev.done-card { opacity:.55; }
.ev-time { font-size:11px; color:var(--text-light); margin:8px 0 2px; }
.ev-check { position:absolute; top:8px; right:8px; width:16px; height:16px; border:2px solid rgba(255,255,255,.85); border-radius:50%; }
.ev-check.on { background:#fff; }
.ev-check.on::after { content:""; position:absolute; inset:3px; border-radius:50%; background:var(--primary); }
.ev-foot { display:flex; justify-content:space-between; font-size:11px; opacity:.92; margin-top:5px; }
.cal-pill { font-size:11.5px; padding:2px 7px; border-radius:6px; margin:2px 0; display:flex; gap:5px; align-items:center; overflow:hidden; white-space:nowrap; max-width:100%; min-width:0; }
.cal-pill .pt { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1 1 auto; min-width:0; }
.cal-pill .dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.cal-we { color:var(--danger); }
@media (max-width:800px) { .u-stats { grid-template-columns:repeat(2,1fr); } }
</style>
</head>
<body>
<div id="app">
  <aside class="sidebar">
    <div class="brand">astrbot_task<small>任务 · 记忆 · 好感</small></div>
    <div class="nav">
      <div class="nav-item on" data-p="dash">仪表盘</div>
      <div class="nav-item" data-p="calendar">日历</div>
      <div class="nav-item" data-p="tasks">任务</div>
      <div class="nav-item" data-p="memories">记忆</div>
      <div class="nav-item" data-p="chats">对话</div>
      <div class="nav-item" data-p="emotion">情绪</div>
      <div class="nav-item" data-p="docs">文档</div>
      <div class="nav-item" data-p="usage">监控</div>
    </div>
    <div style="margin-top:14px; padding:0 10px;">
      <span class="meta" id="side-aff"></span>
    </div>
  </aside>
  <div class="main">

    <div id="p-dash">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <h2 style="margin:0;">今日重点</h2><span class="meta" id="d-date"></span>
        </div>
        <div id="d-focus" style="margin-top:8px;"></div>
      </div>
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
          <h2 style="margin:0;">本周日程</h2>
          <div style="display:flex; gap:6px; align-items:center;">
            <button class="btn" onclick="shiftWeek(-1)">‹</button>
            <input type="date" id="w-date" onchange="jumpWeek(this.value)">
            <button class="btn" onclick="shiftWeek(1)">›</button>
            <button class="btn primary" onclick="jumpWeek(todayStr())">今天</button>
            <span class="meta" id="w-range"></span>
          </div>
        </div>
        <div class="week-grid" id="week-grid"></div>
        <div class="meta" id="w-stats" style="text-align:right; margin-top:8px;"></div>
      </div>
      <div class="card" id="d-overdue-card" style="display:none; border-left:3px solid var(--danger);">
        <h2 style="color:var(--danger);">逾期任务</h2>
        <div id="d-overdue"></div>
      </div>
    </div>

    <div id="p-calendar" style="display:none">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
          <h2 id="cal-title" style="margin:0;">日历</h2>
          <div style="display:flex; gap:6px;">
            <button class="btn" onclick="shiftMonth(-1)">‹</button>
            <button class="btn" onclick="shiftMonth(0)">今天</button>
            <button class="btn" onclick="shiftMonth(1)">›</button>
          </div>
        </div>
        <div class="cal-grid" id="cal-head" style="margin-bottom:4px;"></div>
        <div class="cal-grid" id="cal-grid"></div>
        <div class="meta" style="margin-top:8px;">点击日期查看当天任务</div>
      </div>
    </div>

    <div id="p-tasks" style="display:none">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
          <h2>任务</h2>
          <button class="btn primary" onclick="openTaskModal()">+ 添加</button>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px;">
          <select id="f-status" onchange="loadTasks()"><option value="">全部状态</option><option value="pending">待处理</option><option value="completed">已完成</option></select>
          <select id="f-type" onchange="loadTasks()"><option value="">任务+课程</option><option value="task">仅任务</option><option value="course">仅课程</option></select>
          <input type="text" id="f-search" placeholder="搜索..." style="flex:1; min-width:140px;" oninput="loadTasks()">
        </div>
        <div id="task-groups"></div>
      </div>
    </div>

    <div id="p-memories" style="display:none">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
          <h2>记忆关联图</h2>
          <button class="btn" onclick="loadGraph()">刷新</button>
        </div>
        <canvas id="graph" style="width:100%; height:360px; background:var(--bg); border-radius:10px; border:1px solid var(--border); cursor:pointer;"></canvas>
        <div class="meta" style="margin-top:6px;">节点大小=有效权重 · 连线=关键词连锁 · 金环★=持久层(想起≥5次晋升) · 拖动摆放 · 点击看详情 · 滚轮缩放</div>
        <div id="g-detail" style="display:none; margin-top:10px; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--bg); font-size:12.5px;"></div>
      </div>
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
          <h2 style="margin:0;">记忆列表</h2>
          <div style="display:flex; gap:6px; align-items:center;">
            <button class="btn" onclick="document.getElementById('imp-file').click()">导入</button>
            <input type="file" id="imp-file" accept=".json,application/json" style="display:none" onchange="importMems(this)">
            <span class="meta">导出</span>
            <button class="btn" onclick="location='/api/memories/export?format=json'">JSON</button>
            <button class="btn" onclick="location='/api/memories/export?format=csv'">CSV</button>
            <button class="btn" onclick="location='/api/memories/export?format=md'">MD</button>
            <button class="btn primary" onclick="addMemory()">+ 添加</button>
          </div>
        </div>
        <input type="text" id="m-kw" placeholder="搜索记忆..." style="width:100%; margin-bottom:10px;" oninput="loadMems(this.value)">
        <div id="mem-list"></div>
      </div>
    </div>

    <div id="p-emotion" style="display:none">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h2>情绪状态</h2><span class="meta">随对话规则更新 · AI 每轮微调</span>
        </div>
        <div class="sec">状态层(瞬时)</div>
        <div id="emo-state"></div>
        <div class="sec">关系层(持久)</div>
        <div id="emo-rel"></div>
        <div class="sec">最近变化</div>
        <div id="emo-log" class="meta"></div>
      </div>
    </div>

    <div id="p-chats" style="display:none">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <h2 style="margin:0;">未总结的对话</h2>
          <span class="meta" id="ch-meta"></span>
        </div>
        <div id="chats-list" style="margin-top:8px;"></div>
      </div>
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h2 style="margin:0;">已总结存档</h2><span class="meta">每 20 轮一份 · 事实已并入记忆节点</span>
        </div>
        <div id="sums-list"></div>
      </div>
    </div>

    <div id="p-docs" style="display:none">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <h2 style="margin:0;">文档</h2>
          <span class="meta">她写的 markdown 笔记(提纲/整理/模板) · 让她说"存成笔记"就会出现在这里</span>
        </div>
        <div id="docs-list" style="margin-top:8px;"></div>
      </div>
      <div class="card" id="doc-view-card" style="display:none;">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <h2 style="margin:0;" id="doc-view-title"></h2>
          <div style="display:flex; gap:6px;">
            <button class="btn" onclick="downloadDoc(currentDoc)">下载</button>
            <button class="btn" onclick="closeDocView()">关闭</button>
          </div>
        </div>
        <pre id="doc-view" style="white-space:pre-wrap; font-size:13px; line-height:1.8; margin-top:10px;"></pre>
      </div>
    </div>

    <div id="p-usage" style="display:none">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <h2 style="margin:0;">调用监控</h2>
          <span class="meta">插件内部任务 + 聊天(能取到 usage 时含 token) · 明细保留 90 天</span>
        </div>
        <div class="u-stats" style="margin-top:10px;" id="u-stats"></div>
      </div>
      <div class="u-cols">
        <div class="card">
          <div class="sec" style="margin-top:0;">Token 统计(近 7 天 · 按用途)</div>
          <div id="u-donut-tok"></div>
        </div>
        <div class="card">
          <div class="sec" style="margin-top:0;">调用统计(近 7 天 · 按用途)</div>
          <div id="u-donut-call"></div>
        </div>
      </div>
      <div class="card">
        <div class="sec" style="margin-top:0;">活跃度(近 90 天 · 逐日 tokens)</div>
        <div id="u-heat"></div>
        <div class="meta" style="margin-top:8px;">少 <span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:var(--bg);vertical-align:middle;"></span>
          <span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:#dfe9e8;vertical-align:middle;"></span>
          <span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:#bcd6d4;vertical-align:middle;"></span>
          <span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:#8dbcb8;vertical-align:middle;"></span>
          <span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:#5B8C8F;vertical-align:middle;"></span> 多
          · 悬停看当天次数与 tokens</div>
      </div>
      <div class="card">
        <div class="sec" style="margin-top:0;">最近 30 次调用</div>
        <div id="u-recent"></div>
      </div>
    </div>

  </div>
</div>

<!-- 任务编辑弹窗 -->
<div class="modal" id="task-modal">
  <div class="modal-c">
    <button class="modal-x" onclick="closeModal('task-modal')">×</button>
    <h2 id="tm-title">任务</h2>
    <input type="hidden" id="tm-id">
    <div class="form-g"><label>标题</label><input type="text" id="tm-name"></div>
    <div class="form-g"><label>截止日期</label><input type="date" id="tm-date"></div>
    <div class="form-g"><label>开始时间</label><input type="time" id="tm-time"></div>
    <div class="form-g"><label>结束时间(可选, 课程/会议填)</label><input type="time" id="tm-etime"></div>
    <div class="form-g"><label>优先级</label><select id="tm-pri"><option value="3">P3</option><option value="1">P1</option><option value="2">P2</option><option value="4">P4</option><option value="5">P5</option></select></div>
    <div class="form-g"><label>循环</label><select id="tm-recur"><option value="">不重复</option><option value="daily">每天</option><option value="weekly">每周</option><option value="monthly">每月</option><option value="yearly">每年</option></select></div>
    <div class="form-g"><label>循环截止(可选)</label><input type="date" id="tm-rend"></div>
    <div class="form-g"><label>分类(可选)</label><input type="text" id="tm-cat" placeholder="课程/学习/考研/生活..."></div>
    <button class="btn primary" onclick="saveTask()">保存</button>
    <button class="btn danger" onclick="delFromModal()" style="float:right;">删除任务</button>
  </div>
</div>

<!-- 日历当天任务弹窗 -->
<div class="modal" id="day-modal">
  <div class="modal-c">
    <button class="modal-x" onclick="closeModal('day-modal')">×</button>
    <h2 id="daym-title">当天任务</h2>
    <div id="daym-list"></div>
  </div>
</div>

<!-- 记忆编辑弹窗 -->
<div class="modal" id="mem-modal">
  <div class="modal-c">
    <button class="modal-x" onclick="closeModal('mem-modal')">×</button>
    <h2 id="mm-title">记忆</h2>
    <input type="hidden" id="mm-id">
    <div class="form-g"><label>内容(第一人称, 至少4字)</label><textarea id="mm-content" rows="3"></textarea></div>
    <div class="form-g"><label>类型</label><select id="mm-type"><option value="fact">事实</option><option value="preference">偏好</option><option value="task_context">任务背景</option></select></div>
    <div class="form-g"><label>重要度</label><select id="mm-imp"><option value="5">5 核心</option><option value="4">4 重要</option><option value="3">3 一般</option><option value="2">2 略记</option><option value="1">1 随手</option></select></div>
    <button class="btn primary" onclick="saveMem()">保存</button>
    <span class="meta" style="margin-left:10px;">保存后关键词会自动重新提取</span>
  </div>
</div>

<script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtLocal = d => d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
const todayStr = () => fmtLocal(new Date());
const WD = ['周日','周一','周二','周三','周四','周五','周六'];
const REC = {daily:'每天', weekly:'每周', monthly:'每月', yearly:'每年'};
/* 课程按标题稳定配色(teaskks 同款): 分类=课程 用标题哈希取色, 其他循环任务黄色, 一次性灰色。低饱和莫兰迪调 */
const PALETTE = ['#5E82B5', '#8E6FBF', '#3F9A78', '#C0616B', '#D3A445', '#4F9EA6', '#A76BA6', '#8A9A4D', '#C07E4F', '#6B7FC4'];
function colorOf(title) { let h = 0; for (const ch of String(title)) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; }
function cardColor(t) { return t.category === '课程' ? colorOf(t.title) : (t.recurring ? '#D3A445' : '#7C8AA0'); }
function toggleEv(id, date, isOcc, done) {
  if (isOcc) { occurrenceOrComplete(id, date, !done); }
  else { fetch('/api/tasks/' + id + '/complete', { method:'PUT' }).then(() => loadDash()); }
}
const loc = s => s ? new Date(String(s).replace(' ','T')).toLocaleString('zh-CN') : '';

function page(p) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('on', n.dataset.p === p));
  for (const id of ['dash','calendar','tasks','memories','chats','emotion','docs','usage']) document.getElementById('p-'+id).style.display = id === p ? '' : 'none';
  if (p === 'dash') loadDash();
  if (p === 'calendar') loadCalendar();
  if (p === 'tasks') loadTasks();
  if (p === 'memories') { loadMems(document.getElementById('m-kw').value); loadGraph(); }
  if (p === 'chats') loadChats();
  if (p === 'emotion') loadEmo();
  if (p === 'docs') loadDocs();
  if (p === 'usage') loadUsage();
}
document.querySelectorAll('.nav-item').forEach(n => n.onclick = () => page(n.dataset.p));

const api = async (p, opts) => (await fetch(p, opts)).json();
function openModal(id) { document.getElementById(id).classList.add('on'); }
function closeModal(id) { document.getElementById(id).classList.remove('active'); document.getElementById(id).classList.remove('on'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelectorAll('.modal.on').forEach(m => m.classList.remove('on')); });

/* ===== 仪表盘: 今日重点 + 周视图 ===== */
let weekAnchor = new Date();
async function loadDash() {
  document.getElementById('d-date').textContent = new Date().toLocaleDateString('zh-CN', {month:'long', day:'numeric', weekday:'long'});
  const tStr = todayStr();
  const all = await api('/api/tasks?all=1');
  const pending = all.filter(t => t.status === 'pending');
  const wk = await api('/api/week?start=' + tStr);   // 本周展开实例: 循环课按天出现
  const todayList = wk.filter(t => t.due_date === tStr);
  const overdue = pending.filter(t => !t.recurring && t.due_date && t.due_date < tStr);  // 循环母任务不算逾期
  const nowMin = new Date().getHours()*60 + new Date().getMinutes();
  const timed = todayList.filter(t => t.due_time).sort((a,b) => a.due_time < b.due_time ? -1 : 1);
  const next = timed.find(t => { const [h,m] = t.due_time.split(':').map(Number); return h*60+m >= nowMin - 15; });
  let focus;
  if (next) {
    const [h,m] = next.due_time.split(':').map(Number);
    const diff = h*60+m - nowMin;
    const when = diff <= 0 ? '就是现在' : diff < 60 ? diff + ' 分钟后' : Math.floor(diff/60) + ' 小时' + (diff%60 ? ' ' + diff%60 + ' 分' : '') + '后';
    focus = `<div style="display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;"><span class="next-big">${esc(next.title)}</span><span class="next-when">${next.due_time} · ${when}</span></div>`;
  } else if (timed.length) focus = '<div class="meta">今天带时间的安排都结束了, 干得漂亮</div>';
  else focus = '<div class="meta">今天没有带时间的安排</div>';
  document.getElementById('d-focus').innerHTML = focus;
  const oc = document.getElementById('d-overdue-card');
  if (overdue.length) {
    oc.style.display = '';
    document.getElementById('d-overdue').innerHTML = overdue.map(t => `<div class="row"><span style="flex:1;">${esc(t.title)}</span><span class="meta">截止 ${t.due_date}</span><button class="btn" onclick="occurrenceOrComplete('${t.id}','${t.due_date}',true)">完成</button></div>`).join('');
  } else oc.style.display = 'none';
  // 统计行
  document.getElementById('w-stats').textContent = `共 ${all.length} 项 · 待办 ${pending.length}` + (overdue.length ? ` · 逾期 ${overdue.length}` : '');
  // 侧栏好感
  const emo = await api('/api/emotion');
  document.getElementById('side-aff').textContent = `好感 ${emo.social_liking.toFixed(2)} · 信任 ${emo.trust.toFixed(2)}`;
  await loadWeek();
}

function shiftWeek(d) { weekAnchor.setDate(weekAnchor.getDate() + d); loadWeek(); }
function jumpWeek(s) { weekAnchor = new Date(s + 'T12:00:00'); loadWeek(); }

async function loadWeek() {
  // 滚动 7 天窗: 今天居中, 左三天右三天 — 周日也能看到下周一
  const anchor = new Date(weekAnchor); anchor.setHours(12, 0, 0, 0);
  const start = new Date(anchor); start.setDate(start.getDate() - 3);
  const end = new Date(anchor); end.setDate(end.getDate() + 3);
  document.getElementById('w-date').value = fmtLocal(anchor);
  document.getElementById('w-range').textContent = `今天居中 · ${fmtLocal(start).slice(5)} ~ ${fmtLocal(end).slice(5)}`;
  const items = await api('/api/week?start=' + fmtLocal(start) + '&days=7');
  const byDate = {};
  for (const t of items) (byDate[t.due_date] = byDate[t.due_date] || []).push(t);
  const tStr = todayStr();
  let html = '';
  for (let i = 0; i < 7; i++) {
    const d = new Date(start.getTime() + i*86400000);
    const ds = fmtLocal(d);
    const isToday = ds === tStr;
    const list = (byDate[ds] || []).sort((a,b) => (a.due_time||'99:99').localeCompare(b.due_time||'99:99') || (a.priority||3)-(b.priority||3));
    html += `<div class="week-col ${isToday?'today':''}"><div class="week-col-header"><div class="wd">${WD[d.getDay()]}${isToday?' · 今天':''}</div><div class="meta">${ds.slice(5)}</div></div>`;
    let prevT = '';
    for (const t of list) {
      const done = t.status === 'completed' || t.occurrence_done;
      const tm = t.due_time || '';
      if (tm && tm !== prevT) { html += `<div class="ev-time">${tm}${t.end_time ? ' - ' + t.end_time : ''}</div>`; prevT = tm; }
      const c = cardColor(t);
      const chk = `<span class="ev-check${done?' on':''}" title="打卡/完成" onclick="event.stopPropagation();toggleEv('${t.id}','${t.due_date}',${t.is_occurrence?1:0},${done?1:0})"></span>`;
      const tag = t.recurring ? (t.recurring === 'daily' ? '每天' : '每周') : '';
      html += `<div class="wk-card ev ${done?'done-card':''}" style="background:${c};" onclick="openTaskModal('${t.id}')">
        <div class="t">${esc(t.title)}</div>
        <div class="ev-foot"><span>${tag}</span><span>P${t.priority}</span></div>${chk}</div>`;
    }
    if (!list.length) html += '<div class="empty" style="padding:10px 0;">无安排</div>';
    html += '</div>';
  }
  document.getElementById('week-grid').innerHTML = html;
}

function occurrenceOrComplete(id, date, checked) {
  // 循环任务勾"当天", 普通任务直接完成
  fetch('/api/tasks/' + id + '/occurrence', { method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ date, completed: checked }) }).then(() => loadDash());
}

/* ===== 日历 ===== */
let calY = new Date().getFullYear(), calM = new Date().getMonth() + 1;
function shiftMonth(d) { calM += d; if (calM > 12) { calM = 1; calY++; } if (calM < 1) { calM = 12; calY--; } if (d === 0) { calY = new Date().getFullYear(); calM = new Date().getMonth()+1; } loadCalendar(); }
async function loadCalendar() {
  document.getElementById('cal-title').textContent = `${calY}年${calM}月`;
  const items = await api(`/api/calendar?year=${calY}&month=${calM}`);
  const byDay = {};
  for (const t of items) (byDay[t.due_date] = byDay[t.due_date] || []).push(t);
  const first = new Date(calY, calM-1, 1);
  const daysIn = new Date(calY, calM, 0).getDate();
  const lead = (first.getDay()+6)%7;
  const tStr = todayStr();
  let html = ['周一','周二','周三','周四','周五','周六','周日'].map((w, i) =>
    `<div style="text-align:center;" class="meta ${i>=5?'cal-we':''}"><b>${w}</b></div>`).join('');
  for (let i = 0; i < lead; i++) html += '<div class="cal-cell dim"></div>';
  for (let d = 1; d <= daysIn; d++) {
    const ds = `${calY}-${String(calM).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const dow = (new Date(calY, calM-1, d).getDay() + 6) % 7;
    const evs = (byDay[ds] || []);
    html += `<div class="cal-cell ${ds===tStr?'today-cell':''}" onclick="openDay('${ds}')">
      <div class="cal-day">${d}${ds===tStr?' <span class="tag" style="background:var(--primary);color:#fff;">今</span>':''}</div>
      ${evs.slice(0,4).map(t => { const c = cardColor(t);
        return `<div class="cal-pill" style="background:color-mix(in srgb, ${c} 15%, var(--card));"><span class="dot" style="background:${c};"></span><span class="pt">${t.due_time ? esc(t.due_time)+' ' : ''}${esc(t.title)}</span></div>`; }).join('')}
      ${evs.length > 4 ? `<div class="cal-ev">…还有 ${evs.length-4} 项</div>` : ''}</div>`;
  }
  document.getElementById('cal-head').innerHTML = '';
  document.getElementById('cal-grid').innerHTML = html;
}
async function openDay(ds) {
  document.getElementById('daym-title').textContent = ds + ' 的任务';
  const items = await api(`/api/calendar?year=${ds.slice(0,4)}&month=${parseInt(ds.slice(5,7))}`);
  const dayItems = items.filter(t => t.due_date === ds);
  document.getElementById('daym-list').innerHTML = dayItems.length ? dayItems.map(t => `
    <div class="row"><input type="checkbox" ${t.occurrence_done || t.status==='completed' ? 'checked':''} onchange="occurrenceOrComplete('${t.id}','${ds}',this.checked)">
    <span style="flex:1;" class="${t.occurrence_done||t.status==='completed'?'done':''}">${t.due_time ? '<b>'+t.due_time+'</b> ' : ''}${esc(t.title)}</span>
    <button class="btn" onclick="openTaskModal('${t.id}'); closeModal('day-modal')">编辑</button></div>`).join('')
    : '<div class="empty">当天没有任务</div>';
  openModal('day-modal');
}

/* ===== 任务(分组折叠 + 筛选) ===== */
const GROUPS = [
  {k:'overdue', label:'逾期', open:true}, {k:'today', label:'今天', open:true},
  {k:'recurring', label:'循环课程与习惯', open:false}, {k:'week', label:'本周', open:true},
  {k:'later', label:'以后', open:false}, {k:'someday', label:'无期限', open:false},
  {k:'done', label:'已完成', open:false},
];
function foldState() { try { return JSON.parse(localStorage.getItem('tf') || '{}'); } catch { return {}; } }
function toggleGroup(k) { const s = foldState(); s[k] = !s[k]; localStorage.setItem('tf', JSON.stringify(s)); loadTasks(); }

async function loadTasks() {
  const st = document.getElementById('f-status').value;
  const ty = document.getElementById('f-type').value;
  const se = document.getElementById('f-search').value;
  const qs = new URLSearchParams();
  if (st) qs.append('status', st);
  if (ty) qs.append('type', ty);
  if (se) qs.append('search', se);
  const tasks = await api('/api/tasks' + (qs.toString() ? '?' + qs : '?all=1'));
  const tStr = todayStr();
  const monday = new Date(); monday.setDate(monday.getDate() - ((monday.getDay()+6)%7));
  const sundayStr = fmtLocal(new Date(monday.getTime() + 6*86400000));
  const B = {}; GROUPS.forEach(g => B[g.k] = []);
  for (const t of tasks) {
    const done = t.status === 'completed' || t.status === 'cancelled';
    if (done) { B.done.push(t); continue; }
    if (t.recurring) { B.recurring.push(t); continue; }
    if (!t.due_date) { B.someday.push(t); continue; }
    if (t.due_date < tStr) { B.overdue.push(t); continue; }
    if (t.due_date === tStr) { B.today.push(t); continue; }
    if (t.due_date <= sundayStr) { B.week.push(t); continue; }
    B.later.push(t);
  }
  const sortT = (a,b) => ((a.due_date||'9999')+(a.due_time||'')).localeCompare((b.due_date||'9999')+(b.due_time||'')) || (a.priority||3)-(b.priority||3);
  const fold = foldState();
  document.getElementById('task-groups').innerHTML = GROUPS.filter(g => B[g.k].length).map(g => {
    const items = B[g.k].sort(sortT);
    const open = fold[g.k] !== undefined ? fold[g.k] : g.open;
    return `<div class="fold ${open?'on':''}"><div class="fold-h" onclick="toggleGroup('${g.k}')">
      <div style="display:flex; gap:8px; align-items:center;"><span class="arrow">›</span><b style="font-size:13.5px; ${g.k==='overdue'?'color:var(--danger);':''}">${g.label}</b></div>
      <span class="meta">${items.length} 项</span></div>
      ${open ? '<div class="fold-b">' + items.map(taskRow).join('') + '</div>' : ''}</div>`;
  }).join('') || '<div class="empty">暂无任务</div>';
}
function taskRow(t) {
  const done = t.status === 'completed';
  const catTag = t.category ? '<span class="tag">' + esc(t.category) + '</span> ' : '';
  return `<div class="row" style="cursor:default;">
    <input type="checkbox" ${done?'checked':''} onchange="occurrenceOrComplete('${t.id}','${t.due_date||todayStr()}',this.checked)">
    <span style="flex:1;" class="${done?'done':''}">${t.recurring ? '<span class="tag course">课程</span> ' : ''}${catTag}${esc(t.title)}</span>
    <span class="meta">${t.due_date || '无日期'}${t.due_time ? ' ' + t.due_time + (t.end_time ? '-' + t.end_time : '') : ''} P${t.priority}</span>
    <button class="btn" onclick="openTaskModal('${t.id}')">编辑</button>
    <button class="btn danger" onclick="delTask('${t.id}')">删除</button></div>`;
}
async function completeTask(id, checked) { await api('/api/tasks/' + id + '/complete', { method:'PUT' }); loadTasks(); }
async function delTask(id) { await api('/api/tasks/' + id, { method:'DELETE' }); loadTasks(); }

/* ===== 任务弹窗 ===== */
async function openTaskModal(id) {
  const t = id ? (await api('/api/tasks?all=1')).find(x => x.id == id) : null;
  if (id && !t) return;
  document.getElementById('tm-title').textContent = id ? '编辑任务' : '新建任务';
  document.getElementById('tm-id').value = id || '';
  document.getElementById('tm-name').value = t ? t.title : '';
  document.getElementById('tm-date').value = t ? (t.due_date || '') : '';
  document.getElementById('tm-time').value = t ? (t.due_time || '') : '';
  document.getElementById('tm-etime').value = t ? (t.end_time || '') : '';
  document.getElementById('tm-pri').value = t ? t.priority : 3;
  document.getElementById('tm-recur').value = t ? (t.recurring || '') : '';
  document.getElementById('tm-rend').value = t ? (t.recurrence_end || '') : '';
  document.getElementById('tm-cat').value = t ? (t.category || '') : '';
  openModal('task-modal');
}
async function saveTask() {
  const id = document.getElementById('tm-id').value;
  const body = { title: document.getElementById('tm-name').value, due_date: document.getElementById('tm-date').value,
    due_time: document.getElementById('tm-time').value, end_time: document.getElementById('tm-etime').value,
    priority: parseInt(document.getElementById('tm-pri').value),
    recurring: document.getElementById('tm-recur').value, recurrence_end: document.getElementById('tm-rend').value,
    category: document.getElementById('tm-cat').value };
  if (!body.title.trim()) return;
  if (id) await api('/api/tasks/' + id, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  else await api('/api/tasks', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  closeModal('task-modal');
  loadTasks();
}
function delFromModal() {
  const id = document.getElementById('tm-id').value;
  if (!id) return;
  api('/api/tasks/' + id, { method:'DELETE' }).then(() => { closeModal('task-modal'); loadTasks(); });
}

/* ===== 记忆 + 关联图 ===== */
async function importMems(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    const res = await api('/api/memories/import', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
    if (res.error) { alert('导入失败: ' + res.error); return; }
    alert(`导入完成: 新增 ${res.created} · 升级 ${res.upgraded} · 跳过重复 ${res.skipped}` + (res.summaries ? ` · 总结存档 ${res.summaries}` : ''));
  } catch (e) { alert('导入失败: 文件不是合法的导出 JSON'); }
  input.value = '';
  loadMems(document.getElementById('m-kw').value);
  loadGraph();
}

async function loadMems(kw) {
  const list = await api('/api/memories?kw=' + encodeURIComponent(kw || ''));
  const el = document.getElementById('mem-list');
  if (!list.length) { el.innerHTML = '<div class="empty">暂无记忆(对话满20轮总结 / 说"记住X"会积累)</div>'; return; }
  el.innerHTML = list.map(m => `
    <div class="row" style="align-items:flex-start;">
      <span class="tag">${esc(m.memory_type)}</span>
      <span>${Array(5).fill(0).map((_,i) => `<span style="color:${i < m.importance ? 'var(--primary)' : 'var(--border)'};">●</span>`).join('')}</span>
      <span style="flex:1; font-size:13.5px;">${esc(m.content)}</span>
      <span class="meta">${loc(m.created_at)}</span>
      <button class="btn" onclick="editMem(${m.id})">编辑</button>
      <button class="btn danger" onclick="delMem(${m.id})">删除</button>
    </div>`).join('');
}
async function editMem(id) {
  const list = await api('/api/memories?kw=' + encodeURIComponent(document.getElementById('m-kw').value));
  const t = list.find(x => x.id == id); if (!t) return;
  document.getElementById('mm-id').value = t.id;
  document.getElementById('mm-content').value = t.content;
  document.getElementById('mm-type').value = t.memory_type;
  document.getElementById('mm-imp').value = t.importance;
  openModal('mem-modal');
}
function addMemory() {
  document.getElementById('mm-id').value = '';
  document.getElementById('mm-content').value = '';
  document.getElementById('mm-type').value = 'fact';
  document.getElementById('mm-imp').value = '4';
  openModal('mem-modal');
}
async function saveMem() {
  const id = document.getElementById('mm-id').value;
  const body = { content: document.getElementById('mm-content').value,
    memory_type: document.getElementById('mm-type').value,
    importance: parseInt(document.getElementById('mm-imp').value) };
  if (body.content.trim().length < 4) return;
  if (id) await api('/api/memories/' + id, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  else await api('/api/memories', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  closeModal('mem-modal');
  loadMems(document.getElementById('m-kw').value);
  loadGraph();
}

/* ===== 关联图(力导向 Canvas, 移植自 teaskks) ===== */
let gData = null, gNodes = [], gEdges = [], gZoom = 1, gPanX = 0, gPanY = 0, gHl = null, gDrag = null;
async function loadGraph() {
  gData = await api('/api/memory/graph');
  const c = document.getElementById('graph');
  const w = c.clientWidth || 800, h = 360;
  gNodes = (gData.nodes || []).map((n, i) => ({...n, x: w/2 + Math.cos(i*2.4)*(60+(i%12)*22), y: 180 + Math.sin(i*2.4)*(40+(i%10)*16), vx:0, vy:0}));
  const byId = Object.fromEntries(gNodes.map(n => [n.id, n]));
  gEdges = (gData.edges || []).map(e => ({...e, na: byId[e.a], nb: byId[e.b]})).filter(e => e.na && e.nb);
  const wMax = Math.max(0.001, ...gEdges.map(e => e.w));
  for (let it = 0; it < 300; it++) {
    for (let i = 0; i < gNodes.length; i++) for (let j = i+1; j < gNodes.length; j++) {
      const a = gNodes[i], b = gNodes[j];
      let dx = b.x-a.x, dy = b.y-a.y; const d2 = dx*dx+dy*dy+0.01, d = Math.sqrt(d2), rep = 2600/d2;
      dx/=d; dy/=d; a.vx-=dx*rep; a.vy-=dy*rep; b.vx+=dx*rep; b.vy+=dy*rep;
    }
    for (const e of gEdges) {
      const a = e.na, b = e.nb;
      let dx = b.x-a.x, dy = b.y-a.y; const d = Math.sqrt(dx*dx+dy*dy)+0.01;
      const f = (d - (80+70*(1-e.w/wMax)))*0.02; dx/=d; dy/=d;
      a.vx+=dx*f; a.vy+=dy*f; b.vx-=dx*f; b.vy-=dy*f;
    }
    for (const n of gNodes) {
      n.vx += (w/2-n.x)*0.002; n.vy += (180-n.y)*0.002; n.vx*=0.85; n.vy*=0.85;
      n.x = Math.max(26, Math.min(w-26, n.x+n.vx)); n.y = Math.max(20, Math.min(h-20, n.y+n.vy));
    }
  }
  drawGraph();
}
const TYPE_C = { fact:'#5b8c8f', preference:'#d4a574', task_context:'#7ba89c', conversation:'#8ba8b5', analysis:'#c67b7b' };
function drawGraph() {
  const c = document.getElementById('graph');
  if (!c) return;
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth || 800, h = 360;
  c.width = w*dpr; c.height = h*dpr;
  const ctx = c.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.translate(gPanX, gPanY); ctx.scale(gZoom, gZoom);
  ctx.clearRect(-gPanX/gZoom, -gPanY/gZoom, w/gZoom, h/gZoom);
  if (!gNodes.length) { ctx.fillStyle = '#7b9fa8'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center'; ctx.fillText('暂无记忆', w/2, 180); return; }
  const wMax = Math.max(0.001, ...gEdges.map(e => e.w));
  const nb = gHl ? new Set(gEdges.filter(e => e.a===gHl || e.b===gHl).flatMap(e => [e.a, e.b])) : null;
  for (const e of gEdges) {
    const dim = gHl && e.a !== gHl && e.b !== gHl;
    ctx.strokeStyle = 'rgba(91,140,143,' + (dim ? 0.06 : 0.15+0.45*(e.w/wMax)) + ')';
    ctx.lineWidth = dim ? 0.6 : 1 + 2.4*(e.w/wMax);
    ctx.beginPath(); ctx.moveTo(e.na.x, e.na.y); ctx.lineTo(e.nb.x, e.nb.y); ctx.stroke();
  }
  for (const n of gNodes) {
    const r = 5 + (n.eff || 3) * 2.4;
    const dim = nb && n.id !== gHl && !nb.has(n.id);
    ctx.globalAlpha = dim ? 0.22 : 1;
    const acc = n.access_count || 0;
    if (!n.pinned && acc > 0) { ctx.strokeStyle = '#d4a574'; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(n.x, n.y, r+4, -Math.PI/2, -Math.PI/2+Math.min(acc/5,1)*Math.PI*2); ctx.stroke(); }
    if (n.pinned) { ctx.strokeStyle = '#c9971c'; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(n.x, n.y, r+4, 0, Math.PI*2); ctx.stroke(); }
    ctx.fillStyle = TYPE_C[n.type] || '#8ba8b5';
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI*2); ctx.fill();
    if (n.id === gHl) { ctx.strokeStyle = '#5b8c8f'; ctx.lineWidth = 2.5; ctx.stroke(); }
    if (n.pinned) { ctx.fillStyle = '#c9971c'; ctx.font = 'bold 11px sans-serif'; ctx.textAlign = 'left'; ctx.fillText('★', n.x+r*0.4, n.y-r*0.5); }
    ctx.fillStyle = '#2c3e50'; ctx.font = Math.max(9, Math.min(12, 7+r/3)) + 'px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText((n.label || '').slice(0, 10), n.x, n.y + r + 11);
    ctx.globalAlpha = 1;
  }
}
function bindGraph() {
  const c = document.getElementById('graph');
  if (!c || c.__bound) return;
  c.__bound = true;
  const hit = (mx, my) => {
    const wx = (mx-gPanX)/gZoom, wy = (my-gPanY)/gZoom;
    let hit = null, best = 1e9;
    for (const n of gNodes) { const dd = (n.x-wx)**2 + (n.y-wy)**2; if (dd < 800 && dd < best) { best = dd; hit = n; } }
    return hit;
  };
  const pos = ev => { const r = c.getBoundingClientRect(); return { x: ev.clientX-r.left, y: ev.clientY-r.top }; };
  c.addEventListener('mousedown', ev => { const p = pos(ev); const h = hit(p.x, p.y);
    gDrag = h ? { node: h, moved: false } : { pan: true, sx: p.x, sy: p.y, ox: gPanX, oy: gPanY, moved: false }; });
  c.addEventListener('mousemove', ev => { if (!gDrag) return; const p = pos(ev);
    if (gDrag.pan) { gPanX = gDrag.ox + (p.x-gDrag.sx); gPanY = gDrag.oy + (p.y-gDrag.sy); gDrag.moved = true; drawGraph(); return; }
    const wx = (p.x-gPanX)/gZoom, wy = (p.y-gPanY)/gZoom;
    gDrag.node.x = Math.max(26, Math.min(c.clientWidth-26, wx)); gDrag.node.y = Math.max(20, Math.min(340, wy)); gDrag.moved = true; drawGraph(); });
  c.addEventListener('mouseup', () => { if (!gDrag) return;
    if (!gDrag.moved && gDrag.node) { gHl = gHl === gDrag.node.id ? null : gDrag.node.id; showNode(gHl ? gNodes.find(n => n.id === gHl) : null); }
    gDrag = null; drawGraph(); });
  c.addEventListener('mouseleave', () => { gDrag = null; });
  c.addEventListener('wheel', ev => { ev.preventDefault(); const p = pos(ev);
    const f = ev.deltaY < 0 ? 1.15 : 0.87; const nz = Math.max(0.4, Math.min(3, gZoom*f));
    const wx = (p.x-gPanX)/gZoom, wy = (p.y-gPanY)/gZoom;
    gPanX = p.x - wx*nz; gPanY = p.y - wy*nz; gZoom = nz; drawGraph(); }, { passive: false });
  c.addEventListener('dblclick', () => { gZoom = 1; gPanX = 0; gPanY = 0; drawGraph(); });
}
function showNode(n) {
  const box = document.getElementById('g-detail');
  if (!n) { box.style.display = 'none'; return; }
  const toPin = Math.max(0, 5 - (n.access_count || 0));
  box.style.display = 'block';
  box.innerHTML = `<b>${esc(String(n.content).slice(0, 100))}</b><br>` +
    ['类型 ' + n.type, '重要度 ' + n.importance, '有效权重 ' + n.eff, '强度 ' + n.intensity, '愉悦价 ' + n.valence,
     n.pinned ? '★ 持久层' : '距晋升 ' + toPin + ' 次回想', '被想起 ' + (n.access_count||0) + ' 次']
      .map(x => `<span class="tag">${esc(x)}</span>`).join(' ');
}
function addMemory() {
  const content = prompt('记忆内容(第一人称, 如: 主人每周三下午有实验室组会):');
  if (!content) return;
  const importance = parseInt(prompt('重要性 1-5(核心给4-5):', '4') || '4');
  api('/api/memories', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ content, memory_type:'fact', importance }) }).then(() => { loadMems(''); loadGraph(); });
}

/* ===== 情绪 ===== */
async function loadEmo() {
  const [d, log] = await Promise.all([api('/api/emotion'), api('/api/emotion_log?limit=6')]);
  const bar = (label, v, min, max) => {
    const over = v > max;
    const pct = Math.max(0, Math.min(100, ((v - min) / (max - min)) * 100));
    const fill = over ? 'width:100%; background:linear-gradient(90deg,var(--primary),#c9971c);' : `width:${pct}%; background:var(--primary);`;
    return `<div class="row"><span style="width:76px;">${label}</span><div class="bar"><div class="fill" style="${fill}"></div></div><span class="val">${Number(v).toFixed(2)}</span></div>`;
  };
  document.getElementById('emo-state').innerHTML =
    bar('愉悦度 P', d.P, -1, 1) + bar('唤醒度 A', d.A, 0, 1) + bar('支配度 D', d.D, 0, 1) +
    bar('疲惫', d.fatigue, 0, 1) + bar('挫败', d.frustration, 0, 1);
  // 关系层彩蛋: 满之前绿→金渐变逐渐点亮, 满之后整体金→红越陷越深——界面不做任何文字说明
  const hexLerp = (a, b, t) => {
    const pa = [1,3,5].map(i => parseInt(a.slice(i,i+2),16)), pb = [1,3,5].map(i => parseInt(b.slice(i,i+2),16));
    return '#' + pa.map((x,i) => Math.round(x + (pb[i]-x)*t).toString(16).padStart(2,'0')).join('');
  };
  const relBar = (label, v, alt) => {
    v = Number(v) || 0;
    const name = (alt && v >= 1) ? alt : label;   // 社交好感满1进入红色阶段后, 悄悄改叫「爱」
    let inner;
    if (v < 1) {
      const cover = (1 - Math.max(0, v)) * 100;
      inner = `<div class="rel-ramp"></div><div class="rel-cover" style="width:${cover}%;"></div>`;
    } else {
      const t = Math.min(1, (v - 1) / 4);
      inner = `<div class="rel-fill" style="width:100%; background:linear-gradient(90deg, #D3A445, ${hexLerp('#D3A445', '#C05050', t)});"></div>`;
    }
    return `<div class="row"><span style="width:76px;">${name}</span><div class="bar rel">${inner}</div><span class="val">${v.toFixed(2)}</span></div>`;
  };
  document.getElementById('emo-rel').innerHTML =
    relBar('信任', d.trust) + relBar('任务好感', d.task_liking) + relBar('社交好感', d.social_liking, '爱');
  document.getElementById('emo-log').innerHTML = (log.length ? log.map(l => `<div>${esc(l.created_at)} · ${esc(l.trigger || '')}</div>`).join('') : '暂无变化记录(对话后产生)');
}

/* ===== 对话: 未总结(带标记) + 已总结存档 ===== */
async function loadChats() {
  const [ch, sums] = await Promise.all([api('/api/chats?limit=80'), api('/api/summaries?limit=30')]);
  document.getElementById('ch-meta').textContent =
    `未总结 ${ch.pending_count || 0} 条 · 累计 ${ch.rounds || 0} 轮 · 满 20 轮自动总结并标记`;
  const msgs = ch.pending || [];
  document.getElementById('chats-list').innerHTML = msgs.length ? msgs.map(m => `
    <div style="display:flex; gap:8px; align-items:baseline; padding:5px 0; border-bottom:1px solid var(--border);">
      <span class="tag" style="background:${m.role === 'user' ? 'var(--primary)' : '#8E6FBF'};color:#fff;flex-shrink:0;">${m.role === 'user' ? '主人' : '她'}</span>
      <span style="flex:1; font-size:13.5px; line-height:1.6;">${esc(m.text)}</span>
      <span class="meta" style="flex-shrink:0;">${esc(loc(m.created_at))}</span>
    </div>`).join('') : '<div class="empty">还没有未总结的对话 — 和她聊起来吧</div>';
  document.getElementById('sums-list').innerHTML = sums.length ? sums.map(s => `
    <div class="card" style="margin-bottom:10px;">
      <div class="meta" style="margin-bottom:6px;">${esc(loc(s.created_at))} · ${factsN(s)} 条事实入记忆</div>
      <div style="font-size:14px; line-height:1.8;">${esc(s.summary)}</div>
    </div>`).join('') : '<div class="meta">还没有总结 — 攒满 20 轮会自动生成一份</div>';
}
const factsN = s => { try { return (JSON.parse(s.facts_json) || []).length; } catch (e) { return 0; } };

/* ===== 文档: 她写的笔记(markdown) ===== */
let currentDoc = '';
async function loadDocs() {
  const list = await api('/api/notes');
  document.getElementById('docs-list').innerHTML = list.length ? list.map(n => `
    <div class="row">
      <span style="flex:1; font-size:13.5px;">${esc(n.title)}</span>
      <span class="meta">${fmtN(n.size)} 字节 · ${esc(n.mtime)}</span>
      <button class="btn" onclick="viewDoc('${esc(n.title)}')">查看</button>
      <button class="btn" onclick="downloadDoc('${esc(n.title)}')">下载</button>
      <button class="btn danger" onclick="delDoc('${esc(n.title)}')">删除</button>
    </div>`).join('') : '<div class="empty">还没有文档 — 让她整理点什么, 说"存成笔记"就会出现在这里</div>';
}
async function viewDoc(t) {
  const d = await api('/api/notes/get?title=' + encodeURIComponent(t));
  currentDoc = t;
  document.getElementById('doc-view-title').textContent = t;
  document.getElementById('doc-view').textContent = d.content || '';
  document.getElementById('doc-view-card').style.display = '';
  document.getElementById('doc-view-card').scrollIntoView({ behavior: 'smooth' });
}
function closeDocView() { document.getElementById('doc-view-card').style.display = 'none'; currentDoc = ''; }
function downloadDoc(t) { location = '/api/notes/download?title=' + encodeURIComponent(t); }
async function delDoc(t) {
  if (!confirm('删除「' + t + '」?')) return;
  await api('/api/notes/del?title=' + encodeURIComponent(t), { method: 'DELETE' });
  loadDocs();
  if (currentDoc === t) closeDocView();
}

/* ===== 监控: 调用次数 + token 消耗(甜甜圈 + 热力图) ===== */
const U_KIND = {chat:'聊天', intent:'意图分析', summary:'周期总结', extract:'自动提取', keywords:'关键词精修', notify:'推送口吻', plan:'计划生成', misc:'其他'};
const U_COLOR = {chat:'#5B8C8F', intent:'#8E6FBF', summary:'#D3A445', extract:'#3F9A78', keywords:'#4F9EA6', notify:'#C0616B', plan:'#A76BA6', misc:'#7C8AA0'};
const fmtN = n => (n === null || n === undefined) ? '—' : Number(n).toLocaleString('zh-CN');
const fmtK = n => { if (n === null || n === undefined) return '—'; n = Number(n);
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M'; if (n >= 1e3) return (n/1e3).toFixed(1) + 'k'; return String(n); };

function donut(el, items, centerLab, centerVal) {
  const total = items.reduce((s, i) => s + i.v, 0);
  const R = 54, C = 2 * Math.PI * R;
  let arcs = '', off = 0;
  if (!total) arcs = `<circle r="${R}" cx="70" cy="70" fill="none" stroke="var(--border)" stroke-width="16"/>`;
  items.forEach(it => {
    const len = (it.v / total) * C;
    arcs += `<circle r="${R}" cx="70" cy="70" fill="none" stroke="${it.color}" stroke-width="16" stroke-dasharray="${Math.max(0,len-1.5)} ${C-Math.max(0,len-1.5)}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)"/>`;
    off += len;
  });
  const legend = items.map(it => `<div class="lg-row"><span class="lg-chip" style="background:${it.color};"></span>
    <span>${esc(it.label)}</span><span class="lg-pct">${total ? Math.round(it.v/total*100) : 0}%</span>
    <span class="meta">${fmtK(it.v)} · ${fmtN(it.n)}次</span></div>`).join('');
  el.innerHTML = `<div class="donut-flex">
    <svg viewBox="0 0 140 140" width="132" height="132" role="img">${arcs}
      <text x="70" y="67" text-anchor="middle" font-size="19" font-weight="700">${esc(centerVal)}</text>
      <text x="70" y="84" text-anchor="middle" font-size="10" fill="var(--text-light)">${esc(centerLab)}</text></svg>
    <div class="lg">${legend}</div></div>`;
}

function heatmap(el, byDay) {
  const map = {}; (byDay || []).forEach(r => { map[r.d] = r; });
  let max = 1; (byDay || []).forEach(r => { if ((r.t || 0) > max) max = r.t; });
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const mondayThis = new Date(today); mondayThis.setDate(today.getDate() - (today.getDay() + 6) % 7);
  const start = new Date(mondayThis); start.setDate(mondayThis.getDate() - 12 * 7);
  const WD = ['周一','周二','周三','周四','周五','周六','周日'];
  let html = '';
  for (let r = 0; r < 7; r++) {
    html += '<div class="hm-row"><div class="hm-lab">' + WD[r] + '</div>';
    for (let w = 0; w < 13; w++) {
      const d = new Date(start); d.setDate(start.getDate() + w * 7 + r);
      const ds = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
      if (d > today) { html += '<div class="hm-cell hm-none"></div>'; continue; }
      const rec = map[ds];
      const lvl = rec ? Math.min(4, Math.max(1, Math.ceil((rec.t || 0) / max * 4))) : 0;
      const tip = rec ? `${ds} · ${rec.n} 次 · ${fmtK(rec.t)} tokens` : `${ds} · 无调用`;
      html += `<div class="hm-cell hm-l${lvl}" title="${esc(tip)}"></div>`;
    }
    html += '</div>';
  }
  el.innerHTML = html;
}

async function loadUsage() {
  const d = await api('/api/usage?days=7');
  const tot = d.total || {}, today = d.today || {};
  document.getElementById('u-stats').innerHTML = `
    <div class="u-stat"><div class="v">${fmtN(today.n)}</div><div class="k">今日调用次数</div></div>
    <div class="u-stat"><div class="v">${fmtK(today.t)}</div><div class="k">今日 tokens</div></div>
    <div class="u-stat"><div class="v">${fmtK(tot.t)}</div><div class="k">近 7 天 tokens</div></div>
    <div class="u-stat"><div class="v">${fmtN(tot.n)}</div><div class="k">近 7 天调用次数</div></div>`;
  const kinds = d.by_kind || [];
  donut(document.getElementById('u-donut-tok'),
    kinds.filter(k => k.t).map(k => ({label: U_KIND[k.kind] || k.kind, v: k.t, n: k.n, color: U_COLOR[k.kind] || U_COLOR.misc})),
    '总 tokens', fmtK(tot.t));
  donut(document.getElementById('u-donut-call'),
    kinds.map(k => ({label: U_KIND[k.kind] || k.kind, v: k.n, color: U_COLOR[k.kind] || U_COLOR.misc})),
    '总调用', fmtN(tot.n));
  const pin = kinds.reduce((s, k) => s + (k.p || 0), 0), cout = kinds.reduce((s, k) => s + (k.c || 0), 0);
  if (pin + cout > 0) {
    document.querySelector('#u-donut-tok .donut-flex').insertAdjacentHTML('afterend',
      `<div class="meta" style="margin-top:8px;">输入 ${fmtK(pin)} · 输出 ${fmtK(cout)}</div>`);
  }
  heatmap(document.getElementById('u-heat'), d.heat);
  const rec = d.recent || [];
  document.getElementById('u-recent').innerHTML = rec.length ? `
    <table class="tbl"><tr><th>时间</th><th>用途</th><th>provider / 模型</th><th>tokens (入/出)</th><th>耗时</th><th></th></tr>
    ${rec.map(r => `<tr><td>${esc(loc(r.created_at))}</td><td>${esc(U_KIND[r.kind] || r.kind)}</td>
      <td class="meta">${esc(r.provider || '—')}${r.model ? ' / ' + esc(r.model) : ''}</td>
      <td class="tabular">${fmtN(r.total_tokens)} (${fmtN(r.prompt_tokens)}/${fmtN(r.completion_tokens)})</td>
      <td class="tabular">${r.duration_ms === null || r.duration_ms === undefined ? '—' : r.duration_ms + ' ms'}</td>
      <td${r.ok ? '' : ' style="color:var(--danger);"'}>${r.ok ? '' : '失败'}</td></tr>`).join('')}
    </table>` : '<div class="meta">还没有调用记录 — 聊几句或等一次定时推送后这里会出现数据</div>';
}

page('dash');
setTimeout(bindGraph, 300);
</script>
</body>
</html>"""


def start(db_path: str, port: int = 6187, host: str = "127.0.0.1"):
    if _get_server():
        return  # 进程级注册表里已有存活服务(跨模块重载存活), 不重复绑定
    _state["db_path"] = db_path
    _state["port"] = port
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"[astrbot_task] WebUI 端口 {port} 被占用, 未启动: {e}")
        return
    server.daemon_threads = True
    _set_server(server)
    threading.Thread(target=server.serve_forever, daemon=True, name="astrbot-task-webui").start()
    print(f"[astrbot_task] WebUI 已启动: http://{host}:{port}")


def stop():
    srv = _get_server()
    if srv:
        try:
            srv.shutdown()
        except Exception:
            pass
        _set_server(None)
        print("[astrbot_task] WebUI 已停止")
