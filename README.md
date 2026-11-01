![header](https://capsule-render.vercel.app/api?type=waving&height=150&color=gradient:5B8C8F,8fb8ba&section=header&text=%E6%97%A5%E7%A8%8B%E7%AE%A1%E7%90%86&fontSize=44&fontColor=ffffff)

<img align="right" src="logo.png" width="110">

[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10+-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-v4.29.0+-orange)
![Stars](https://img.shields.io/github/stars/p0TATo-bit/astrbot_task?style=flat&label=Stars)
![Release](https://img.shields.io/github/v/release/p0TATo-bit/astrbot_task)

# 日程管理 (astrbot_task)

**任务管理 / 长期记忆 / 情绪陪伴** —— 为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的伴侣管家插件。纯 Python + SQLite,零第三方依赖,29 个 LLM 工具自然语言调用。

她不只是日程表:有长期记忆(会记住你的偏好与约定)、有情绪与好感(三条关系值无上限成长)、能操作网页替你做题、能检索论文、能主动关心你——日常三句话回复,长内容自动整理成文档发给你。

> [!IMPORTANT]
> 本插件基于较新版本的 AstrBot(v4.29+)开发,推荐使用最新版 AstrBot 以获得最佳体验。
> 对话本体由 AstrBot 驱动:请先配置好 LLM 提供商(OpenAI 兼容格式)与人格提示词。

## 功能特性

- **任务管理**:自然语言创建/修改/**批量导入课表**/完成/删除,支持循环(daily/weekly/monthly/yearly)、截止时间、优先级、分类;时段**冲突检测**,危险操作需二次确认,支持快照撤销(30 步)
- **长期记忆**:重要事实/偏好/计划自动存储,扩散激活召回(关键词 IDF + 多跳 + 情绪调制),每 20 轮周期总结回灌,45 天半衰期 + 金环晋升(被想起 5 次进入持久层)
- **情绪与好感**:PAD 情绪状态 + 三条关系值(无上限,满 1 金色渐变,之后「爱」),随对话自然变化,并注入每轮回复的上下文
- **手机聊天入口**:内置 selfchat 平台适配器,手机浏览器/PWA 直连机器人,提醒与主动关心推送到手机
- **网页操作**:无头浏览器代理,替你打开网页、点击、填写、提交并带回结果(可选依赖 playwright)
- **学术论文检索**:arXiv + OpenAlex 双源,标题/摘要/PDF 链接
- **替主人做题**:MBTI 等测试检索记忆作依据,逐题作答并计分
- **知识库读写**:与 AstrBot 原生知识库互通,重要资料可写入供全局检索
- **长回复自动转文档**:回复超过阈值自动整理成 markdown 文件发送,日常短消息照常
- **联网与实用工具**:Bing 搜索 / 网页正文读取 / ast 白名单精确计算 / Open-Meteo 天气 / 识图(需配视觉模型)
- **定时推送体系**:AI 定时提醒 / 临近开始提醒 / 过期汇总 / AI 主动关心(概率随好感) / 每日内容推送 / 计划推送 / 自动备份

## 安装

1. **插件市场**:AstrBot 面板 → 插件市场 → 搜索「日程管理」
2. **从文件安装**:下载 Release 或仓库 zip → 面板 → 插件管理 → 从文件安装
3. 配置 LLM 提供商与人格(AstrBot 面板-人格管理,插件不内置人设),即可对话

数据存放于 `data/plugin_data/astrbot_task/`(数据库/备份/笔记),**更新插件不会丢失数据**。

## 内嵌 WebUI

插件启动后自动开启管理面板(默认 `http://127.0.0.1:6187`):

- **仪表盘 / 日历**:本周 7 天视图 + 月视图,循环任务展开,点击打卡
- **任务**:时间分组折叠、搜索筛选、编辑弹窗
- **记忆**:列表(分页)+ 搜索 + 编辑 + 关联图(力导向/同心圆/时间轴三模式,可全屏)+ 导出/导入
- **对话 / 情绪 / 文档 / 监控**:未总结对话、情绪面板与日志、她写的文档、调用与 token 监控
- **设置**:20+ 项配置网页直改(推送/人格提醒/回复风格/管理模型/知识库写入等)

手机访问:`webui_host` 改 `0.0.0.0`(注意安全),或启用内置 selfchat 手机入口。

## 指令

- `/任务 列表` / `添加 <标题> [日期] [时间]` / `完成` / `删除 <编号>` — 常规管理
- `/任务 模型` — 查看管理模型与可用 Provider ID
- `/任务 撤销 [步数]` — 回退最近变更 / `/任务 清空` — 清空全部(可撤销)
- `/任务 帮助`

## 29 个 LLM 工具

create_task / create_tasks(课表批量导入) / update_task / list_tasks / complete_task / delete_task / move_occurrence(单次改期) / undo_task_change(撤销) / store_memory / search_memory / current_time / set_reminder / write_note / list_notes / read_note / send_document(长内容发文件) / calculate / weather / add_quiz / quiz_me / answer_quiz(背诵抽查) / set_daily_content / web_search / open_url / search_papers(论文检索) / take_test(替做题) / browser_task(操作网页) / browser_login(扫码登录) / kb_write(写知识库)

插件内部任务(总结/提取/意图/口吻/计划)统一走「管理模型」配置,不占用以上工具。

## 与 Teaskks 的关系

本插件是 Teaskks(Node 版任务管理)能力的独立移植,数据各自独立;Teaskks 已退役保留,可随时回退。

## License

[MIT](LICENSE) © p67at0

![访客计数](https://count.getloli.com/get/@p67at0-astrbot-task?theme=moebooru)
