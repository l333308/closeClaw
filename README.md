# closeClaw

AI 热点自动化 Agent 系统。全自动完成「抓取 → 去重 → 分析 → 文案 → 视频 → 发布通知」全流程，目标 2 小时内产出可发布短视频。

## 架构

```
热点抓取 → 向量去重 → 热点分析 → 文案生成 → 视频合成 → 发布通知
 Crawler    Dedup     Analyzer    Writer      Video      Publisher
   └──────────────────────── RabbitMQ ──────────────────────────┘
                         Go Orchestrator
                        （状态机 + HTTP API）
                              Redis
                           （Job 状态）
                             Chroma
                          （embedding 去重）
```

**Go Orchestrator** 负责调度和状态管理，**Python Agents** 负责每个阶段的具体执行，两者通过 RabbitMQ 解耦通信。

### 目录结构

```
closeClaw/
├── cmd/orchestrator/        # Go 入口
├── internal/
│   ├── config/              # 配置加载（环境变量）
│   ├── pipeline/            # DAG 状态机
│   ├── queue/               # RabbitMQ 封装
│   └── cache/               # Redis 封装
├── agents/
│   ├── base.py              # Python 公共工具
│   ├── crawler/             # Tavily API 抓取热点
│   ├── dedup/               # Chroma 向量去重
│   ├── analyzer/            # Qwen 分析热点
│   ├── writer/              # Claude 生成文案（双源）
│   ├── video/               # edge-tts + FFmpeg 合成视频
│   └── publisher/           # Webhook 通知人工发布
├── shared/schema/           # 共享消息结构（Go + Python）
├── podman-compose.yml       # 基础设施（RabbitMQ / Redis / Chroma）
├── Makefile
├── requirements.txt
└── .env.example
```

### Pipeline 各阶段

| 阶段 | Agent | 延迟预算 | 关键依赖 |
|---|---|---|---|
| 热点抓取 | crawler | 10 min | Tavily Search API |
| 向量去重 | dedup | 异步 | Chroma + sentence-transformers |
| 热点分析 | analyzer | 10 s | Qwen（DashScope） |
| 文案生成 | writer | 5 s | Claude 双源（any / geek）|
| 视频合成 | video | 20 min | edge-tts + FFmpeg |
| 发布通知 | publisher | 5 min | 飞书 / 企业微信 Webhook |

## 依赖

- **Go** 1.23+
- **Python** 3.11+
- **Podman** + **podman-compose**
- **FFmpeg**（视频合成阶段需要）

## 配置

复制 `.env.example` 为 `.env` 并填写 API Keys：

```bash
cp .env.example .env
```

关键配置项：

```ini
# 抓取
TAVILY_API_KEY=...

# 分析（阿里云 DashScope Qwen）
QWEN_API_KEY=...
QWEN_MODEL_35_PLUS=qwen3.5-plus

# 文案（Claude 双源，主源失败自动切换备用源）
CLAUDE_BASE_URL_ANY=...
CLAUDE_API_KEY_ANY=...
CLAUDE_MODEL_ANY=claude-opus-4-6

CLAUDE_BASE_URL_GEEK=...
CLAUDE_API_KEY_GEEK=...
CLAUDE_MODEL_GEEK=claude-sonnet-4-6

# 发布通知（飞书 / 企业微信 / 自定义）
WEBHOOK_TYPE=feishu
PUBLISH_WEBHOOK_URL=...
```

## 启动

### 1. 启动基础设施

```bash
make infra
```

启动后可访问：
- RabbitMQ 管理界面：http://localhost:15672（用户名/密码：`closeclaw`）
- Chroma API：http://localhost:8000

### 2. 安装 Python 依赖

```bash
make venv
```

### 3. 启动 Go Orchestrator

```bash
make run
```

Orchestrator 启动后会每小时自动触发一次 pipeline，同时监听 `:8080`。

### 4. 启动 Python Agents

```bash
make agents
```

所有 agent 在后台运行，日志写入 `logs/` 目录：

```
logs/crawler.log
logs/dedup.log
logs/analyzer.log
logs/writer.log
logs/video.log
logs/publisher.log
```

停止所有 agents：

```bash
make agents-stop
```

## HTTP API

Orchestrator 提供以下管理接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/jobs/trigger` | 手动触发一次 pipeline |
| `GET` | `/jobs` | 列出所有 Job ID |
| `GET` | `/jobs/{id}` | 查看单个 Job 详情 |
| `POST` | `/jobs/{id}/advance` | Agent 回调，推进到下一阶段 |
| `POST` | `/jobs/{id}/fail` | Agent 报告当前阶段失败 |
| `GET` | `/healthz` | 健康检查 |

常用快捷命令：

```bash
make trigger   # 手动触发 pipeline
make jobs      # 列出所有 Job
echo > logs/*.log # 清空 log 文件内容
```

## Makefile 命令

```
make infra         启动 RabbitMQ / Redis / Chroma
make infra-down    停止并清理容器
make infra-logs    查看基础设施日志
make build         编译 Go Orchestrator
make run           构建并启动 Orchestrator（前台）后续可看到 任务pipeline执行情况
make venv          创建 Python 虚拟环境并安装依赖
make agents        后台启动所有 Python agents（另一个终端）
make agents-stop   停止所有 Python agents
make trigger       手动触发一次 pipeline
make jobs          列出所有 Job
make tidy          整理 Go 依赖
make fmt           格式化 Go 代码
make lint          检查 Go 代码（需安装 golangci-lint）
```
