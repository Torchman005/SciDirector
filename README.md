# SciDirector · 科学视频导演

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
| 大文件（MP4/PNG） | MinIO / 共享卷，proto 只传**路径与元数据** | 禁止通过 gRPC 传输视频二进制 |

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
| Go | ≥ 1.23 | 编译 api / worker |
| Python | ≥ 3.11 | AI 大脑（Manim 需要 LaTeX，见 `ai/Dockerfile`） |
| Node.js | ≥ 20 | 前端审核台 |
| Docker | ≥ 24 | 本地中间件（Redis / Postgres / MinIO） |
| ffmpeg | ≥ 6 | 抽帧与合成（本地开发需在 PATH 中） |
| protoc | ≥ 3.20 | 仅在修改 proto 后需要 |

### 方式一：容器全栈（推荐）

```bash
cp .env.example .env          # 按需填入 OPENAI_API_KEY；不填则自动进入 mock 模式
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
| minio console | http://localhost:9001 | 产物对象存储 |

### 方式二：本地进程（迭代更快）

```bash
# 1) 只起中间件
make dev-infra          # 或 docker compose up -d redis postgres minio minio-init

# 2) 三个进程分别开三个终端
make dev-ai             # Python：FastAPI(8000) + gRPC(50051)
make dev-worker         # Go：Asynq 消费者 + ffmpeg 编排
make dev-api            # Go：REST + WebSocket (8080)

# 3) 前端
make dev-web
```

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
docker compose up -d redis postgres minio minio-init

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
| 阶段二边界 | `curl.exe -X POST http://127.0.0.1:8000/v1/pipeline -H "Content-Type: application/json" --data-binary "@.tmp/plan-request.json"` | **501**（阶段二实现） |

### 2. gRPC 契约

```bash
python scripts/smoke-grpc.py                      # 默认 127.0.0.1:50051
```

逐项校验 `Health` / `PlanScript`，并断言 `RunPipeline` 等四个 RPC 返回
`UNIMPLEMENTED`（阶段一的**有意行为**，见 `docs/ROADMAP.md`）。

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

阶段一观察到的现象（**这是预期结果，不是故障**）：

```
events:  api      "任务已创建，等待导演智能体拆解脚本"
events:  plan     "导演智能体正在拆解脚本…"          <- worker 已消费并调用 gRPC
events:  pipeline "任务失败：... RunPipeline ... Unimplemented"
```

提交 → 入队 → worker 消费 → gRPC 调用 → 明确失败并落库，
说明**整条编排链路是通的**，缺的只是阶段二的流水线实现。

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
│   │   ├── llm.py               # LLM/VLM 客户端（重试/结构化输出/成本/mock）
│   │   ├── graph/               # LangGraph 状态与图拓扑
│   │   ├── agents/              # 导演 / 编码 / 审查（提示词独立成 .md）
│   │   ├── sandbox/             # 安全执行生成的渲染代码
│   │   ├── service.py           # 业务门面（HTTP 与 gRPC 共用）
│   │   ├── grpc_server.py       # gRPC servicer + 契约适配
│   │   └── main.py              # FastAPI + gRPC 双栈进程入口
│   └── tests/
├── web/                         # React + Vite + TS 审核台
├── deploy/                      # redis.conf / postgres init / nginx
└── scripts/                     # dev-env.ps1 / gen-proto.ps1
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
| 二 | Python 多智能体核心（编码 / 审查、沙盒渲染器、图拓扑、RAG） | ⏳ 待办 |
| 三 | Go 编排与媒体处理（Asynq、gRPC 流式、ffmpeg 并发合成） | ⏳ 待办 |
| 四 | 反馈闭环与前端（WebSocket、React 审核台、打回重做） | ⏳ 待办 |
| 五 | 生产加固（可观测性、成本核算、多租户） | ⏳ 待办 |

> 阶段一已额外落地：**导演智能体**（脚本 → 结构化分镜表，含时长预算修复）
> 与**沙盒静态安全策略**（AST 白名单）。两者都是阶段二的前置能力，
> 让 `PlanScript` 这条链路可以端到端验证。
>
> 阶段二的其余 RPC 目前返回 `UNIMPLEMENTED` —— 这是**有意为之**：
> Go 侧据此判定「不可重试」，而不是把「还没实现」误当成基础设施故障反复重投。

阶段一的实际交付范围见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

---

## 文档索引

| 文档 | 内容 | 读者 |
| --- | --- | --- |
| [`Agent.md`](Agent.md) | 智能体契约、状态机、目录职责、编码规范 | 所有贡献者（**先读这个**） |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 设计思路：为什么这么做、代价与取舍、风险与缓解 | 架构评审 / 新人 |
| [`docs/API.md`](docs/API.md) | REST / WebSocket / gRPC 契约与错误码 | 前后端 / 集成方 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 各阶段交付物与验收标准 | 项目管理 |

---

## 许可

MIT，见 [`LICENSE`](LICENSE)。
