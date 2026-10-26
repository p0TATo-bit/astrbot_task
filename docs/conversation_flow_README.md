# astrbot_task + AstrBot 话术生成流程图 (Mermaid)

## 使用方法

### 1. 直接在 GitHub/GitLab/Notion/Obsidian 渲染
复制 `.mmd` 文件内容粘贴到支持 Mermaid 的编辑器即可。

### 2. 导入 draw.io (diagrams.net)
1. 打开 https://app.diagrams.net/
2. `文件` → `导入自` → `设备` → 选 `conversation_flow.mmd`
3. 或：`扩展` → `Mermaid` → 粘贴代码 → `插入`

### 3. CLI 转 SVG/PNG (需安装 @mermaid-js/mermaid-cli)
```bash
npx -y @mermaid-js/mermaid-cli -i conversation_flow.mmd -o conversation_flow.svg
# 或 PNG
npx -y @mermaid-js/mermaid-cli -i conversation_flow.mmd -o conversation_flow.png -b transparent
```

---

## 流程图结构说明

### 四大层级 (配色对应)

| 层级 | 颜色 | 说明 |
|------|------|------|
| **AstrBot 核心** | 🟢 绿 | Provider管理、LLM请求/响应、发回用户 — **你不控制，只配置** |
| **插件钩子注入** | 🟠 橙 | `on_llm_request`：每轮把「情绪+记忆+时间」追加到 `system_prompt` — **核心增强点** |
| **后台异步 LLM 调用** | 🔵 蓝 | 四类后台调用都走 `_llm_call` 代理 AstrBot provider：**意图分析/周期总结/情绪校准/推送口吻** |
| **SQLite 数据层** | 🟣 粉 | 任务/记忆/情绪/配置 — 持久化 |

### 关键数据流

```
用户消息
    ↓
AstrBot 构建请求 (system_prompt = 面板人格 + 历史)
    ↓
🔶 on_llm_request 钩子拦截
    ├─ emotion.build_context() → 自然语言情绪描述
    ├─ memory.associative_recall() → 相关记忆文本
    └─ now_str() → 当前时间
    ↓
三段拼接追加到 system_prompt
    ↓
发给大模型 (NVIDIA API 等)
    ↓
模型一次性看到：人格 + 情绪 + 记忆 + 时间 → 生成回复
    ↓
AstrBot 发回用户
```

### 后台四类异步调用 (互不阻塞主流程)

| 调用 | 触发 | Prompt 关键词 | 产出落地 |
|------|------|--------------|----------|
| `_intent_call` | 每轮 | "内部检索规划器" | `recall[]` 给检索 / `emotion{}` 给微调 |
| `periodic_summary` | 20轮 | "第一人称总结 JSON" | `summaries` + `facts→memories` |
| `apply_adjust(校准)` | 10轮 | "情绪校准 JSON" | `emotion` 表 ±0.15 |
| `persona_notify/_gen_plan` | 定时 | "人格口吻" | 纯文本 `send_message` |

---

## 关键代码定位 (想改参数找这里)

| 需求 | 文件:行 |
|------|---------|
| 换模型/提供商 | AstrBot 面板 → 模型提供商 |
| 调整情绪微调上限 | `emotion.py:127` `cap=0.08` |
| 调整情绪校准上限 | `emotion.py:127` `cap=0.15` |
| 调整总结触发轮数 | `memory.py:395` `>= 20` |
| 调整召回条数 | `main.py:382` `limit=5` |
| 调整意图分析超时 | `main.py:399` `timeout=8.5` |
| 加新后台 LLM 调用 | 仿照 `_llm_call` 写新方法，加 `asyncio.wait_for` 超时保护 |