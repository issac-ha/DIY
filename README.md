# AI 硬件价格分析 Agent

从 B 站硬件日报视频里自动提取显卡、CPU、内存、硬盘的每日价格，清洗入库，并提供对话式查询与趋势分析。

面向 AI Agent 开发岗求职的实战项目：完整走通「多模态抽取 → 规则清洗 → 结构化入库 → Agent 工具调用 → 接口与前端」链路。

## 解决什么问题

硬件价格每天在变，散落在京东、淘宝、拼多多、抖音、闲鱼五个渠道，人工比价成本高。

本项目不写脆弱的电商爬虫，而是从固定 UP 主的日报视频中抽帧识图，把画面里的价格表转成结构化数据，从而获得稳定的每日价格序列。

## 处理链路

```
B站视频列表 → 标题过滤(显卡日报) → 音频下载 + ASR 转写
→ ffmpeg 每 3 秒抽帧 → dHash 去重
→ 视觉模型逐帧读表(一张表一行) → LLM 结构化抽取(严格匹配型号目录)
→ 平台白名单/型号归一/异常过滤
→ 按视频发布日期幂等入库 → 转写分段持久化 → 日报
```

音频转写只提供涨跌方向和新闻素材，具体价格必须从画面读，两者互补。

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 1. 配置，填入 BILIBILI_COOKIE、VISION_API_KEY、LLM_API_KEY
copy .env.example .env

# 2. 启动 API 和 MySQL
docker compose up -d

# 3. 浏览器打开
# http://127.0.0.1:8000
```

`app` 和 `mysql` 都只绑定 `127.0.0.1`，局域网不可见。数据存在命名卷 `mysql_data` 里，删容器不丢数据。

### 方式二：本地 Python

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置
copy .env.example .env
# 填入 BILIBILI_COOKIE、VISION_API_KEY、LLM_API_KEY
# DATABASE_URL 默认 SQLite，要连 MySQL 就改成 mysql+pymysql://...

# 3. 初始化基准数据（可选，会清空现有数据）
python seed_data.py

# 4. 采集一期视频
python -m app.crawler.bilibili_pipeline --videos 1 --before 2026-11-17 --no-asr

# 5. 启动服务
uvicorn app.api:app --reload
```

### 常用命令

```bash
# 扫码更新 B站 Cookie（过期时执行）
python -m app.crawler.bilibili_login

# 区间采集，断点续跑，已完成的日期自动跳过
python -m app.crawler.bilibili_pipeline --from 2026-05-01

# 只补转写，不动价格
python -m app.crawler.bilibili_pipeline --transcripts-only --from 2026-05-01

# 跑测试
python -m pytest
```

## 目录结构

```
diy-agent/
├── app/
│   ├── api.py                 接口：对话、趋势、产品、报告、采集
│   ├── config.py              环境变量配置
│   ├── agent/                 LangChain Agent 与提示词
│   ├── crawler/               采集链路（抽帧、识图、抽取、ASR、编排）
│   ├── db/                    模型、仓储层、连接配置
│   ├── data/                  基准价格目录
│   ├── tools/                 Agent 可调用的工具（含只读 SQL）
│   └── web/                   纯 HTML + ECharts 前端
├── tests/                     pytest 用例
├── docs/                      需求文档、项目文档、错题本
├── reports/                   每期采集报告
├── snapshots/                 抽帧结果
├── Dockerfile
├── docker-compose.yml
├── seed_data.py               基准数据初始化
├── pytest.ini
└── requirements.txt
```

## 文档

| 文档 | 内容 |
|---|---|
| [需求文档](docs/需求文档.md) | 目标、功能需求、验收标准、里程碑 |
| [项目文档](docs/项目文档.md) | 架构、数据模型、关键设计决策、接口、配置 |
| [错题本](docs/错题本.md) | 18 个真实错误的因果链、不变量、排障决策表、回归清单 |

修改采集链路前先读错题本，改完执行它的回归清单。

## 当前状态

**数据**

- 覆盖 2026-05-01 至今，已入库 180+ 天、约 3 万条价格记录
- 五类价格：显卡、CPU、内存、硬盘、闲鱼二手
- 每期视频同时产出结构化价格、行情摘要、硬件新闻、转写分段

**功能**

- 前端行情看板：按日期/品类/成色筛选，多平台比价，点击型号看 7/30 天走势
- 对话页：Agent 自主调用工具，信息不足时追问，给 2-3 套方案并标注超支
- 只读 SQL 工具：模型可根据问题自行查询，服务端强制只读事务

**工程化**

- MySQL 8 替代 SQLite，连接参数按方言分支
- Docker Compose 一键启动，app 与 MySQL 均只绑回环地址
- pytest 17 个用例，覆盖入库幂等、涨跌默认值、品类校验、接口鉴权
- API Key 鉴权开关，默认关闭以适配单机部署

**待完成**

- 全量补采剩余日期
- CI/CD 流水线与定时调度
- RAG：在行情摘要与转写分段上做语义检索
