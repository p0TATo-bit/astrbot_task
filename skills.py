# astrbot_task 技能库: 笔记文档 / 精确计算 / 天气查询 (纯标准库, 零第三方依赖)
import ast
import datetime
import io
import json
import math
import os
import re

# ---------- 笔记文档(markdown 文件, 存数据目录 notes/) ----------
_ILLEGAL = re.compile(r'[\\/:*?"<>|\.\.]')


def notes_dir(db_path):
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "notes")
    os.makedirs(d, exist_ok=True)
    return d


def safe_title(title):
    t = _ILLEGAL.sub("", str(title or "").strip()).strip(". ")
    if not t:
        raise ValueError("标题不能为空")
    return t[:60]


def note_path(db_path, title):
    return os.path.join(notes_dir(db_path), safe_title(title) + ".md")


def write_note(db_path, title, content):
    """写/覆盖笔记。返回 (路径, 是否覆盖了已有文件)"""
    p = note_path(db_path, title)
    existed = os.path.exists(p)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(str(content or "").replace("\r\n", "\n"))
    return p, existed


def list_notes(db_path):
    d = notes_dir(db_path)
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".md"):
            p = os.path.join(d, f)
            out.append({"title": f[:-3], "size": os.path.getsize(p),
                        "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
    return out


def read_note(db_path, title):
    p = note_path(db_path, title)
    if not os.path.exists(p):
        raise ValueError("笔记不存在")
    return io.open(p, encoding="utf-8").read()


def delete_note(db_path, title):
    p = note_path(db_path, title)
    if os.path.exists(p):
        os.remove(p)


# ---------- 精确计算(ast 白名单求值, 绝不 eval 任意代码) ----------
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "asin": math.asin, "acos": math.acos, "atan": math.atan,
          "log": math.log, "log2": math.log2, "log10": math.log10,
          "exp": math.exp, "abs": abs, "round": round, "floor": math.floor,
          "ceil": math.ceil, "degrees": math.degrees, "radians": math.radians,
          "factorial": math.factorial}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def calculate(expr):
    """精确计算数学表达式, 返回 int/float。白名单: + - * / // % ** 括号、函数、常数; 其他一律拒绝"""
    expr = str(expr or "").strip()
    if not expr or len(expr) > 500:
        raise ValueError("表达式为空或过长")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp):
            l, r = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return l + r
            if isinstance(n.op, ast.Sub):
                return l - r
            if isinstance(n.op, ast.Mult):
                return l * r
            if isinstance(n.op, ast.Div):
                return l / r
            if isinstance(n.op, ast.FloorDiv):
                return l // r
            if isinstance(n.op, ast.Mod):
                return l % r
            if isinstance(n.op, ast.Pow):
                return l ** r
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.UAdd):
                return +v
            if isinstance(n.op, ast.USub):
                return -v
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("表达式包含不支持的元素")

    r = ev(ast.parse(expr, mode="eval"))
    if isinstance(r, float):
        if math.isnan(r) or math.isinf(r):
            raise ValueError("结果不是有限数")
        if abs(r - round(r)) < 1e-12 and abs(r) < 1e15:
            r = int(round(r))
    return r


# ---------- 天气(Open-Meteo: 免费免 Key, 地理编码+预报两跳) ----------
_WMO = {0: "晴", 1: "基本晴", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
        51: "毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
        66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
        95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹"}


def weather(city):
    """查询城市今明两天天气, 返回中文文本。阻塞网络 IO, 调用方放线程"""
    import urllib.parse
    import urllib.request

    def get(url):
        return json.loads(urllib.request.urlopen(url, timeout=10).read().decode("utf-8"))

    city = str(city or "").strip()[:40]
    if not city:
        raise ValueError("城市名不能为空")
    g = get("https://geocoding-api.open-meteo.com/v1/search?name="
            + urllib.parse.quote(city) + "&count=1&language=zh&format=json")
    hits = g.get("results") or []
    if not hits:
        raise ValueError(f"找不到城市「{city}」")
    h = hits[0]
    name = h.get("name") or city
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=auto&forecast_days=2" % (h["latitude"], h["longitude"]))
    daily = (get(url).get("daily") or {})
    times = daily.get("time") or []
    wcodes = daily.get("weather_code") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    prep = daily.get("precipitation_probability_max") or []
    lines = [f"{name}({h.get('country', '')}{h.get('admin1', '')})"]
    for i, ds in enumerate(times[:2]):
        desc = _WMO.get(wcodes[i] if i < len(wcodes) else None, "未知")
        lo = tmin[i] if i < len(tmin) else "?"
        hi = tmax[i] if i < len(tmax) else "?"
        pp = prep[i] if i < len(prep) else "?"
        lines.append(f"{'今天' if i == 0 else '明天'}: {desc}, {lo}~{hi}°C, 降水概率 {pp}%")
    return "\n".join(lines)


# ---------- 网络搜索(Bing, 推送与工具共用) ----------
def web_search(query, limit=6):
    """Bing 搜索, 返回 [{title, url, snippet}](阻塞网络 IO, 调用方放线程)"""
    import urllib.parse
    import urllib.request
    query = str(query or "").strip()
    if not query:
        raise ValueError("搜索词不能为空")
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "count": 10})
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9"})
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")

    def strip_tags(s):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()

    out = []
    for b in re.findall(r'<li class="b_algo"[\s\S]*?</li>', html)[:8]:
        link = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"', b)
        title = re.search(r'<h2[^>]*>([\s\S]*?)</h2>', b)
        snippet = re.search(r'<p[^>]*>([\s\S]*?)</p>', b)
        if link and title:
            out.append({"title": strip_tags(title[1])[:80], "url": link[1],
                        "snippet": strip_tags(snippet[1])[:150] if snippet else ""})
        if len(out) >= limit:
            break
    if not out:
        raise ValueError("搜索没有返回结果")
    return out


def search_papers(query, source="arxiv", limit=5):
    """检索学术论文, 返回 [{title, authors, year, venue, abstract, link}](阻塞网络 IO, 调用方放线程)
    arxiv: 计算机/物理/数学预印本(export.arxiv.org); openalex: 全学科期刊(api.openalex.org)"""
    import json as _json
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET
    query = str(query or "").strip()
    limit = max(1, min(10, int(limit or 5)))
    if not query:
        raise ValueError("检索词不能为空")
    src = (source or "arxiv").lower()

    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "astrbot_task/1.0 (paper search)"})
        return urllib.request.urlopen(req, timeout=25).read()

    if src == "openalex":
        u = ("https://api.openalex.org/works?search=" + urllib.parse.quote(query)
             + f"&per-page={limit}&select=display_name,authorships,publication_year,primary_location,doi,abstract_inverted_index")
        data = _json.loads(_get(u).decode("utf-8", "ignore"))
        out = []
        for w in data.get("results", []):
            authors = ", ".join((a.get("author") or {}).get("display_name", "")
                                for a in (w.get("authorships") or [])[:4])
            inv = w.get("abstract_inverted_index") or {}
            pos = {}
            for word, idxs in inv.items():
                for i in idxs:
                    pos[i] = word
            abstract = " ".join(pos[i] for i in sorted(pos))[:400] if pos else ""
            loc = (w.get("primary_location") or {}).get("source") or {}
            out.append({"title": (w.get("display_name") or "")[:120], "authors": authors,
                        "year": w.get("publication_year") or "",
                        "venue": (loc.get("display_name") or "")[:60],
                        "abstract": abstract, "link": w.get("doi") or ""})
        if not out:
            raise ValueError("没有检索到结果")
        return out

    u = ("https://export.arxiv.org/api/query?search_query=all:" + urllib.parse.quote(query)
         + f"&start=0&max_results={limit}&sortBy=relevance")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(_get(u).decode("utf-8", "ignore"))
    out = []
    for e in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", (e.findtext("a:title", "", ns) or "")).strip()
        abstract = re.sub(r"\s+", " ", (e.findtext("a:summary", "", ns) or "")).strip()[:400]
        authors = ", ".join((a.findtext("a:name", "", ns) or "") for a in e.findall("a:author", ns)[:4])
        link = ""
        for l in e.findall("a:link", ns):
            if l.get("title") == "pdf":
                link = l.get("href") or ""
                break
        if not link:
            link = (e.findtext("a:id", "", ns) or "")
        out.append({"title": title[:120], "authors": authors,
                    "year": (e.findtext("a:published", "", ns) or "")[:4],
                    "venue": "arXiv", "abstract": abstract, "link": link})
    if not out:
        raise ValueError("没有检索到结果")
    return out
