# astrbot_task 任务核心(v0.5, 移植自 Teaskks taskService 的循环展开与单次打卡):
# expand_recurring: daily/weekly/monthly/yearly 循环任务按日期区间展开(recurrence_end 尊重)
# occurrence: 循环任务"勾掉某一天这一次"(task_occurrence_completions), 可逆
# v0.5 新增: validate_task/create_task/update_task/complete_task/delete_task 纯函数(LLM 工具与确认流程共用)

import datetime
import json
import re


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")
RECUR_TYPES = ("daily", "weekly", "monthly", "yearly")
TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–~至到]\s*(\d{1,2}:\d{2})")


def _to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _time_range(task):
    """任务的 (开始分钟, 结束分钟)。结束优先级: end_time 字段 > 标题里的 '8:00-9:40' > 开始+1(瞬时)。无时间返回 None"""
    start = task.get("due_time")
    if not start:
        return None
    end = task.get("end_time")
    if not end:
        m = TIME_RANGE_RE.search(str(task.get("title") or ""))
        if m:
            end = m.group(2)
    s = _to_min(start)
    e = _to_min(end) if end else s + 1
    if e <= s:
        e = s + 1
    return s, e


def _slot_key(task):
    """任务的时段标识: 单次=具体日期; 循环=星期/月日(带首次与截止, 供单次比对窗口)"""
    dd = task.get("due_date")
    if not dd:
        return None
    d = _d(dd)
    rec = task.get("recurring")
    if rec == "weekly":
        return ("w", d.weekday(), dd, task.get("recurrence_end") or None)
    if rec == "monthly":
        return ("m", d.day, dd, task.get("recurrence_end") or None)
    if rec == "yearly":
        return ("y", d.month, d.day, dd, task.get("recurrence_end") or None)
    return ("d", dd)


def _key_match(a, b):
    """两个时段标识是否落在同一时间轴上(单次日期落在循环周期内也算, 循环间比对学期窗口)"""
    ka, kb = a[0], b[0]

    def one_vs_loop(dstr, loop):
        """单次日期是否落在某个循环的发生日上(尊重首次与截止)"""
        dv = _d(dstr)
        if loop[0] == "w":
            wd, base, rend = loop[1], loop[2], loop[3]
            if dv.weekday() != wd:
                return False
            if dv < _d(base):
                return False
            return not rend or dv <= _d(rend)
        if loop[0] == "m":
            dom, base, rend = loop[1], loop[2], loop[3]
            if dv.day != dom:
                return False
            if dv < _d(base):
                return False
            return not rend or dv <= _d(rend)
        if loop[0] == "y":
            mo, dy, base, rend = loop[1], loop[2], loop[3], loop[4]
            if (dv.month, dv.day) != (mo, dy):
                return False
            if dv < _d(base):
                return False
            return not rend or dv <= _d(rend)
        return None

    if ka == kb:
        if ka == "d":
            return a[1] == b[1]
        if ka == "y":
            slot_a, slot_b = (a[1], a[2]), (b[1], b[2])
            abase, arend, bbase, brend = a[3], a[4], b[3], b[4]
        else:
            slot_a, slot_b = a[1], b[1]
            abase, arend, bbase, brend = a[2], a[3], b[2], b[3]
        # 循环间: 学期窗口不相交则不冲突(下学期新课表不受本学期影响)
        if arend and brend:
            pass
        if abase and bbase:
            ae = _d(arend) if arend else None
            be = _d(brend) if brend else None
            if ae and ae < _d(bbase):
                return False
            if be and _d(abase) > be:
                return False
        return slot_a == slot_b

    r = one_vs_loop(a[1], b) if ka == "d" else (one_vs_loop(b[1], a) if kb == "d" else None)
    if r is None:
        r = one_vs_loop(b[1], a) if kb == "d" else False
    return r


def time_conflicts(conn, item, exclude_id=None):
    """与 pending 任务的同时段冲突检测(循环 vs 单次按星期比对, 尊重循环截止)。
    只有带 due_time 的任务参与; 返回冲突任务 dict 列表。"""
    rng = _time_range(item)
    key = _slot_key(item)
    if not rng or not key:
        return []
    sql = ("SELECT id, title, due_date, due_time, end_time, recurring, recurrence_end FROM tasks "
           "WHERE status='pending' AND due_date IS NOT NULL AND due_date != '' "
           "AND due_time IS NOT NULL AND due_time != ''")
    params = ()
    if exclude_id:
        sql += " AND id != ?"
        params = (int(exclude_id),)
    out = []
    ns, ne = rng
    for r in conn.execute(sql, params):
        c = dict(r)
        crng = _time_range(c)
        ckey = _slot_key(c)
        if not crng or not ckey or not _key_match(key, ckey):
            continue
        cs, ce = crng
        if ns < ce and cs < ne:
            out.append(c)
    return out


def _d(s: str) -> datetime.date:
    return datetime.date.fromisoformat(s)


def validate_task(item, require_title=True):
    """校验任务字段(dict, 键可缺省)。
    返回 (clean_dict, err): clean 仅含合法非空键; err 为 None 表示通过。"""
    item = item or {}
    clean = {}

    title = str(item.get("title") or "").strip()
    if require_title:
        if not title:
            return None, "title 必填"
        clean["title"] = title[:120]
    elif title:
        clean["title"] = title[:120]

    due_date = str(item.get("due_date") or "").strip()
    if due_date and not DATE_RE.match(due_date):
        return None, "due_date 格式必须是 YYYY-MM-DD"
    if due_date:
        clean["due_date"] = due_date

    due_time = str(item.get("due_time") or "").strip()
    if due_time:
        mt = TIME_RE.match(due_time)
        if not mt or int(mt.group(1)) > 23:
            return None, "due_time 格式必须是 HH:mm"
        clean["due_time"] = f"{int(mt.group(1)):02d}:{mt.group(2)}"  # 8:00 → 08:00

    end_time = str(item.get("end_time") or "").strip()
    if end_time:
        mt = TIME_RE.match(end_time)
        if not mt or int(mt.group(1)) > 23:
            return None, "end_time 格式必须是 HH:mm"
        end_time = f"{int(mt.group(1)):02d}:{mt.group(2)}"
        if "due_time" in clean and end_time <= clean["due_time"]:
            return None, "end_time 必须晚于 due_time"
        clean["end_time"] = end_time

    if item.get("priority") not in (None, ""):
        try:
            pri = int(item["priority"])
        except (TypeError, ValueError):
            return None, "priority 必须是 1-5 的数字"
        if not 1 <= pri <= 5:
            return None, "priority 必须是 1-5 的数字"
        clean["priority"] = pri

    recurring = str(item.get("recurring") or "").strip().lower()
    if recurring and recurring not in RECUR_TYPES:
        return None, "recurring 必须是 daily/weekly/monthly/yearly"
    if recurring and not due_date:
        return None, "循环任务必须提供 due_date(第一次发生的日期)"
    if recurring:
        clean["recurring"] = recurring

    recurrence_end = str(item.get("recurrence_end") or "").strip()
    if recurrence_end and not DATE_RE.match(recurrence_end):
        return None, "recurrence_end 格式必须是 YYYY-MM-DD"
    if recurring and recurrence_end:
        clean["recurrence_end"] = recurrence_end

    category = str(item.get("category") or "").strip()
    if category:
        clean["category"] = category[:40]

    return clean, None


def create_task(conn, item, force=False):
    """创建任务(同名 pending 去重 + 同时段冲突检测)。返回 (task_id, err)。"""
    clean, err = validate_task(item)
    if err:
        return None, err
    title = clean["title"]
    dup = conn.execute("SELECT id FROM tasks WHERE title=? AND status='pending'", (title,)).fetchone()
    if dup:
        return None, f"同名任务已存在(#{dup['id']}), 不要重复创建"
    if not force:
        clashes = time_conflicts(conn, clean)
        if clashes:
            names = "; ".join(f"「{c['title']}」{c['due_date']} {c['due_time']}" for c in clashes[:3])
            return None, (f"时间冲突: {names}。请告知主人; 若主人明确要同时保留, "
                          "带 force=true 重新调用本工具即可绕过")
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, due_date, due_time, end_time, priority, recurring, recurrence_end, category)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (title, clean.get("due_date", ""), clean.get("due_time", ""), clean.get("end_time"),
             clean.get("priority", 3), clean.get("recurring"),
             clean.get("recurrence_end"), clean.get("category", "")),
        )
        return cur.lastrowid, None


def update_task(conn, task_id, fields, force=False):
    """按字段更新任务(空值/0 表示不改; recurring="none" 取消循环)。返回 (task_dict_after, err)。"""
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    if not row:
        return None, f"任务 #{task_id} 不存在"
    row = dict(row)

    sets, params = [], []
    f = fields or {}
    title = str(f.get("title") or "").strip()
    if title:
        sets.append("title=?"); params.append(title[:120])
    for k in ("due_date", "due_time", "end_time", "category"):
        v = str(f.get(k) or "").strip()
        if v:
            if k == "due_date" and not DATE_RE.match(v):
                return None, "due_date 格式必须是 YYYY-MM-DD"
            if k in ("due_time", "end_time"):
                mt = TIME_RE.match(v)
                if not mt or int(mt.group(1)) > 23:
                    return None, f"{k} 格式必须是 HH:mm"
                v = f"{int(mt.group(1)):02d}:{mt.group(2)}"
            sets.append(f"{k}=?"); params.append(v)
    if f.get("priority") not in (None, "", 0):
        try:
            pri = int(f["priority"])
        except (TypeError, ValueError):
            return None, "priority 必须是 1-5 的数字"
        if not 1 <= pri <= 5:
            return None, "priority 必须是 1-5 的数字"
        sets.append("priority=?"); params.append(pri)

    recurring = str(f.get("recurring") or "").strip().lower()
    if recurring == "none":
        sets.append("recurring=?"); params.append(None)
        sets.append("recurrence_end=?"); params.append(None)
    elif recurring:
        if recurring not in RECUR_TYPES:
            return None, "recurring 必须是 daily/weekly/monthly/yearly"
        new_date = str(f.get("due_date") or "").strip() or (row["due_date"] or "")
        if not new_date:
            return None, "循环任务必须有 due_date(第一次发生的日期)"
        sets.append("recurring=?"); params.append(recurring)
        sets.append("recurrence_end=?"); params.append(str(f.get("recurrence_end") or "").strip() or None)

    if not sets:
        return dict(row), None
    # 冲突检测: 用更新后的完整状态比对待改成的时段(排除自己)
    if not force:
        merged = dict(row)
        for s, v in zip(sets, params):
            merged[s.split("=")[0]] = v
        clashes = time_conflicts(conn, merged, exclude_id=int(task_id))
        if clashes:
            names = "; ".join(f"「{c['title']}」{c['due_date']} {c['due_time']}" for c in clashes[:3])
            return None, (f"时间冲突: {names}。请告知主人; 若主人明确要同时保留, "
                          "带 force=true 重新调用本工具即可绕过")
    params.append(int(task_id))
    with conn:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
    after = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    return dict(after), None


def complete_task(conn, task_id):
    """完成任务。返回 (title, err)。"""
    row = conn.execute("SELECT title FROM tasks WHERE id=? AND status='pending'", (int(task_id),)).fetchone()
    if not row:
        return None, f"找不到未完成任务 #{task_id}"
    with conn:
        conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (int(task_id),))
    return row["title"], None


def delete_task(conn, task_id):
    """删除任务(含打卡记录)。返回 (title, err)。"""
    row = conn.execute("SELECT title FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    if not row:
        return None, f"找不到任务 #{task_id}"
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (int(task_id),))
        conn.execute("DELETE FROM task_occurrence_completions WHERE task_id=?", (int(task_id),))
        conn.execute("DELETE FROM task_occurrence_overrides WHERE task_id=?", (int(task_id),))
    return row["title"], None


def expand_recurring(rows, start: str, end: str):
    """把循环母任务按区间展开成逐日实例(含 occurrence 打卡状态)。
    rows: 任务 dict 列表(含 due_date/due_time/recurring/recurrence_end/status/id/title/priority/description)
    返回: 展开后的实例列表(母任务原样保留在列表首位——非循环或已完成不展开)"""
    out = []
    s, e = _d(start), _d(end)
    for t in rows:
        if not t.get("recurring") or t.get("status") == "completed" or t.get("status") == "cancelled":
            out.append(dict(t))
            continue
        base = t.get("due_date")
        if not base:
            out.append(dict(t))
            continue
        rec = t["recurring"]
        r_end = t.get("recurrence_end")
        try:
            cur = _d(base)
        except Exception:
            out.append(dict(t))
            continue
        stop = min(e, _d(r_end)) if r_end else e
        # 从 max(base, start) 开始按频率推进
        if rec == "daily":
            step = datetime.timedelta(days=1)
            cur = max(cur, s)
        elif rec == "weekly":
            wd = cur.weekday()
            # 第一次发生不早于 start 的同 weekday
            delta = (s - cur).days
            cur = cur + datetime.timedelta(days=(delta // 7) * 7 if delta > 0 else 0)
            if cur < s:
                cur = cur + datetime.timedelta(days=7)
            step = datetime.timedelta(days=7)
        elif rec == "monthly":
            # 推进到 start 所在月及以后
            months = (s.year - cur.year) * 12 + (s.month - cur.month)
            if s.day >= cur.day or months > 0:
                pass
            cur = _add_months(cur, max(0, months))
            step = None  # 月度用 _add_months
        elif rec == "yearly":
            years = s.year - cur.year
            cur = _replace_year(cur, cur.year + max(0, years))
            step = None
        else:
            out.append(dict(t))
            continue

        guard = 0
        while cur <= stop and guard < 400:
            if cur >= s:
                inst = dict(t)
                inst["due_date"] = cur.isoformat()
                inst["is_occurrence"] = True
                out.append(inst)
            if rec == "daily":
                cur = cur + step
            elif rec == "weekly":
                cur = cur + step
            elif rec == "monthly":
                cur = _add_months(cur, 1)
            elif rec == "yearly":
                cur = _replace_year(cur, cur.year + 1)
            guard += 1
    return out


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # 日越界则钳到月末
    for day in range(d.day, 27, -1):
        try:
            return d.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    return d.replace(year=year, month=month, day=d.day)


def _replace_year(d: datetime.date, year: int) -> datetime.date:
    try:
        return d.replace(year=year)
    except ValueError:  # 2/29
        return d.replace(year=year, day=28)


def latest_occurrence_date(task: dict, today: str):
    """循环任务在 today(含)之前的最近一次发生日期; 未开始/非循环返回 None。
    直接按频率推算(不走 expand_recurring, 避免 daily 长任务撞 400 轮 guard)。"""
    base, rec = task.get("due_date"), task.get("recurring")
    if not base or not rec or base > today:
        return None
    d, t = _d(base), _d(today)
    if rec == "daily":
        return t.isoformat()
    if rec == "weekly":
        return (t - datetime.timedelta(days=(t - d).days % 7)).isoformat()
    if rec == "monthly":
        m = (t.year - d.year) * 12 + (t.month - d.month)
        cand = _add_months(d, m)
        if cand > t:
            m -= 1
            cand = _add_months(d, m)
        return cand.isoformat() if m >= 0 else None
    if rec == "yearly":
        y = t.year - d.year
        cand = _replace_year(d, d.year + y)
        if cand > t:
            y -= 1
            cand = _replace_year(d, d.year + y)
        return cand.isoformat() if y >= 0 else None
    return None


def occurrence_sync(conn, expanded):
    """给展开实例标记当日打卡状态(occurrence_done)"""
    if not expanded:
        return
    ids = [t["id"] for t in expanded if t.get("is_occurrence")]
    if not ids:
        return
    marks = {}
    q = ",".join("?" * len(ids))
    for r in conn.execute(f"SELECT task_id, date FROM task_occurrence_completions WHERE task_id IN ({q})", ids):
        marks[(r["task_id"], r["date"])] = True
    for t in expanded:
        if t.get("is_occurrence"):
            t["occurrence_done"] = bool(marks.get((t["id"], t["due_date"])))


def apply_occurrence_overrides(conn, expanded):
    """把单次改期(overrides)应用到展开实例上: 命中的实例日期/时间被挪到新位置(其他周不动)"""
    if not expanded:
        return
    ids = [t["id"] for t in expanded if t.get("is_occurrence")]
    if not ids:
        return
    q = ",".join("?" * len(ids))
    ovs = {}
    for r in conn.execute(
            f"SELECT task_id, orig_date, new_date, due_time, end_time FROM task_occurrence_overrides WHERE task_id IN ({q})",
            ids):
        ovs[(r["task_id"], r["orig_date"])] = dict(r)
    for t in expanded:
        if not t.get("is_occurrence"):
            continue
        ov = ovs.get((t["id"], t.get("due_date")))
        if ov:
            t["due_date"] = ov["new_date"]
            if ov["due_time"]:
                t["due_time"] = ov["due_time"]
            t["end_time"] = ov["end_time"] or t.get("end_time")
            t["moved"] = True


def expand_range(conn, rows, start: str, end: str, pad_days: int = 7):
    """标准展开入口: 循环展开(±pad_days 缓冲) → 应用单次改期 → 打卡同步 → 按区间过滤。
    缓冲解决"改期跨出原区间后在两侧视图都消失"的经典问题。"""
    s = (_d(start) - datetime.timedelta(days=pad_days)).isoformat()
    e = (_d(end) + datetime.timedelta(days=pad_days)).isoformat()
    expanded = expand_recurring(rows, s, e)
    apply_occurrence_overrides(conn, expanded)
    in_range = [t for t in expanded if start <= (t.get("due_date") or "") <= end]
    occurrence_sync(conn, in_range)
    return in_range


def move_occurrence(conn, task_id: int, orig_date: str, new_date: str, due_time: str = "", end_time: str = "", force: bool = False):
    """把循环任务的某一次单独挪到新日期/时间(写 override, 只影响那一天)。
    返回 (信息文本, err)。会做目标时段冲突检测(排除任务自身)。"""
    row = conn.execute("SELECT * FROM tasks WHERE id=? AND status='pending' AND recurring IS NOT NULL AND recurring != ''",
                       (int(task_id),)).fetchone()
    if not row:
        return None, "找不到该编号的循环任务"
    orig_date = orig_date.strip()
    new_date = new_date.strip()
    if not DATE_RE.match(orig_date) or not DATE_RE.match(new_date):
        return None, "日期格式必须是 YYYY-MM-DD"
    task = dict(row)
    dt = due_time.strip() or task.get("due_time") or ""
    et = end_time.strip() or task.get("end_time") or ""
    if dt:
        mt = TIME_RE.match(dt)
        if not mt or int(mt.group(1)) > 23:
            return None, "时间格式必须是 HH:mm"
        dt = f"{int(mt.group(1)):02d}:{mt.group(2)}"
    if et:
        mt = TIME_RE.match(et)
        if not mt or int(mt.group(1)) > 23:
            return None, "时间格式必须是 HH:mm"
        et = f"{int(mt.group(1)):02d}:{mt.group(2)}"
    # 目标时段冲突检测(排除任务自身; 伪条目=一次性)
    pseudo = {"title": task["title"], "due_date": new_date, "due_time": dt or None,
              "end_time": et or None, "recurring": None}
    if dt:
        clashes = time_conflicts(conn, pseudo, exclude_id=int(task_id))
        if clashes and not force:
            names = "; ".join(f"「{c['title']}」{c['due_date']} {c['due_time']}" for c in clashes[:3])
            return None, f"目标时段冲突: {names}。请告知主人; 若主人明确要同时保留, 可加 force 参数重来"
    with conn:
        conn.execute(
            "INSERT INTO task_occurrence_overrides (task_id, orig_date, new_date, due_time, end_time) VALUES (?,?,?,?,?) "
            "ON CONFLICT(task_id, orig_date) DO UPDATE SET new_date=excluded.new_date, due_time=excluded.due_time, end_time=excluded.end_time",
            (int(task_id), orig_date, new_date, dt or None, et or None))
    return f"「{task['title']}」的 {orig_date} 这一次已挪到 {new_date}" + (f" {dt}" + (f"-{et}" if et else "") if dt else "") + "(只影响这一次, 其他周照旧)", None


def toggle_occurrence(conn, task_id: int, date: str, completed: bool):
    with conn:
        if completed:
            conn.execute("INSERT OR IGNORE INTO task_occurrence_completions (task_id, date) VALUES (?, ?)", (task_id, date))
        else:
            conn.execute("DELETE FROM task_occurrence_completions WHERE task_id = ? AND date = ?", (task_id, date))


def snapshot_tasks(conn) -> str:
    """任务/打卡/单次改期的全量快照(JSON 字符串), 供撤销/回退"""
    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()]
    occ = [dict(r) for r in conn.execute("SELECT task_id, date FROM task_occurrence_completions").fetchall()]
    ovr = [dict(r) for r in conn.execute("SELECT * FROM task_occurrence_overrides").fetchall()]
    return json.dumps({"tasks": tasks, "occ": occ, "ovr": ovr}, ensure_ascii=False)


def restore_tasks(conn, payload: str):
    """从快照恢复(全量覆盖, 字段以快照为准)"""
    data = json.loads(payload)
    with conn:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM task_occurrence_completions")
        conn.execute("DELETE FROM task_occurrence_overrides")
        for t in data.get("tasks", []):
            cols = ", ".join(t.keys())
            ph = ", ".join("?" * len(t))
            conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({ph})", tuple(t.values()))
        for o in data.get("occ", []):
            conn.execute("INSERT OR IGNORE INTO task_occurrence_completions (task_id, date) VALUES (?, ?)",
                         (o["task_id"], o["date"]))
        for o in data.get("ovr", []):
            conn.execute(
                "INSERT OR REPLACE INTO task_occurrence_overrides (task_id, orig_date, new_date, due_time, end_time) "
                "VALUES (?, ?, ?, ?, ?)",
                (o["task_id"], o["orig_date"], o["new_date"], o.get("due_time"), o.get("end_time")))
