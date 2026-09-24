# =============================================================================
# SciDirector —— 统一命令入口
# -----------------------------------------------------------------------------
# 所有开发者（人类或 AI 智能体）只应通过本文件暴露的命令操作仓库，
# 以保证本地与 CI 行为一致。
# =============================================================================

SHELL := /bin/bash
ROOT  := $(CURDIR)

# ---------------------------------------------------------------------------
# 工具链版本锚定（本地开发用，Docker 内由镜像决定）
# ---------------------------------------------------------------------------
GO              ?= go
# Python 解释器解析。
#
# 为什么不能只写 `python`：**很多系统里根本没有这个名字**（Debian/Ubuntu 只装
# `python3`，本机就是这样），于是所有 Python 目标都以
# `/bin/bash: python: command not found` + Error 127 失败 ——
# 报错看起来像"make 坏了"，实际只是解释器名字不对。
#
# 顺序：显式覆盖 > 当前激活的 venv > 仓库内的 .venv/ai/.venv > python3 > python。
# 优先 venv 是必须的：系统 python3 通常**没有本项目依赖**（fastapi/grpc/langgraph），
# 退到它会得到一长串 ModuleNotFoundError，而不是一句"该装依赖了"。
#
# **必须是绝对路径**：多处目标会先 `cd ai` 再用它（`cd ai && $(PYTHON) -m ...`），
# 相对路径在 cd 之后就失效了 —— 现象是 `.venv/bin/python: No such file or directory`
# 加 Error 127，看起来像 venv 坏了，实际只是路径没写全。
PYTHON          ?= $(shell \
	if [ -x "$$VIRTUAL_ENV/bin/python" ]; then echo "$$VIRTUAL_ENV/bin/python"; \
	elif [ -x .venv/bin/python ]; then echo "$(CURDIR)/.venv/bin/python"; \
	elif [ -x ai/.venv/bin/python ]; then echo "$(CURDIR)/ai/.venv/bin/python"; \
	elif command -v python3 >/dev/null 2>&1; then command -v python3; \
	elif command -v python >/dev/null 2>&1; then command -v python; \
	else echo python; fi)
PROTOC          ?= protoc
GOPATH_LOCAL    := $(ROOT)/.gocache/gopath
GOMODCACHE_LOCAL:= $(GOPATH_LOCAL)/pkg/mod
GOCACHE_LOCAL   := $(ROOT)/.gocache/build

# 把 Go 的缓存固定在仓库内：既隔离全局环境，也让 sandbox 环境可写可用。
export GOPATH           := $(GOPATH_LOCAL)
export GOMODCACHE       := $(GOMODCACHE_LOCAL)
export GOCACHE          := $(GOCACHE_LOCAL)
export GOFLAGS          := -mod=mod
export GOTELEMETRY      := off
export PATH             := $(GOPATH_LOCAL)/bin:$(PATH)

GO_MODULE := github.com/itJinYu/SciDirector/backend
PROTO_DIR := proto
PROTO_INC := $(PROTO_DIR)
PROTO_FILES := $(shell find $(PROTO_DIR) -name '*.proto')

.PHONY: help doctor venv
help: ## 显示所有可用命令
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ===========================================================================
# 契约：proto 代码生成
# ===========================================================================
.PHONY: proto proto-tools proto-go proto-python
proto: proto-go proto-python ## 生成 Go + Python 两端的 gRPC 代码

proto-tools: ## 安装 protoc 插件（protoc-gen-go / protoc-gen-go-grpc / grpcio-tools）
	$(GO) install google.golang.org/protobuf/cmd/protoc-gen-go@latest
	$(GO) install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
	$(PYTHON) -m pip install --upgrade grpcio-tools

proto-go: ## 由 proto 生成 Go 代码
	@mkdir -p backend/internal/pb
# 没有系统 protoc 时回退到 **Python 的 grpc_tools 自带 protoc**（`pip install grpcio-tools`
# 就带了它）。为什么值得写这个回退：很多机器（含本机）只装了 grpcio-tools，
# 没有独立的 protoc 二进制，于是 `make proto` 直接报 `protoc: command not found` ——
# 而所需的 protoc 其实就在手边。Go 插件仍需 `make proto-tools` 装好。
	@if command -v $(PROTOC) >/dev/null 2>&1; then \
		$(PROTOC) -I $(PROTO_INC) \
			--go_out=backend/internal/pb --go_opt=paths=source_relative \
			--go-grpc_out=backend/internal/pb --go-grpc_opt=paths=source_relative \
			$(PROTO_FILES); \
	else \
		echo "未找到 $(PROTOC)，回退到 grpc_tools 自带的 protoc"; \
		$(PYTHON) -m grpc_tools.protoc -I $(PROTO_INC) \
			--plugin=protoc-gen-go=$$(command -v protoc-gen-go || echo $$(go env GOPATH)/bin/protoc-gen-go) \
			--plugin=protoc-gen-go-grpc=$$(command -v protoc-gen-go-grpc || echo $$(go env GOPATH)/bin/protoc-gen-go-grpc) \
			--go_out=backend/internal/pb --go_opt=paths=source_relative \
			--go-grpc_out=backend/internal/pb --go-grpc_opt=paths=source_relative \
			$(PROTO_FILES); \
	fi

proto-python: ## 由 proto 生成 Python 代码
	@mkdir -p ai/scidirector_ai/pb
	$(PYTHON) -m grpc_tools.protoc -I $(PROTO_INC) \
		--python_out=ai/scidirector_ai/pb \
		--pyi_out=ai/scidirector_ai/pb \
		--grpc_python_out=ai/scidirector_ai/pb \
		$(PROTO_FILES)

# ===========================================================================
# 构建
# ===========================================================================
.PHONY: build build-go build-web
build: build-go ## 构建全部可编译产物

build-go: ## 编译 Go 的 api / worker
	cd backend && $(GO) build -trimpath -o bin/scid-api    ./cmd/api
	cd backend && $(GO) build -trimpath -o bin/scid-worker ./cmd/worker

build-web: ## 构建前端产物
	cd web && npm ci && npm run build

# ===========================================================================
# 环境自检与准备
# ===========================================================================
.PHONY: doctor venv
doctor: ## 检查本机工具链（make 目标报错时先跑这个）
# 逻辑在 scripts/doctor.sh 里，纯 sh 实现 —— 因为**最常见的 make 报错就是没有 make**，
# 而 `make doctor` 在那种情况下自己就跑不起来。`sh scripts/doctor.sh` 等价。
	@sh scripts/doctor.sh

venv: ## 创建仓库内 .venv 并安装 Python 依赖
	@if [ -x .venv/bin/python ]; then echo "已存在 .venv，跳过创建"; \
	else python3 -m venv .venv && echo "已创建 .venv"; fi
	@.venv/bin/python -m pip install -q --upgrade pip
	@.venv/bin/python -m pip install -q -r ai/requirements.txt
	@echo "✓ 完成。之后 make dev-ai / test-python 会自动使用 .venv（无需再指定 PYTHON）"

# ===========================================================================
# 本地开发
# ===========================================================================
# Python 依赖自检。
#
# 为什么要有：解析出来的解释器可能是系统 python3（没有本项目依赖），
# 那时直接跑会抛一大段 ModuleNotFoundError 栈，看着像代码坏了。
# 这里先探一次，失败就给一句能照做的指引。开销约 40ms，可接受。
PY_DEPS = fastapi, grpc, langgraph, pydantic, openai
define require_python_deps
	@$(PYTHON) -c "import $(PY_DEPS)" 2>/dev/null || { \
		echo "✗ 解释器 $(PYTHON) 缺少项目依赖。"; \
		echo "  修法（任选其一）："; \
		echo "    python3 -m venv .venv && .venv/bin/pip install -r ai/requirements.txt"; \
		echo "    或指定已有解释器：make $@ PYTHON=/path/to/venv/bin/python"; \
		exit 1; }
endef

.PHONY: dev-infra dev-api dev-worker dev-ai dev-web
dev-infra: ## 仅启动依赖中间件（redis / postgres / rustfs）
	docker compose up -d redis postgres rustfs

# 本地开发时**先加载根目录 .env**。
#
# 为什么必须显式做这件事：`make dev-api` 实际在 `backend/` 下运行，而 **Go 根本不读
# `.env` 文件**（只读进程环境变量）；`make dev-ai` 在 `ai/` 下运行，而 Python 的
# `env_file=".env"` 是**相对当前目录**解析的，找的是 `ai/.env`。
# 于是根目录那个 `.env` 在本地模式下**完全不起作用** —— 表现是"照着文档配了、却
# 静默进了 mock 模式"（内容全是占位，没有任何报错）。
#
# 用 shell 的 `.` 而不是 make 的 `include`：make 会把值里的 ` #` 当注释、
# 也不支持多行值，而 `.env` 是给 shell/docker 用的格式。`set -a` 让读进来的变量
# 自动导出给子进程。文件不存在时安静跳过（CI 里就没有 .env）。
# 本地开发时加载根目录 .env（详见 scripts/load-env.sh 里的说明）。
#
# 一句话：根目录 .env 只在 `docker compose` 插值时生效，本地跑进程时**不读它** ——
# 于是"照着 .env.example 配好、make dev-ai 起服务"会静默进 mock 模式。
# 解析规则（显式环境变量优先、去成对引号、保留值里的 #）都在那个脚本里，
# 且有独立用例覆盖；这里只负责调用，避免在 Makefile 里写一长串带 $$ 转义的 shell。
LOAD_ENV = . ./scripts/load-env.sh;
dev-api: ## 本地运行 Go API（自动加载根目录 .env）
	@$(LOAD_ENV) cd backend && $(GO) run ./cmd/api

dev-worker: ## 本地运行 Go Worker（自动加载根目录 .env）
	@$(LOAD_ENV) cd backend && $(GO) run ./cmd/worker

dev-ai: ## 本地运行 Python AI 服务（自动加载根目录 .env）
	$(require_python_deps)
	@$(LOAD_ENV) cd ai && $(PYTHON) -m scidirector_ai.main

dev-web: ## 本地运行前端
	@$(LOAD_ENV) cd web && npm run dev

# ===========================================================================
# 冒烟测试（手动联调）
# ===========================================================================
# 这两个脚本需要对应的服务已经在跑（dev-ai / dev-api）。
.PHONY: smoke smoke-grpc smoke-ws smoke-web
smoke: smoke-grpc smoke-ws ## 依次冒烟 gRPC 与 WebSocket

smoke-grpc: ## 冒烟 Python 大脑的 gRPC 契约（需先 make dev-ai）
	cd $(ROOT) && $(PYTHON) scripts/smoke-grpc.py

smoke-ws: ## 冒烟 Go 网关的 WebSocket 闭环（需先 make dev-api）
	cd $(ROOT) && $(PYTHON) scripts/smoke-ws.py

# C1/C3 的浏览器验证：真实 Chromium 驱动真实全栈，产出截图供人工目视。
# 需要 playwright（`pip install playwright`）与一个可用的 Chromium
# （用 SCID_CHROME 指定；脚本也会自动在常见路径里找）。
# 断网演练默认用 set_offline，但它**不会**断开已建立的 WebSocket，
# 因此要真正切断实时通道请传 API_RESTART_CMD：
#   make smoke-web API_RESTART_CMD=./scripts/api-ctl.sh
smoke-web: ## 冒烟审核台（C1 实时推进 + C3 断网重连；需全栈在跑）
	cd $(ROOT) && $(PYTHON) scripts/smoke-web.py --shots .tmp/shots \
		$(if $(API_RESTART_CMD),--api-restart-cmd "$(API_RESTART_CMD)",)

# 可观测性验收：在真实浏览器里确认「一次生成请求的 span 树」能在 Grafana 上看到。
# 需要先起观测栈与全栈：
#   docker compose --profile observability up -d
#   SCID_OTEL_ENDPOINT=127.0.0.1:4317 make dev-api   # 并让 worker/ai 也带上这个变量
# 判定依据是 DOM 文本（模型读不了图），截图另存供人复核。
# 也支持指定链路：make verify-obs TRACE_ID=<32位hex>
verify-obs: ## 验证 Grafana 上能看到跨服务的 span 树（需观测栈 + 全栈在跑）
	cd $(ROOT) && $(PYTHON) scripts/verify-observability.py \
		$(if $(TRACE_ID),--trace-id "$(TRACE_ID)",) --out-dir .tmp/obs-shots

# ===========================================================================
# 质量
# ===========================================================================
.PHONY: test test-go test-python test-failover test-s3 lint fmt verify-obs
test: test-go test-python ## 全量测试

# 竞态检测默认开启（CI 上必须跑）。Windows 本地若缺少 race runtime DLL
# （表现为 exit status 0xc0000139），用 `make test-go RACE=` 关闭即可。
RACE ?= -race

test-go: ## Go 单元测试（默认含竞态检测）
	cd backend && $(GO) test $(RACE) -count=1 ./...

test-python: ## Python 单元测试
	$(require_python_deps)
	cd ai && $(PYTHON) -m pytest -q

# B5 的「执行中被断线」用例需要 1~2 分钟：Asynq 的租约（30s）与 recoverer 轮询（60s）
# 都是硬编码的，压不下去。因此**不放进默认目标** —— 长耗时用例混进默认目标，
# 最后一定会被整体加 skip 或在超时后被忽略，等于没写。
# 需要 redis-server 可执行文件；若不在 PATH 上，用 SCID_TEST_REDIS_BIN 指定。
test-failover: ## 故障注入测试：Redis 断线后恢复（需 1~2 分钟）
	cd backend && SCID_TEST_REDIS_FAILOVER=1 $(GO) test -timeout 10m -count=1 -run TestB5 -v ./internal/queue/

# s3 归档的端到端验证需要**真实的 S3 端点**：MinIO / SeaweedFS / moto 等任意
# S3 兼容实现都行（用例只依赖 S3 API，不绑定具体产品）。未配置时自动跳过。
# 同时覆盖 archive 包（归档器本身）与 worker 包（真实归档器接进 finalizeArtifacts）——
# 两半各自通过不等于接起来通过，本地路径能否被归档器真正读到只有合起来才知道。
test-s3: ## s3 归档端到端验证（需 SCID_TEST_S3_ENDPOINT / ACCESS_KEY / SECRET_KEY）
	cd backend && $(GO) test -count=1 -v -run 'TestS3|TestFinalizeArtifactsUploadsToRealS3' ./internal/archive/ ./internal/worker/

lint: ## 静态检查
	cd backend && $(GO) vet ./...
	cd backend && $(GO) fmt ./...
	cd ai && $(PYTHON) -m ruff check scidirector_ai || true
	cd ai && $(PYTHON) -m mypy scidirector_ai || true

fmt: ## 格式化
	cd backend && $(GO) fmt ./...
	cd ai && $(PYTHON) -m ruff format scidirector_ai || true

# ===========================================================================
# 容器编排
# ===========================================================================
.PHONY: up down logs ps clean
up: ## 全栈启动
	docker compose up -d --build

down: ## 停止全栈（保留数据卷）
	docker compose down

logs: ## 跟踪日志
	docker compose logs -f --tail=100

ps: ## 查看服务状态
	docker compose ps

clean: ## 清理构建/运行产物（保留 git 跟踪文件）
	rm -rf .gocache backend/bin .data ai/**/__pycache__ web/dist
