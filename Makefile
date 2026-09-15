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
PYTHON          ?= python
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

.PHONY: help
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
	$(PROTOC) -I $(PROTO_INC) \
		--go_out=backend/internal/pb --go_opt=paths=source_relative \
		--go-grpc_out=backend/internal/pb --go-grpc_opt=paths=source_relative \
		$(PROTO_FILES)

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
# 本地开发
# ===========================================================================
.PHONY: dev-infra dev-api dev-worker dev-ai dev-web
dev-infra: ## 仅启动依赖中间件（redis / postgres / minio）
	docker compose up -d redis postgres minio minio-init

dev-api: ## 本地运行 Go API
	cd backend && $(GO) run ./cmd/api

dev-worker: ## 本地运行 Go Worker
	cd backend && $(GO) run ./cmd/worker

dev-ai: ## 本地运行 Python AI 服务（HTTP + gRPC 双栈）
	cd ai && $(PYTHON) -m scidirector_ai.main

dev-web: ## 本地运行前端
	cd web && npm run dev

# ===========================================================================
# 冒烟测试（手动联调）
# ===========================================================================
# 这两个脚本需要对应的服务已经在跑（dev-ai / dev-api）。
.PHONY: smoke smoke-grpc smoke-ws
smoke: smoke-grpc smoke-ws ## 依次冒烟 gRPC 与 WebSocket

smoke-grpc: ## 冒烟 Python 大脑的 gRPC 契约（需先 make dev-ai）
	cd $(ROOT) && $(PYTHON) scripts/smoke-grpc.py

smoke-ws: ## 冒烟 Go 网关的 WebSocket 闭环（需先 make dev-api）
	cd $(ROOT) && $(PYTHON) scripts/smoke-ws.py

# ===========================================================================
# 质量
# ===========================================================================
.PHONY: test test-go test-python lint fmt
test: test-go test-python ## 全量测试

# 竞态检测默认开启（CI 上必须跑）。Windows 本地若缺少 race runtime DLL
# （表现为 exit status 0xc0000139），用 `make test-go RACE=` 关闭即可。
RACE ?= -race

test-go: ## Go 单元测试（默认含竞态检测）
	cd backend && $(GO) test $(RACE) -count=1 ./...

test-python: ## Python 单元测试
	cd ai && $(PYTHON) -m pytest -q

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
