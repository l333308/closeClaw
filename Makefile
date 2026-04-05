.PHONY: infra infra-down infra-logs reset-data check-compose run build agents agents-stop copy-validate lint fmt tidy help

# ─── 变量 ────────────────────────────────────────────────────────────────────

BINARY        := bin/orchestrator
GO_MAIN       := ./cmd/orchestrator
COMPOSE_CMD   := $(shell if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then printf '%s' 'docker compose'; elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then printf '%s' 'podman compose'; elif command -v podman-compose >/dev/null 2>&1; then printf '%s' 'podman-compose'; fi)
AGENT_ENV     := no_proxy=localhost,127.0.0.1,::1 NO_PROXY=localhost,127.0.0.1,::1 HF_ENDPOINT=$${HF_ENDPOINT:-https://hf-mirror.com} HF_HOME=$(PWD)/.cache/huggingface TRANSFORMERS_CACHE=$(PWD)/.cache/huggingface/transformers HF_HUB_DOWNLOAD_TIMEOUT=$${HF_HUB_DOWNLOAD_TIMEOUT:-60} HF_HUB_ETAG_TIMEOUT=$${HF_HUB_ETAG_TIMEOUT:-30}

# ─── 基础设施（Podman） ────────────────────────────────────────────────────────

## check-compose: 检查可用的 compose 命令
check-compose:
	@if [ -z "$(COMPOSE_CMD)" ]; then \
		echo "未找到可用的 compose 命令。请安装并配置以下任意一种：docker compose / podman compose / podman-compose"; \
		exit 1; \
	fi
	@echo "使用 compose 命令: $(COMPOSE_CMD)"

## infra: 启动 RabbitMQ / Redis / Chroma
infra: check-compose
	$(COMPOSE_CMD) -f podman-compose.yml up -d
	@echo "等待服务就绪..."
	@sleep 5
	@echo "✓ RabbitMQ  http://localhost:15672  (closeclaw/closeclaw)"
	@echo "✓ Redis     localhost:6379"
	@echo "✓ Chroma    http://localhost:8000"

## infra-down: 停止并清理容器
infra-down: check-compose
	$(COMPOSE_CMD) -f podman-compose.yml down

## infra-logs: 查看基础设施日志
infra-logs: check-compose
	$(COMPOSE_CMD) -f podman-compose.yml logs -f

## reset-data: 停止基础设施并清空所有持久化数据（含向量数据）
reset-data: check-compose
	@echo "这会删除 RabbitMQ / Redis / Chroma 的持久化数据（包括 Chroma 向量数据）"
	$(COMPOSE_CMD) -f podman-compose.yml down -v
	@echo "✓ 基础设施数据已清空；如需继续使用，请重新执行 make infra"

# ─── Go Orchestrator ─────────────────────────────────────────────────────────

## build: 编译 Go Orchestrator
build:
	@mkdir -p bin
	go build -o $(BINARY) $(GO_MAIN)

## run: 构建并启动 Orchestrator（前台）
run: build
	./$(BINARY)

## tidy: 整理 Go 依赖
tidy:
	go mod tidy

## fmt: 格式化 Go 代码
fmt:
	gofmt -w .

## lint: 检查 Go 代码（需安装 golangci-lint）
lint:
	golangci-lint run ./...

# ─── Python Agents ───────────────────────────────────────────────────────────

## venv: 创建 Python 虚拟环境并安装依赖
venv:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

## agents: 在后台启动所有 Python agents
agents:
	@mkdir -p logs
	$(AGENT_ENV) PYTHONPATH=$(PWD) .venv/bin/python agents/crawler/main.py   >> logs/crawler.log  2>&1 &
	$(AGENT_ENV) PYTHONPATH=$(PWD) .venv/bin/python agents/dedup/main.py     >> logs/dedup.log    2>&1 &
	$(AGENT_ENV) PYTHONPATH=$(PWD) .venv/bin/python agents/analyzer/main.py  >> logs/analyzer.log 2>&1 &
	$(AGENT_ENV) PYTHONPATH=$(PWD) .venv/bin/python agents/writer/main.py    >> logs/writer.log   2>&1 &
	$(AGENT_ENV) PYTHONPATH=$(PWD) .venv/bin/python agents/review/main.py    >> logs/review.log   2>&1 &
	$(AGENT_ENV) PYTHONPATH=$(PWD) .venv/bin/python agents/video/main.py     >> logs/video.log    2>&1 &
	$(AGENT_ENV) PYTHONPATH=$(PWD) .venv/bin/python agents/publisher/main.py >> logs/publisher.log 2>&1 &
	@echo "✓ all agents started. logs/ 目录可查看各 agent 日志"

## agents-stop: 停止所有 Python agents
agents-stop:
	@pkill -f "agents/crawler/main.py"   2>/dev/null || true
	@pkill -f "agents/dedup/main.py"     2>/dev/null || true
	@pkill -f "agents/analyzer/main.py"  2>/dev/null || true
	@pkill -f "agents/writer/main.py"    2>/dev/null || true
	@pkill -f "agents/review/main.py"    2>/dev/null || true
	@pkill -f "agents/video/main.py"     2>/dev/null || true
	@pkill -f "agents/publisher/main.py" 2>/dev/null || true
	@echo "✓ all agents stopped"

## trigger: 手动触发一次 pipeline
trigger:
	curl -s -X POST http://localhost:8080/jobs/trigger | python3 -m json.tool

## jobs: 列出所有 Job
jobs:
	curl -s http://localhost:8080/jobs | python3 -m json.tool

## copy-validate: 运行固定热点样例，生成文案验收结果
copy-validate:
	PYTHONPATH=$(PWD) .venv/bin/python scripts/run_copy_quality_samples.py

## open-latest: 用系统播放器打开最新生成的视频
open-latest:
	@latest=$$(ls -t output/videos/*.mp4 2>/dev/null | grep -v thumb | head -1); \
	if [ -z "$$latest" ]; then echo "no video found"; else open "$$latest" && echo "opened: $$latest"; fi

# ─── 帮助 ──────────────────────────────────────────────────────────────────

help:
	@grep -E '^## ' Makefile | sed 's/## /  /'
