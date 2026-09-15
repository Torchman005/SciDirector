# Agent.md — SciDirector 智能体协作与工程规约

> 本文件是本仓库的**唯一权威协作契约**。任何人类开发者或 AI 编码智能体在修改本仓库前，
> 必须先读本文件；修改架构、契约、状态机、目录职责后，**必须在同一次提交中更新本文件**。
>
> 版本：v0.1.0 · 阶段：阶段一（环境与骨架搭建）· 最后更新：见文末「迭代日志」

---

## 1. 项目一句话

SciDirector（科学视频导演）**不生成像素，而是生成"可验证的渲染计划"**：
用多智能体把科普脚本拆成分镜，把每个分镜路由到最合适的**确定性渲染引擎**
（Manim 渲染数学、D3/ECharts 渲染数据、代码高亮动画渲染代码），
再用视觉大模型（VLM）审查渲染出的帧，不合格就打回重做，直到科学严谨且流畅。

---

## 2. 架构总览

```
                         ┌──────────────────────────────────────────┐
   用户 / 审核员  ──HTTP──▶│  web (React + Vite)  分镜表 / 预览 / 打回 │
                         └───────────────┬──────────────────────────┘
                                         │ REST + WebSocket
                         ┌───────────────▼──────────────────────────┐
                         │  api (Go · Gin)                          │
                         │  · /api/v1/generate  接收脚本             │
                         │  · /api/v1/jobs/:id  查询状态             │
                         │  · /ws/jobs/:id      进度推送 + HITL 回流 │
                         └───────────────┬──────────────────────────┘
                                         │ Asynq Enqueue
                         ┌───────────────▼──────────────────────────┐
                         │  Redis   队列(asynq) + 状态快照 + 锁      │
                         └───────────────┬──────────────────────────┘
                                         │ Asynq Consume
                         ┌───────────────▼──────────────────────────┐
                         │  worker (Go)  长链路编排                  │
                         │  · gRPC 调用 Python 大脑（含流式事件）     │
                         │  · 状态机推进 + 失败重试（指数退避）        │
                         │  · os/exec + Goroutine 并发 ffmpeg 合成   │
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

## 3. 目录职责

```
SciDirector/
├── Agent.md                     # 本文件：协作契约（活文档）
├── docker-compose.yml           # 单机全栈编排
├── Makefile                     # 统一入口：make proto / make dev / make test
├── docs/
│   ├── DESIGN.md                # 思路文档：设计决策、算法、取舍（活文档）
│   ├── ARCHITECTURE.md          # 运行时拓扑与部署说明
│   ├── API.md                   # REST / WebSocket / gRPC 契约说明
│   └── ROADMAP.md               # 阶段进度与验收标准
├── proto/scidirector/v1/        # 跨语言契约（唯一真源）
├── backend/                     # Go module
│   ├── cmd/api, cmd/worker      # 两个可执行入口，共享 internal
│   └── internal/
│       ├── config               # 配置装载（环境变量 + 默认值 + 校验）
│       ├── logging              # 结构化日志
│       ├── domain               # 领域模型 + 状态机（无外部依赖，纯逻辑）
│       ├── store                # Redis 状态仓储
│       ├── queue                # Asynq 客户端/服务端/任务载荷
│       ├── ai                   # Python 大脑的 gRPC 客户端
│       ├── media                # ffmpeg 封装与并发合成器
│       ├── httpapi              # Gin 路由、中间件、handler
│       ├── ws                   # WebSocket Hub（广播 + 上行反馈）
│       ├── worker               # Asynq handler 与流水线编排
│       └── pb                   # protoc 生成代码（勿手改）
├── ai/                          # Python package: scidirector_ai
│   ├── scidirector_ai/
│   │   ├── main.py              # FastAPI 入口（/healthz, /v1/plan）
│   │   ├── grpc_server.py       # gRPC servicer 与契约适配
│   │   ├── service.py           # 业务门面（HTTP 与 gRPC 共用）
│   │   ├── schemas.py           # 领域模型与业务校验
│   │   ├── llm.py               # LLM/VLM 客户端（重试/结构化输出/成本/mock）
│   │   ├── graph/               # LangGraph 状态定义（阶段二补 nodes/builder）
│   │   ├── agents/              # base / director（阶段二补 coder、critic）
│   │   │   └── prompts/         # 提示词独立成 .md，与代码分离
│   │   ├── sandbox/             # 静态安全策略（阶段二补 runner 进程隔离）
│   │   └── pb/                  # protoc 生成代码（勿手改）
│   └── tests/
├── web/                         # React + Vite + TS 审核台
├── deploy/                      # redis.conf / postgres init / nginx
└── scripts/                     # 开发脚本（含 gen-proto）
```

---

## 4. 开发阶段与验收（活进度表）

- [x] **阶段一 · 环境与骨架搭建**
  - [x] Go：Gin + Asynq + gRPC 客户端 + 配置/日志/状态机骨架
  - [x] Python：FastAPI + gRPC 服务端 + LangGraph State 定义
  - [x] `docker-compose.yml`（Redis / Postgres / MinIO / ai / api / worker / web）
  - [x] proto 契约 + 跨语言代码生成脚本
  - [x] **提前注入**：Go 主链路 worker 处理器（gRPC 流式消费 + 事件落库）、
        ffmpeg 并发合成；Python 导演智能体完整实现、沙盒静态安全策略
- [ ] **阶段二 · Python 多智能体核心**
  - [x] Director Agent：脚本 → 结构化分镜表 + 场景标签（含时长预算修复）
  - [x] 沙盒静态安全策略（AST 白名单）
  - [ ] Coder Agent：标签路由 → Manim / D3 / 代码动画源码
  - [ ] Critic Agent（VLM）：抽帧审查 + 具体修改意见
  - [ ] 沙盒运行器：子进程执行 Manim → MP4 片段 + 资源限制
  - [ ] LangGraph 图拓扑 + 循环 + 重试上限 + checkpoint 持久化
  - [ ] RAG：Few-shot 优秀案例检索
- [ ] **阶段三 · Go 编排与媒体处理**
  - [x] `/api/v1/generate` → Redis 队列
  - [x] Worker 消费 → gRPC 流式调用 → 状态推进
  - [x] ffmpeg 并发归一化 + concat 合成（基础版）
  - [ ] 全片 TTS 配音与字幕时间轴对齐
  - [ ] 转场与统一调色
- [ ] **阶段四 · 反馈闭环与前端**
  - [x] WebSocket 服务端（快照重放 + 扇出 + 背压 + 多实例 Pub/Sub）
  - [ ] React 分镜审核台
  - [x] 「打回重做」的服务端链路（REST + WS 上行 → 单镜头任务入队）
- [ ] **阶段五 · 生产加固**（后续）
  - [ ] OpenTelemetry 链路追踪、Prometheus 指标、Grafana 看板
  - [ ] 多租户与配额、成本核算（token/渲染时长）

> 各阶段的**验收命令与预期输出**见 `docs/ROADMAP.md`。
> 阶段一中 `RunPipeline` 等 RPC 返回 `UNIMPLEMENTED` 是**正确行为**，
> 不是缺陷 —— Go 侧据此判定「不可重试」，避免把「还没实现」误当成基础设施故障反复重投。

---

## 5. 智能体契约（AI Agent Contract）

每个智能体必须满足：**输入输出为可校验的结构化对象**、**幂等**、**单次调用内不产生外部副作用（除沙盒渲染外）**。

### 5.1 Director Agent（导演）
- 输入：`raw_script`（用户脚本）、`style_guide`（可选风格约束）、`target_duration_sec`
- 输出：`list[ShotSpec]`，其中每个 `ShotSpec` 必须含
  `shot_id / index / narration / visual_brief / tag / engine / duration_sec`
- 硬约束：所有 `duration_sec` 之和 ∈ `target_duration_sec ± 10%`；`tag` ∈ `{MATH, DATA, CODE, AMBIENCE}`

### 5.2 Coder Agent（编码）
- 路由表（**确定性映射，不允许模型自由发挥**）：

  | tag | engine | 产物 |
  | --- | --- | --- |
  | `MATH` | `manim` | `Scene` 子类 Python 源码 |
  | `DATA` | `d3` / `echarts` | HTML+JS，由 headless 浏览器录帧 |
  | `CODE` | `code_anim` | 带语法高亮的代码打字动画源码 |
  | `AMBIENCE` | `stock` | 素材检索 query / 占位渐变 |

- 硬约束：生成的代码**禁止**网络访问、文件系统写越界、`subprocess`、`eval` 之外的动态执行；
  必须只使用沙盒白名单内的导入。

### 5.3 Critic Agent（审查 / VLM）
- 输入：抽帧图像（3~6 帧，等间隔）+ 该镜头 `narration` + `visual_brief`
- 输出：`CriticFeedback{passed, score, issues[], suggestions[], model}`
- 硬约束：`passed=false` 时**必须**至少给出 1 条**可执行**的 suggestion
  （例如 "字号从 24 提到 48"、"把 `Create` 的 run_time 从 0.5 提到 2.0"），
  禁止 "画面不好看" 这类不可执行意见。
- 通过阈值：`score >= 0.75` 且 `critical_issues == 0`。

### 5.4 循环与熔断
```
plan → for each shot:
         coder → render → critic
           ├─ passed → APPROVED
           └─ failed → attempt+1
                 ├─ attempt < MAX_ATTEMPTS(3) → 回 coder（携带 feedback）
                 └─ attempt >= MAX_ATTEMPTS   → AWAITING_HUMAN（转人工，禁止无限重试）
```

---

## 6. 状态机（唯一真源）

```
PENDING ──▶ GENERATING ──▶ RENDERING ──▶ CRITIQUING ──┬─▶ APPROVED
   ▲              │              │             │      │
   │              ▼              ▼             ▼      └─▶ RETRYING ──┐
   │            FAILED ◀─────────┴─────────────┘            │        │
   │              │                                          └────────┘
   │              └─▶ RETRYING（指数退避，上限后→AWAITING_HUMAN）
   └───────────────── AWAITING_HUMAN ──(人工意见)──▶ RETRYING
```

- 合法迁移在 `backend/internal/domain/state.go` 中**硬编码**，非法迁移必须返回错误而非静默忽略。
- 每次迁移必须产出：`shot_id / from / to / attempt / reason / ts`，并写入 Redis 事件流（供 WS 重放）。

---

## 7. 编码规范

### 7.1 Go
- 包名小写单数；错误必须 `fmt.Errorf("...: %w", err)` 包裹；不使用 `panic` 处理业务错误。
- `context.Context` 作为第一个参数贯穿所有 IO；所有外部调用必须有超时。
- 并发：Goroutine 必须有明确退出路径（`errgroup` / `context` 取消）；共享状态用 channel 或 mutex 保护。
- 每个导出符号有 doc comment；复杂逻辑写「为什么」而不是「做了什么」。

### 7.2 Python
- Python ≥ 3.11，全量类型注解；Pydantic v2 做边界校验。
- `async` 入口（FastAPI/gRPC aio）不得阻塞事件循环，重活丢 `asyncio.to_thread` 或独立进程池。
- 所有 LLM 调用必须：可重试、可降级、记录 token 成本、超时可控。
- 提示词（prompt）与代码分离，放在 `agents/prompts/`，便于版本化与 A/B。

### 7.3 通用
- **契约先行**：先改 `proto/`，再 `make proto`，最后改两侧实现。
- 任何"魔法数字"必须具名常量并注释来源。
- 禁止提交密钥；一律走环境变量，模板见 `.env.example`。

---

## 8. 常用命令

```bash
make proto            # 由 proto 生成 Go + Python 代码
make dev-infra        # 仅启动 redis/postgres/minio
make dev-api          # 本地跑 Go API（热重载）
make dev-worker       # 本地跑 Go Worker
make dev-ai           # 本地跑 Python AI 服务
make dev-web          # 本地跑前端
make test             # 全量测试
make lint             # gofmt + go vet + ruff + mypy
make up / make down   # docker compose 全栈
```

---

## 9. 跨语言实现约定（阶段一确立）

这些约定是阶段一踩过的坑，后续修改**必须**遵守：

| 约定 | 原因 |
| --- | --- |
| proto 枚举的 `UNSPECIFIED`（值 0）在 Python 侧必须映射为空串，在 Go 侧必须映射为 `""` | 否则「任务级事件」（不带状态）会把镜头状态污染成一个非法值 `UNSPECIFIED` |
| 所有事件**先写入 Redis 事件流，再广播** | `AppendEvent` 内含 `Publish`，api 侧订阅后转发。顺序反过来会产生「客户端收到但历史里没有」的事件，重连时出现事件缺口 |
| worker **不持有** WebSocket Hub | worker 与 api 可能在不同进程/容器，推到进程内 Hub 对前端毫无意义；统一走 Redis Pub/Sub |
| proto 的 `RuntimeError` 类错误必须映射为 `UNIMPLEMENTED` 而非 `INTERNAL` | 前者不可重试，后者会触发 Asynq 重试同一个注定失败的调用 |
| 大文件（MP4/PNG）只传路径 | 走共享卷或 MinIO；塞进 gRPC 消息会让内存放大数倍 |
| `plan` 节点的分镜表通过 `payload_json` 传 JSON 而非 proto 字段 | 分镜表结构仍在快速迭代，用 JSON 可避免每次都重新生成两侧代码 |
| Python 侧 `PolicyReport.ok` 只要求无 **error** 级违规 | warning（如 `while True`）由运行时超时兜底，静态阶段误杀合法写法的代价更高 |
| Go 侧状态机的 `Job.ProgressRatio()` 与字段 `Progress` 并存 | Go 不允许同名字段与方法；字段负责 JSON 序列化，方法负责计算 |
| Windows 下**禁止**用 PowerShell 5.1 的 `Get-Content`/`Set-Content` 改 UTF-8 源码 | 它按系统 GBK 代码页读写，会静默破坏中文注释的多字节序列（本项目已踩过一次） |
| 模型调用的意图必须**显式传参**（`llm.Task.*`），禁止从提示词文本嗅探关键词 | mock 客户端曾因导演提示词含「审查」二字而返回错误结构，被静默解析成「空分镜表」——这类「看起来成功」的失败形态比抛异常危险得多 |
| 派生的模拟数据必须**结构上等价**于真实产出 | 否则 mock 模式会掩盖真实的解析/校验问题，联调通过但上线即挂 |
| 仓库内自带的依赖目录（`.pylibs`）必须置于 `PYTHONPATH` **末尾** | 它可能包含被深度依赖的包（如 `typing_extensions`），置于前面会遮蔽版本更完整的同名包 |

---

## 10. 迭代日志

| 版本 | 阶段 | 变更 |
| --- | --- | --- |
| v0.1.0 | 阶段一 | 建立目录骨架、跨语言 proto 契约与生成流水线、docker-compose 全栈、Go 骨架（配置/日志/状态机/仓储/队列/gRPC 客户端/WS Hub/编排处理器/ffmpeg 封装）、Python 骨架（配置/日志/Schema/LLM 客户端/LangGraph 状态/导演智能体/沙盒策略/gRPC+FastAPI 双栈）、四份文档与构建脚本；Go 与 Python 测试全部通过 |
| v0.1.1 | 阶段一（修订） | **回退阶段二的提前实现**，把仓库收敛到经过验证的阶段一状态：移除编码/审查智能体、沙盒运行器、渲染与媒体工具、RAG、图拓扑与 checkpointer；`RunPipeline` / `GenerateShot` / `CritiqueShot` / `ReviseShot` 恢复为返回 `UNIMPLEMENTED`。保留阶段一两处前置能力（导演智能体、沙盒静态安全策略）。修复 `scripts/dev-env.ps1` 的 `PYTHONPATH` 顺序缺陷（`.pylibs` 必须置于**末尾**，否则会遮蔽版本更完整的同名包，表现为 pydantic 导入时莫名的 `cannot import name`） |
| v0.1.2 | 阶段一（修复） | 手工联调实测发现并修复两项语义缺陷：① `UNIMPLEMENTED` 不再被 Asynq 重试（新增哨兵 `ai.ErrNotImplemented`，worker 返回 `asynq.SkipRetry` 直接归档）；② `sandbox_ready` 改为「至少一个渲染引擎可用」并新增结构化 `engines` 字段，使实现与文档一致。新增 `scripts/smoke-grpc.py`、`scripts/smoke-ws.py` 两个可复用冒烟脚本与 `make smoke*` 目标；README 新增「手动测试」章节。Go 测试 +1 包，Python 测试 71 → 83 |
