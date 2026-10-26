# astrbot_task 识图(v0.3, 移植自 Teaskks 识图管线):
# 消息里的图片 → 下载 → 视觉多模态模型 → 文字描述 → 注入对话

import base64
import json
import urllib.request


def load_image_b64(src: str) -> str:
    """URL 或本地路径 → base64"""
    if src.startswith("http://") or src.startswith("https://"):
        data = urllib.request.urlopen(src, timeout=20).read()
    else:
        with open(src, "rb") as f:
            data = f.read()
    return base64.b64encode(data).decode()


def describe_image(src: str, api_url: str, api_key: str, model: str, timeout: int = 25) -> str:
    """下载/读取图片并调用视觉模型, 返回描述文本; 失败抛异常由调用方降级"""
    b64 = load_image_b64(src)
    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请详细描述这张图片: 场景、物体、以及逐字提取所有可见文字。若含表格/清单/课表/题目, 结构化转述全部内容, 不要遗漏数字。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "temperature": 0.2,
        "max_tokens": 4000,
    }).encode()
    req = urllib.request.Request(
        api_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    msg = resp.get("choices", [{}])[0].get("message", {})
    return (msg.get("content") or "").strip()
