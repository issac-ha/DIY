# 项目规则

本文件约束 AI 在本项目中的所有行为。修改前先读，冲突时以本文件为准。

## 项目定位

AI 硬件价格分析 Agent，用于求职展示。数据来源是 B站硬件 UP 主的「显卡日报」视频，
通过视觉识别截图表 + 音频转写，抽取每日显卡价格，入库并提供比价、趋势、性价比查询。

## 环境约定

| 项目 | 值 |
|---|---|
| Python | `D:\python\python3.10.6\python.exe` |
| 项目根 | `d:\code\project1\ai-vue\diy-agent` |
| LangChain | 1.4.x，`create_agent(model=llm, tools=TOOLS, system_prompt=...)` |
| 文本模型 | DeepSeek `deepseek-chat` |
| 视觉模型 | 火山方舟 `doubao-seed-2-1-lite-260915` |
| 语音转写 | faster-whisper `small`，CPU int8 |
| 数据库 | MySQL 8（生产，容器）/ SQLite（本地测试），由 `DATABASE_URL` 决定 |
| 视频处理 | `imageio-ffmpeg` 自带二进制，不依赖系统 ffmpeg |

## 数据链路（顺序不可随意调换）

1. 拉取 UP 主视频列表，只保留标题含「显卡日报」的视频
2. ffmpeg 按固定间隔抽帧（默认 3 秒，720P）
3. dHash 感知哈希去重，丢弃画面无变化的帧
4. 视觉模型分批读取（每批 2 张，检测截断后逐帧重读），提取型号、平台、价格
5. faster-whisper 转写音频，保留分段与时间戳，不使用 B站 CC 字幕
6. LLM 抽取结构化价格 JSON
7. 基准价异常过滤，偏离阈值直接丢弃
8. 入库并生成 Markdown 报告

## 代码规范

- 不使用 emoji
- 注释解释「为什么」，不复述「做了什么」
- 不引入未使用的依赖、不对一次性逻辑做抽象
- 注释、日志、报告统一中文
- 删除旧方案时彻底删除，不保留注释掉的死代码

## 数据准确性要求

- 价格必须有明确数字来源，禁止推测和换算
- 视觉识别模糊的单元格直接跳过，不猜
- 平台化名必须映射：某东/东/狗东 → jd，某宝/宝 → taobao，某多/夕 → pdd，某Yin/音 → douyin
- 某天所有视频都没有有效价格时，标记 skipped，不写入推测数据
- 字幕或转写为空时只用简介兜底，禁止用评论区内容总结视频主题

## 已知坑（不要重复踩）

- LangChain 1.4 的 `create_agent` 参数是 `model=` 和 `system_prompt=`，旧参数已废弃
- B站 `videoshot` 帧间隔固定 5-6 秒且只有 480x270，需要精确间隔时必须用 ffmpeg
- DASH 音视频流请求必须带 `Referer: https://www.bilibili.com/`
- whisper 模型首次下载在国内很慢，命令前置 `HF_ENDPOINT=https://hf-mirror.com`
- prompt 里含 JSON 花括号示例时不能用 `str.format()`，改用 `str.replace()`
- B站 CC 字幕经常为空，且评论内容与视频主题常无关，不能作为主数据源
- `app.mount("/")` 必须放在所有 API 路由定义之后，否则接口 404

## 错题本（修改采集链路前必读）

`docs/错题本.md` 记录了 17 个真实错误的因果链、硬性不变量、症状决策表和回归清单。
修改 `app/crawler/**`、`app/db/**`、`seed_data.py`、`app/data/benchmark_prices.json` 之前先读，
改完执行该文件第六节的回归命令，不能只看代码。

最容易复发的三条：

- 视觉输出必须"一行一个型号 + 连续行号"，退回"一平台一行"会让模型沿用上一行型号，
  把相邻型号合并，而且没有任何结构能发现
- 型号严格匹配基准目录，禁止模糊匹配；目录里没有就跳过并告警
- 入库日期取视频发布日期，不用 `date.today()`

## 教学要求

- 每讲完一个实现，补充 1-2 个面试常见问题及参考答案
- 先讲思路和取舍，再给实现
- 涉及性能、成本、准确率的改动，说明量化依据而不是感觉

## 尚未完成

- 全量历史视频补采
- scheduler 定时采集
- Redis 缓存、CI/CD 流水线
- RAG 语义检索
- QQ 机器人接入
