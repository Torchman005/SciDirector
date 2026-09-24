# SciDirector

> **不生成像素，而是生成「可验证的渲染计划」。**
>
> SciDirector 用多智能体把科普脚本编译成分镜，把每个分镜路由到最合适的**确定性渲染引擎**
> （Manim 渲染数学、D3/ECharts 渲染数据、代码高亮动画渲染代码），
> 再用视觉大模型（VLM）审查渲染出的帧，不合格就打回重做，直到科学严谨且流畅。

传统文生视频工具（Sora / Runway）讲不好科学：公式会写错、图表会张冠李戴、
镜头之间没有逻辑。根因不是模型不够强，而是**用像素空间去逼近符号系统**。
SciDirector 的做法是把自然语言意图**编译**为确定性图形程序，并在管线末端加一个**视觉验证器**。

---

## 目录

- [架构总览](#架构总览)
- [为什么是 Go + Python](#为什么是-go--python)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [核心概念](#核心概念)
- [开发指南](#开发指南)
- [阶段进度](#阶段进度)
- [文档索引](#文档索引)

---

## 架构总览

```
                         ┌──────────────────────────────────────────┐
   用户 / 审核员  ──HTTP──▶│  web (React + Vite)  分镜表 / 预览 / 打回 │
                         └───────────────┬──────────────────────────┘
                                         │ REST + WebSocket
                         ┌───────────────▼──────────────────────────┐
                         │  api (Go · Gin)                          │
                         │  · POST /api/v1/generate   接收脚本       │
                         │  · GET  /api/v1/jobs/:id   查询状态       │
                         │  · WS   /ws/jobs/:id       进度推送 + HITL│
                         └───────────────┬──────────────────────────┘
                                         │ Asynq Enqueue
                         ┌───────────────▼──────────────────────────┐
                         │  Redis  队列(asynq) + 状态快照 + 事件流   │
                         └───────────────┬──────────────────────────┘
                                         │ Asynq Consume
                         ┌───────────────▼──────────────────────────┐
                         │  worker (Go)  长链路编排                  │
                         │  · gRPC 流式驱动 Python 大脑              │
                         │  · 状态机推进（含熔断转人工）             │
                         │  · os/exec + errgroup 并发 ffmpeg 合成    │
                         └───────────────┬──────────────────────────┘
                                         │ gRPC (proto/scidirector/v1)
                         ┌───────────────▼──────────────────────────┐
                         │  ai (Python · FastAPI + gRPC + LangGraph)│
                         │  Director → Coder → Sandbox Render →     │
                         │  Critic(VLM) ──不合格──┐                  │
                         │      ▲                 │                  │
                         │      └── 修订反馈 ◀────┘  (循环，带重试上限)│
                         └──────────────────────────────────────────┘
```

**分层铁律**

| 关注点 | 归属 | 不允许 |
| --- | --- | --- |
| 任务调度、重试、并发、状态机、媒体合成 | **Go** | Python 侧不得直接调 ffmpeg 做最终合成 |
| 语义理解、代码生成、VLM 审查、RAG | **Python** | Go 侧不得内嵌 LLM SDK 做推理 |
| 跨语言数据契约 | `proto/scidirector/v1/*.proto` | 任何一侧私自改字段不加 proto 版本 |
| 大文件（MP4/PNG） | S3 兼容对象存储（RustFS）/ 共享卷，proto 只传**路径与元数据** | 禁止通过 gRPC 传输视频二进制 |

---

## 为什么是 Go + Python

单一语言做不好这件事：

| 维度 | Go 的优势 | Python 的优势 |
| --- | --- | --- |
| 并发与长连接 | Goroutine + 原生 WebSocket，万级连接成本低 | asyncio 生态相对弱 |
| 进程与媒体编排 | `os/exec` + `errgroup` 并发 ffmpeg，进程管理稳 | 子进程管理易泄漏 |
| 长链路任务可靠性 | Asynq 成熟的重试/超时/死信机制 | Celery 较重且状态难调 |
| AI 生态 | 几乎没有 | LangGraph / LangChain / VLM SDK 全在这里 |

分界线是 **`proto` 契约 + gRPC**。代价是两套构建与跨语言调试成本，
换来的是「并发与稳定性」和「AI 生态」两边的长板都能用上。

完整的设计取舍见 [`docs/DESIGN.md`](docs/DESIGN.md)。

---

## 快速开始

### 前置要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Go | ≥ 1.24 | 编译 api / worker（与 `backend/go.mod` 的 go 指令一致；镜像里的版本也必须满足它） |
| Python | ≥ 3.11 | AI 大脑（Manim 需要 LaTeX，见 `ai/Dockerfile`） |
| Node.js | ≥ 20 | 前端审核台 |
| Docker | ≥ 24 | 本地中间件（Redis / Postgres / RustFS） |
| ffmpeg | ≥ 6 | 抽帧与合成（本地开发需在 PATH 中） |
| protoc | ≥ 3.20 | 仅在修改 proto 后需要 |

### 方式一：容器全栈（推荐）

```bash
cp .env.example .env          # 至少填一个模型服务商的密钥；一个都不填则进入 mock 模式
docker compose up -d --build  # 首次构建会拉取 Manim/LaTeX，耗时较长
docker compose ps
```

启动后：

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| web | http://localhost:5173 | 分镜审核台 |
| api | http://localhost:8080 | REST + WebSocket |
| ai (HTTP) | http://localhost:8000/healthz | 健康与能力探测 |
| ai (gRPC) | localhost:50051 | Go ↔ Python |
| Grafana | http://localhost:3000 | 链路与指标看板（需 `--profile observability`） |
| rustfs console | http://localhost:9001 | 产物对象存储（S3 兼容） |

### 方式二：本地进程（迭代更快）

```bash
# 1) 只起中间件
make dev-infra          # = docker compose up -d redis postgres rustfs

# 2) 三个进程分别开三个终端
make dev-ai             # Python：FastAPI(8000) + gRPC(50051)
make dev-worker         # Go：Asynq 消费者 + ffmpeg 编排
make dev-api            # Go：REST + WebSocket (8080)

# 3) 前端
make dev-web
```

> 💡 **先跑一次 `make doctor`**：它打印本机解析到的 make / go / python（含解释器路径与
> 依赖是否就绪）/ node / docker / ffmpeg / protoc / 浏览器。`make` 目标报错时，
> 十有八九这一屏就能看出原因（最常见的是解释器名字或缺少 Python 依赖）。

> 💡 没有 Python 环境时执行 `make venv`：它在仓库内建 `.venv` 并安装 `ai/requirements.txt`，
> 之后 `make dev-ai` / `make test-python` 会**自动使用它**，不必每次指定 `PYTHON=`。

> ⚠️ **本地进程模式下根目录的 `.env` 原本不会被读到 —— 现已由 `make dev-*` 自动加载。**
> `make dev-api` 实际在 `backend/` 下运行（Go 根本不读 `.env` 文件，只读进程环境变量），
> `make dev-ai` 在 `ai/` 下运行（Python 的 `.env` 是**相对当前目录**解析的，找的是 `ai/.env`）。
> 根目录 `.env` 只在 `docker compose` 插值时生效。
> 四个 `dev-*` 目标现在会先加载根目录 `.env`（`scripts/load-env.sh`），
> 语义与 dotenv/compose 一致：**显式环境变量优先于文件**，因此
> `make dev-ai SCID_AI_HTTP_PORT=18000` 这类覆盖依然生效。
> 若变量一个都没配，表现是**静默进入 mock 模式**（内容全是占位），不会有报错。

### 不想用（或没有）make

`make` 不是必须的 —— 每个目标都对应一条直接命令，实测等价。**容器里没有 make**
（三个镜像都没装，它是开发机上的工具），`bash: make: command not found` 就是这个原因；
诊断工具也刻意做成了纯 sh（`scripts/doctor.sh`），因为"没有 make"正是最需要它的场景。

| 想做的事 | 不用 make 的命令 |
| --- | --- |
| 环境自检 | `sh scripts/doctor.sh` |
| 起中间件 | `docker compose up -d redis postgres rustfs` |
| AI 服务 | `sh -c '. ./scripts/load-env.sh; cd ai && python -m scidirector_ai.main'` |
| worker | `sh -c '. ./scripts/load-env.sh; cd backend && go run ./cmd/worker'` |
| api | `sh -c '. ./scripts/load-env.sh; cd backend && go run ./cmd/api'` |
| 前端 | `cd web && npm run dev` |
| Python 测试 | `cd ai && python -m pytest -q` |
| Go 测试 | `cd backend && go test ./internal/...` |
| 全栈容器 | `docker compose up -d --build` / `docker compose down` |
| 看日志 | `docker compose logs -f --tail=100` |
| 观测栈 | `docker compose --profile observability up -d` |
| 链路验证 | `python scripts/verify-observability.py --out-dir .tmp/obs-shots` |
| 建 Python 环境 | `python3 -m venv .venv && .venv/bin/pip install -r ai/requirements.txt` |

`make` 确实没装时：Debian/Ubuntu `apt-get install -y make`，Alpine `apk add make`。

Windows 环境先执行一次（把 Go 缓存与 Python 依赖固定在仓库内）：

```powershell
. .\scripts\dev-env.ps1
```

### 冒烟验证

```bash
# 1) 三段链路的健康检查
curl http://localhost:8000/healthz     # Python 大脑（含工具链明细）
curl http://localhost:8080/readyz      # Go 网关（检查 Redis 与 AI）

# 2) 只跑导演智能体（脚本 -> 分镜表），无需 Go 层
curl -X POST http://localhost:8000/v1/plan \
  -H 'Content-Type: application/json' \
  -d '{"job_id":"demo","raw_script":"从勾股定理出发，用面积法证明，再用数据说明其应用。","target_duration_sec":60}'

# 3) 提交一次完整生成
curl -X POST http://localhost:8080/api/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"raw_script":"从勾股定理出发……","target_duration_sec":90,"locale":"zh-CN"}'
```

不配置 `OPENAI_API_KEY` 时，Python 会自动进入 **mock 模式**（返回确定性占位结果），
因此本地联调与 CI 都不需要密钥、不产生费用。

---

## 手动测试

上面的命令一条条敲下来即可完成一次完整的链路验证。下面的顺序是**从内到外**的：
先验证 Python 大脑（不依赖 Go 与 Redis），再验证 Go 网关，最后验证整条链路。

> **Windows / PowerShell 提示**：本项目源码与接口都是 UTF-8。
> PowerShell 5.1 默认按系统代码页解码响应，中文会显示成乱码；
> 且 `curl` 是 `Invoke-WebRequest` 的别名，行为与真实 curl 不同。
> 因此**一律使用 `curl.exe`**，并且**不要把含中文的 JSON 内联写在命令行里**
> （引号与编码会被破坏，服务端收到的是一堆乱码）——用 `--data-binary '@file.json'`。

### 0. 启动依赖

```bash
# 方式 A（推荐）：容器起中间件
docker compose up -d redis postgres rustfs   # 注意：不是 minio（已于 v0.4.4 换成 RustFS）

# 方式 B：已有本机 Redis，直接起（Windows 原生 Redis 需要一份自己的配置）
redis-server .tmp/redis-manual.conf
```

### 1. Python 大脑（不需要 Redis）

```bash
. .\scripts\dev-env.ps1          # Windows；设置 GOPATH/PYTHONPATH 等
make dev-ai                       # 或 python -m scidirector_ai.main
```

| 检查 | 命令 | 预期 |
| --- | --- | --- |
| 存活 | `curl.exe http://127.0.0.1:8000/healthz` | `status=ok`，`capabilities` 含 `tool:manim=...` 等工具链明细 |
| 就绪 | `curl.exe http://127.0.0.1:8000/readyz` | 沙盒可用时 200；不可用时 503 |
| 配置 | `curl.exe http://127.0.0.1:8000/version` | 密钥字段已被掩码 |
| **导演智能体** | `curl.exe -X POST http://127.0.0.1:8000/v1/plan -H "Content-Type: application/json" --data-binary "@.tmp/plan-request.json"` | 返回分镜表；**时长之和落在目标 ±10%** |
| 阶段二流水线 | `curl.exe -X POST http://127.0.0.1:8000/v1/pipeline -H "Content-Type: application/json" --data-binary "@.tmp/plan-request.json"` | **200**，NDJSON 逐行流出 `plan`/`render`/`critique`/`done` 事件 |

### 2. gRPC 契约

```bash
python scripts/smoke-grpc.py                      # 默认 127.0.0.1:50051
```

逐项校验 `Health` / `PlanScript` / `RunPipeline` / `GenerateShot` / `CritiqueShot` / `ReviseShot`。
脚本会**自动探测**大脑所处的阶段：RPC 返回 `UNIMPLEMENTED` 即判定为阶段一并跳过调用，
全部实现则跑完整流水线断言，因此新旧两个版本都能复用同一份冒烟脚本。

### 3. Go 网关

```bash
make dev-api                      # 另一个终端
```

```bash
curl.exe http://127.0.0.1:8080/healthz    # 存活（不检查下游）
curl.exe http://127.0.0.1:8080/readyz     # 就绪：检查 Redis 与 AI 大脑
```

**负面用例**（这类用例最容易漏，务必都跑一遍）：

| 场景 | 命令 | 预期 |
| --- | --- | --- |
| 脚本过短 | `--data-binary "@.tmp/bad-request.json"` | `400 BAD_REQUEST`，detail 指向 `min` tag |
| 任务不存在 | `curl.exe http://127.0.0.1:8080/api/v1/jobs/job-not-exist` | `404 NOT_FOUND` |
| 接口不存在 | `curl.exe http://127.0.0.1:8080/api/v1/nope` | 结构化 `404`（不是纯文本） |
| WS 任务不存在 | `curl.exe http://127.0.0.1:8080/ws/jobs/job-not-exist` | `404`，**在升级前**拒绝 |

链路追踪：任一响应头都会回传 `X-Request-ID`，且与错误体里的 `trace_id` 一致。

### 4. 完整链路（Go + Redis + Python）

```bash
make dev-worker                   # 第三个终端
```

```bash
# 提交生成任务
curl.exe -X POST http://127.0.0.1:8080/api/v1/generate \
  -H "Content-Type: application/json" --data-binary "@.tmp/generate-request.json"

# 查状态与事件流（把 <job_id> 换成上一步返回的）
curl.exe http://127.0.0.1:8080/api/v1/jobs/<job_id>
curl.exe "http://127.0.0.1:8080/api/v1/jobs/<job_id>/events?after_id=0"
```

阶段二观察到的现象：

```
status:  PARTIAL   progress: 0.5
stat:    {"total":4,"approved":2,"failed":0,"awaiting_human":2,"in_progress":0}
events:  api      "任务已创建，等待导演智能体拆解脚本"
events:  plan     "导演智能体正在拆解脚本…"
events:  render   "AMBIENCE 镜头渲染完成"          <- 真实 MP4 落盘
events:  critique "审查通过"
events:  ...      MATH / DATA 镜头在第 3 次尝试后熔断 → AWAITING_HUMAN
```

任务终态是 `PARTIAL` 而非 `SUCCESS`，这是**正确行为**：本机没有 manim / d3 工具链，
依赖它们的镜头连续失败后熔断转人工，而可渲染的镜头照常完成。
**「引擎不可用」被当作镜头级失败，而不是任务级崩溃** —— 这正是熔断设计的意图。
完整验收明细见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

### 5. WebSocket 反馈闭环

```bash
python scripts/smoke-ws.py --api http://127.0.0.1:8080
```

验证 `snapshot`（加入即重放历史）、`ping/pong`、`resync`（断线增量补齐）、
以及新任务状态迁移的**实时推送**。

### 6. 清理

```bash
docker compose down               # 容器
# 本机进程：在各终端 Ctrl+C；或用 job_kill / Stop-Process 按端口结束
```

---

## 目录结构

```
SciDirector/
├── Agent.md                     # 智能体协作与工程规约（活文档，改架构必须同步）
├── docker-compose.yml           # 单机全栈编排
├── Makefile                     # 统一命令入口
├── docs/
│   ├── DESIGN.md                # 思路文档：设计决策、算法、取舍（活文档）
│   ├── API.md                   # REST / WebSocket / gRPC 契约
│   └── ROADMAP.md               # 阶段进度与验收标准
├── proto/scidirector/v1/        # 跨语言契约（唯一真源）
├── backend/                     # Go module
│   ├── cmd/api, cmd/worker      # 两个可执行入口
│   └── internal/
│       ├── config               # 配置装载与校验
│       ├── logging              # 结构化日志（字段与 Python 对齐）
│       ├── domain               # 领域模型 + 状态机（零外部依赖）
│       ├── store                # Redis 状态仓储（分布式锁 + 事件流）
│       ├── queue                # Asynq 客户端 / 服务端 / 任务载荷
│       ├── ai                   # Python 大脑的 gRPC 客户端
│       ├── media                # ffmpeg / ffprobe 封装
│       ├── httpapi              # Gin 路由、中间件、handler
│       ├── ws                   # WebSocket Hub（扇出 + 心跳 + 背压）
│       ├── worker               # Asynq handler 与流水线编排
│       ├── pbconv               # 领域模型 ↔ proto 转换
│       └── pb                   # protoc 生成代码（勿手改）
├── ai/                          # Python package: scidirector_ai
│   ├── scidirector_ai/
│   │   ├── config.py            # pydantic-settings 配置
│   │   ├── logging.py           # JSON 日志 + contextvar 链路绑定
│   │   ├── schemas.py           # 领域模型与业务校验
│   │   ├── pbconv.py            # 领域模型 ↔ proto 双向转换（含枚举映射）
│   │   ├── llm.py               # LLM/VLM 客户端（重试/结构化输出/成本/mock）
│   │   ├── media.py             # ffmpeg 封装、抽帧、环境镜头、字体探测
│   │   ├── renderer.py          # 渲染器抽象与确定性路由 + 就绪探测
│   │   ├── graph/               # LangGraph 状态、节点、图拓扑、checkpointer
│   │   ├── agents/              # 导演 / 编码 / 审查（提示词独立成 .md）
│   │   ├── rag/                 # Few-shot 优秀案例检索
│   │   ├── sandbox/             # 静态策略 + 进程隔离运行器 + Manim 沙盒
│   │   ├── service.py           # 业务门面（HTTP 与 gRPC 共用）
│   │   ├── grpc_server.py       # gRPC servicer + 契约适配
│   │   └── main.py              # FastAPI + gRPC 双栈进程入口
│   └── tests/
├── web/                         # React + Vite + TS 审核台
├── deploy/                      # redis.conf / postgres init / nginx
└── scripts/                     # dev-env.ps1 / gen-proto.ps1 / smoke-*.py
```

---

## 核心概念

### 场景标签与渲染路由

标签由**导演智能体**打，路由是**确定性映射**，模型无权自由指定引擎：

| 标签 | 语义 | 渲染引擎 | 为什么 |
| --- | --- | --- | --- |
| `MATH` | 公式、几何、符号运算 | Manim + LaTeX | 符号排版天然正确，不会出现"公式写错" |
| `DATA` | 图表、趋势、对比 | D3.js / ECharts | 数值来自脚本，坐标轴由代码决定 |
| `CODE` | 算法、代码演示 | 代码高亮动画 | 逐行高亮，与讲解节奏对齐 |
| `AMBIENCE` | 开场、过渡、总结 | 素材检索 / 渐变 | 无可计算内容，重在情绪与节奏 |

### 状态机

```
PENDING ──▶ GENERATING ──▶ RENDERING ──▶ CRITIQUING ──┬─▶ APPROVED
   ▲              │              │             │      │
   │              ▼              ▼             ▼      └─▶ RETRYING ──┐
   │            FAILED ◀─────────┴─────────────┘            │        │
   │              │                                          └────────┘
   │              └─▶ RETRYING（指数退避，上限后 → AWAITING_HUMAN）
   └───────────────── AWAITING_HUMAN ──(人工意见)──▶ RETRYING
```

合法迁移在 `backend/internal/domain/state.go` 中硬编码 —— **非法迁移返回错误而不是静默忽略**。
熔断（`AWAITING_HUMAN`）是成本控制的核心闸门：没有它，一个永远渲染不好的镜头会无限烧钱。

### 人类反馈闭环（HITL）

审核员在网页上打回某个镜头时，系统**只重做那一镜**（`task:shot:render`），不重跑全片。
一次全片生成可能耗费数十次渲染与 VLM 调用，按镜头重做把返工成本降到 1/N。

---

## 开发指南

### 常用命令

```bash
make help          # 查看全部命令
make proto         # 由 proto 生成 Go + Python 代码
make test          # 全量测试（Go 含 -race）
make lint          # go vet + gofmt + ruff + mypy
make build         # 编译 api / worker
make up / down     # 容器全栈
```

Windows 下若没有 make，直接使用脚本：

```powershell
. .\scripts\dev-env.ps1
.\scripts\gen-proto.ps1
cd backend; go build ./...; go test ./...
cd ..\ai; python -m pytest -q
```

### 修改跨语言契约的流程

**必须按顺序执行**，否则两侧会失配：

1. 改 `proto/scidirector/v1/*.proto`（只增不改 tag，不复用已删除的字段号）；
2. 运行 `make proto`（或 `.\scripts\gen-proto.ps1`）；
3. 调整两侧实现（Go 的 `internal/pbconv`，Python 的 `grpc_server.py`）；
4. 更新 `docs/API.md`；
5. 若涉及智能体职责或状态机，同步更新 `Agent.md`。

### 代码规范

- **Go**：错误必须 `%w` 包裹；`context` 贯穿所有 IO 且有超时；goroutine 必须有退出路径。
- **Python**：全量类型注解；Pydantic v2 做边界校验；异步入口不得阻塞事件循环。
- **通用**：契约先行；魔法数字具名并注释来源；禁止提交密钥。

详见 [`Agent.md`](Agent.md)。

---

## 阶段进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 一 | 环境与骨架（proto 契约、docker-compose、Go/Python 骨架、状态机） | ✅ 已完成 |
| 二 | Python 多智能体核心（编码 / 审查、沙盒渲染器、图拓扑、RAG） | ✅ 已完成 |
| 三 | Go 编排与媒体处理（并发收敛、转场调色、字幕、局部重渲、归档、队列观测） | ✅ 已完成 |
| 四 | 反馈闭环与前端（React 审核台、打回重做、断线重连、成分镜编辑） | ✅ 已完成 |
| 五 | 生产加固（可观测性、成本核算、多租户） | ⏳ 待办 |

> **阶段四已落地**：`web/` 从「连通性自检页」做成可用的分镜审核台 ——
> 分镜表、事件时间线、进度条、**始终可见的连接状态**；
> WS 断线重连以**快照为权威基线**（+ 按 seq 去重、空洞检测自动补发、带抖动退避）；
> 三种人工操作：**放行**（熔断出口）、**打回**（回灌意见重写代码）、
> **编辑文案**（`PATCH` 改 narration/visual_brief 后重做）。
>
> C4/C5 已用真实服务端到端验证：打回只影响目标镜头；
> 放行最后一个熔断镜头 → `compose_enqueued:true` → worker 合成 → `COMPLETED`。
> C1/C3 的数据通路已验证，浏览器人工目视验证待做。
>
> 前端本地开发：`cd web && npm install && npm run dev`（已配好 `/api` 与 `/ws` 代理），
> 测试 `npm test`，构建 `npm run build`。

> **阶段三已落地**：FFmpeg 并发收敛为**两道防线**（任务内 Worker Pool +
> 进程级全局单例闸门，防 OOM），进程生命周期加固（`WaitDelay` 兜住管道占用、
> 杀整棵进程树、单命令硬超时）；跨镜头转场（xfade/acrossfade）与统一规格调色
> （显式转换并打标色彩范围，解决 Manim 与浏览器录制的质感割裂）；字幕时间轴对齐
> 与软字幕封装；局部重渲染（只重渲某几秒并拼回原片）；产物归档（none/local/s3）
> 与本地卷保留策略；队列可观测（`GET /api/v1/queue/stats`）。
>
> **仍待办**：全片 TTS 配音 —— 本机无可用引擎且需先确定服务商。
> `MuxFinal` 的 `audioPath` 与音频转场链均已就绪，接入时无需改动调用契约。
>
> 阶段二为让流水线端到端可验证而落地的**沙盒执行器、三大渲染器、编码/审查智能体、
> LangGraph 图与 RAG** 仍是当前的内容生产核心。

各阶段的交付范围与验收明细见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

---

## 文档索引

| 文档 | 内容 | 读者 |
| --- | --- | --- |
| [`Agent.md`](Agent.md) | 智能体契约、状态机、目录职责、编码规范 | 所有贡献者（**先读这个**） |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 设计思路：为什么这么做、代价与取舍、风险与缓解 | 架构评审 / 新人 |
| [`docs/API.md`](docs/API.md) | REST / WebSocket / gRPC 契约与错误码 | 前后端 / 集成方 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 各阶段交付物与验收标准 | 项目管理 |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | 换机 / 新会话续作的引导提示词与环境清单 | 接手的人 |

---

## 许可

MIT，见 [`LICENSE`](LICENSE)。
