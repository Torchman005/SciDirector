# Agent.md — SciDirector 智能体协作与工程规约

> 本文件是本仓库的**唯一权威协作契约**。任何人类开发者或 AI 编码智能体在修改本仓库前，
> 必须先读本文件；修改架构、契约、状态机、目录职责后，**必须在同一次提交中更新本文件**。
>
> 版本：v0.6.3 · 阶段：阶段五（生产加固）· 最后更新：见文末「迭代日志」

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
│   │   ├── main.py              # FastAPI 入口（/healthz, /v1/pipeline）
│   │   ├── grpc_server.py       # gRPC servicer 与契约适配
│   │   ├── service.py           # 业务门面（HTTP 与 gRPC 共用）
│   │   ├── schemas.py           # 领域模型与业务校验
│   │   ├── pbconv.py            # 领域模型 ↔ proto 双向转换（含枚举映射）
│   │   ├── llm.py               # LLM/VLM 客户端（重试/结构化输出/成本/mock）
│   │   ├── media.py             # ffmpeg 封装、抽帧、lavfi 环境镜头、字体探测
│   │   ├── renderer.py          # 渲染器统一抽象（Manim/Html/Ambient 路由与就绪探测）
│   │   ├── graph/               # state / nodes / builder / checkpoint
│   │   ├── agents/              # base / director / coder / critic
│   │   │   └── prompts/         # 提示词独立成 .md，与代码分离
│   │   ├── rag/                 # Few-shot 检索（JSON 语料 + 可解释打分）
│   │   ├── sandbox/             # policy.py 静态策略 + runner.py 进程隔离 + manim.py
│   │   └── pb/                  # protoc 生成代码（勿手改）
│   └── tests/
├── web/                         # React + Vite + TS 审核台
├── deploy/                      # redis.conf / postgres init / nginx
└── scripts/                     # 开发脚本（含 gen-proto、smoke-grpc、smoke-ws）
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
- [x] **阶段二 · Python 多智能体核心**
  - [x] Director Agent：脚本 → 结构化分镜表 + 场景标签（含时长预算修复）
  - [x] 沙盒静态安全策略（AST 白名单）
  - [x] Coder Agent：标签路由 → Manim / D3 / 代码动画源码（生成前 RAG 召回 + 生成后策略校验）
  - [x] Critic Agent（VLM）：抽帧审查 + 分维度打分 + 强约束 JSON + 具体修改意见
  - [x] 沙盒运行器：子进程执行 Manim → MP4 片段 + 超时/内存限制（Windows Job Object / POSIX rlimit）
  - [x] LangGraph 图拓扑 + 带反馈循环 + 重试上限 + 熔断转人工 + checkpoint 持久化
  - [x] RAG：Few-shot 优秀案例检索（可解释打分，中文用 CJK 二元组匹配）
  - [x] 5 个 RPC 全部实现并经 Go 侧 gRPC 端到端调通（`RunPipeline` 服务端流式）
- [x] **阶段三 · Go 编排与媒体处理**
  - [x] `/api/v1/generate` → Redis 队列
  - [x] Worker 消费 → gRPC 流式调用 → 状态推进
  - [x] ffmpeg 并发归一化 + concat 合成（基础版）
  - [x] **并发收敛**：任务内 Worker Pool + 进程级全局闸门（防 OOM 两道防线）
  - [x] **进程生命周期**：`WaitDelay` 兜底 + 杀进程树 + 单命令硬超时
  - [x] **跨镜头转场**（xfade / acrossfade）与**统一规格＋调色**（色彩范围转换与打标）
  - [x] **字幕时间轴对齐 + 软字幕封装**（mov_text）
  - [ ] 全片 TTS 配音 —— **待办**：本机无可用引擎，且需先确定服务商。接口已就绪
  - [x] **局部重渲染**：只重渲某几秒并拼回原片（按引擎能力如实降级）
  - [x] **产物归档**：none/local/s3 三后端 + 本地卷保留策略
  - [x] **队列可观测**：`GET /api/v1/queue/stats`
  - [x] **验收 B4/B5 自动化**：重复投递只渲染一次（入队侧 `TaskID` + 执行侧幂等键，两道闸门）；
        断开 Redis 后**排队中**与**执行中**的任务都能恢复（私有 Redis + `SIGKILL` 真实故障注入）
- [x] **阶段四 · 反馈闭环与前端**
  - [x] WebSocket 服务端（快照重放 + 扇出 + 背压 + 多实例 Pub/Sub）
  - [x] 「打回重做」的服务端链路（REST + WS 上行 → 单镜头任务入队）
  - [x] React 分镜审核台（分镜表 / 事件时间线 / 进度条 / 连接状态可见）
  - [x] **断线重连**：以快照为权威基线 + 按 seq 去重 + 空洞检测自动补发
  - [x] **人工放行**：熔断镜头的 approve 出口（含「全部通过即入队合成」）
  - [x] **成分镜编辑**：`PATCH /jobs/:id/shots/:id` 改文案并可立即重做
- [ ] **阶段五 · 生产加固**（后续）
  - [x] **成本核算**：`GET /api/v1/jobs/:jobID/cost` + 任务详情内 `cost`。
        口径：报**用量**不报金额；`llm.*` 由 Python 上报并持久化，`render_sec`/`tts_chars`
        由 Go 读取时现算（见 `docs/ROADMAP.md`「成本核算：已完成的部分与口径」）
  - [x] **沙盒网络隔离**：`SandboxRunner` 用 `unshare -r -n` 包住渲染子进程，
        `ExecResult.network_isolation` 与 `/healthz` 的 `sandbox:network=…` **如实上报**实际机制；
        `SCID_SANDBOX_NETWORK_ISOLATION=require` 时拿不到隔离就拒绝执行（fail closed）。
        **边界**：只覆盖经 runner 的子进程（manim、ffmpeg/ffprobe），
        HTML 引擎的 Chromium 仍在服务进程内直接起、**未被隔离**；容器层加固亦未做
  - [ ] 配额与限流 —— **待办**：验收只要求「可查询」，且限额要按归属算，与多租户耦合
  - [x] **可观测性**：`internal/obs`（OTLP + W3C 传播 + Prometheus）+ Python `obs.py` +
        `docker compose --profile observability`（collector → Tempo/Prometheus → Grafana，
        数据源与看板全部 provisioning）。一次生成请求 = 跨 `scid-api`/`scid-worker`/`scidirector-ai`
        的 25 个 span；浏览器验证见 `make verify-obs`。
        **边界**：业务指标未导出为 Prometheus 指标；嵌套瀑布图未做自动化断言；
        应用容器未在 compose 内端到端跑过（见 `docs/ROADMAP.md`）
  - [x] **多租户**：归属（创建时落定）+ 隔离（读取/写入/WS 三个收口点，越权与不存在
        返回**完全一致**的 404）+ 按租户配额（在跑集合、现算自愈；超出返回 429）。
        **边界**：身份来源是请求头、**不是认证**；未做按存储的物理隔离；
        `/queue/stats` 仍是全局视图。
        一条真实教训：任务是整份 JSON 存的，api/worker 都会读改写 ⇒
        **加结构体字段不是向后兼容的**（改字段必须两个二进制一起重启）
  - [x] **状态对账**：`GetCheckpointSnapshot` RPC（只读）+ `internal/reconcile` +
        `GET /jobs/:id/reconcile`（默认只读，`?repair=true` 才改）+ worker 周期扫描。
        **只自动修「stuck_job」一类**（终态依据 Redis 自己的镜头状态推出），
        其余只报告；修复幂等且写 `node=reconcile` 事件。
        **边界**：在跑索引覆盖"从未变成终态"的任务（真实故障的形状），
        "曾经终态又被改回非终态"扫不到；多副本并发未实测；对账历史未留痕
  - [ ] 沙盒加固（`--network=none --read-only`、seccomp 白名单）

> 各阶段的**验收命令与预期输出**见 `docs/ROADMAP.md`。
> 阶段二结束后 `RunPipeline` / `GenerateShot` / `CritiqueShot` / `ReviseShot` 均已实现；
> Go 侧 `ai.ErrNotImplemented` 分支**保留不删** —— 它是灰度期与未来新增 RPC 的安全网：
> 未实现的 RPC 必须被判定为「不可重试」，避免把「还没实现」误当成基础设施故障反复重投。
> 阶段二验收结果（含 A2/A8 受本机工具链限制的部分验证）记录在 `docs/ROADMAP.md`。

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

## 9. 跨语言实现约定（阶段一 / 阶段二确立）

这些约定是踩过的坑，后续修改**必须**遵守：

| 约定 | 原因 |
| --- | --- |
| proto 枚举的 `UNSPECIFIED`（值 0）在 Python 侧必须映射为空串，在 Go 侧必须映射为 `""` | 否则「任务级事件」（不带状态）会把镜头状态污染成一个非法值 `UNSPECIFIED` |
| 所有事件**先写入 Redis 事件流，再广播** | `AppendEvent` 内含 `Publish`，api 侧订阅后转发。顺序反过来会产生「客户端收到但历史里没有」的事件，重连时出现事件缺口 |
| worker **不持有** WebSocket Hub | worker 与 api 可能在不同进程/容器，推到进程内 Hub 对前端毫无意义；统一走 Redis Pub/Sub |
| proto 的 `RuntimeError` 类错误必须映射为 `UNIMPLEMENTED` 而非 `INTERNAL` | 前者不可重试，后者会触发 Asynq 重试同一个注定失败的调用 |
| 大文件（MP4/PNG）只传路径 | 走共享卷或 MinIO；塞进 gRPC 消息会让内存放大数倍 |
| `payload_json` 的键**必须**是 snake_case、枚举**必须**是数字 | 它最终由 Go 的 `encoding/json` 反序列化进 `pb.ShotSpec`；写成驼峰或枚举名字符串在 Python 侧看不出问题，到 Go 侧静默变成零值 |
| `plan` 节点的分镜表通过 `payload_json` 传 JSON 而非 proto 字段 | 分镜表结构仍在快速迭代，用 JSON 可避免每次都重新生成两侧代码 |
| Python 侧 `PolicyReport.ok` 只要求无 **error** 级违规 | warning（如 `while True`）由运行时超时兜底，静态阶段误杀合法写法的代价更高 |
| 契约型违规（如 HTML 缺 `window.__seek`）必须自带 `advice` 文案 | 否则拼出的反馈是病句（「使用了被禁止的 缺少渲染契约…」），回灌给编码模型等于噪音 |
| Go 侧状态机的 `Job.ProgressRatio()` 与字段 `Progress` 并存 | Go 不允许同名字段与方法；字段负责 JSON 序列化，方法负责计算 |
| Windows 下**禁止**用 PowerShell 5.1 的 `Get-Content`/`Set-Content` 改 UTF-8 源码 | 它按系统 GBK 代码页读写，会静默破坏中文注释的多字节序列（本项目已踩过一次） |
| **含非 ASCII 的 `.ps1` 必须带 UTF-8 BOM** | Windows PowerShell 5.1 对**无 BOM** 的脚本按 ANSI（中文系统上是 GBK）解码。中文注释被误码后会产生游离引号/反引号并**吞掉换行**，导致整个文件无法解析，而报错行号还指向错误的位置（本项目 `gen-proto.ps1` 与 `dev-env.ps1` 都因此完全不可用，报出的 `Unexpected token 'import'` 让排查方向完全跑偏）。注意：这个环境的 `pwsh` 实际就是 5.1，不能指望「新版本默认 UTF-8」 |
| `.ps1` 里给原生命令做能力探测时，必须临时把 `$ErrorActionPreference` 置为 `Continue` | PowerShell 会把原生命令写到 stderr 的任何内容包装成 `NativeCommandError`，而 `2>$null` **只丢弃输出、挡不住这个错误**。在 `Stop` 下，「探测到某工具未安装」这条完全正常的退路会直接中断脚本 —— 表现为「本该自动回退的路径永远走不到」 |
| `.ps1` 避免反引号续行与 here-string | 两者对行边界/编码异常极度敏感，一旦解码出错会连带整份文件不可解析。外部命令一律用**数组参数**传递（`& cmd @args`），没有续行符也不受引号转义影响 |
| `asynq.Inspector.GetQueueInfo` 对**不存在的队列**返回的是内部 RDB 错误，既不包装 `ErrQueueNotFound` 也无从判定 | Asynq 自己的文档说会 wrap，实际实现没有（`GetQueueInfo` 直接透传 `rdb.CurrentStats` 的错误）。判定「队列不存在」必须改用文档化的 `Inspector.Queues()` 先列现存队列。更根本的一条：**队列不存在 == 队列为空**，应当返回全 0 而不是错误 —— 否则观测接口会在最需要它的时刻（全新部署上确认「没东西卡住」）整个失败 |
| concat 滤镜同时处理音视频时，输入必须**交替排列**为 `[v0][a0][v1][a1]` | 写成 `[v0][v1]…[a0][a1]…` 时 ffmpeg 不会说「顺序错了」，只报像素格式/采样率之类的费解错误 |
| 分流**音频**必须用 `asplit`，`split` 是视频专用滤镜 | 用错时 ffmpeg 抛出的是「Media type mismatch … (video) … (audio)」，极具误导性，会让人以为是标签或流选择写错了 |
| 删除是不可逆操作，保留策略要做成**纯函数** | `PlanCleanup` 不碰 IO、不看时间，因此「什么情况下会删什么」可被完整断言；散落在若干 if 里的隐式删除行为无法被测试，也没人敢改 |
| 归档失败时**不清理本地**，且归档/清理失败都不改变任务结果 | 「宁可占盘，不可丢件」—— 磁盘可以加，数据丢了找不回来；而产物已生成、任务已成功时，因对象存储抖动判失败，用户看到的是「失败」而片子其实好好的 |
| 对象键必须清洗文件名 | 文件名可能来自渲染器并含 `..` 或路径分隔符。对象存储的键没有目录概念，但按前缀授权的策略会因此被绕过 |
| 成功响应统一是 `{"ok":true,"data":…}`，查询接口在 `data` 内**还有一层** | `GET /jobs/:id` 的 `data` 是 `{job,stat,progress}`，不是 Job 本身。少拆一层**不会报错**，只会让所有字段变成 `undefined`、页面静静地什么都不显示。Go 侧有「响应形状契约」测试钉死这个结构 |
| 状态变更**不等于**任务推进，必须有人入队 | `HandleApproveShot` 曾只把 `Job.Status` 改成 COMPOSING 就结束 —— 而状态本身不驱动任何东西。结果是最后一个熔断镜头放行后任务永远停在 COMPOSING，前端看着 100% 却等不到成片，且无任何错误可报 |
| 人工放行了**无产物**的镜头后，成片会静默缺镜头 | 引擎缺失时镜头熔断，审核员放行它只是让流程继续 —— 它根本没有视频文件。此时若把任务标成 COMPLETED，用户会拿到一部少了几段的片子而界面显示一切正常，比直接失败更危险。必须如实标为 `PARTIAL` |
| PATCH 的可选字段必须用**指针** | nil 表示「不改」、空串表示「清空」。用值类型 + 零值判断会把「清空画外音」误判成「不改」，是一个静默不生效的缺陷 |
| 重连一律以**快照**为基线，不能只补事件 | 快照含任务明细 + 统计 + 历史，是唯一能保证「重连后与刷新页面一致」的东西。只补事件不够：断线期间分镜表本身可能已经变了 |
| 事件消费必须按 seq **去重**且**排序**后应用 | 订阅建立与快照生成之间存在竞态窗口，同一条事件可能既在快照里又从实时通道到达。不去重会让派生量算错，不排序会让最终状态取决于到达顺序 —— 而这正是「进度回跳」的根因 |
| 重连退避必须带**抖动** | 固定间隔重连在服务端重启时会让所有客户端同时涌上来（惊群），抖动把重连时间摊开 |
| 长跑列表只保留最近 N 条 | 事件流是诊断视图，让它无限增长会拖垮长跑任务的页面 |
| 前端状态归约必须是**纯函数** | C2/C3 的全部逻辑都在归约里，而出问题的表现是「偶尔少一条事件」「进度条退一格」—— 靠肉眼点页面几乎不可能稳定复现，只能靠单测 |
| **编辑含中文的 `.ps1` 之后必须确认 BOM 还在** | PS 5.1 对无 BOM 的脚本按 GBK 解码，中文误码会破坏语法，且报错指向错误位置。而**多数编辑器/工具写文件时默认不写 BOM**，于是「改一行注释 → 脚本整个不可解析」反复发生（本项目已踩三次）。改完请验一次：`([System.IO.File]::ReadAllBytes($p))[0] -eq 0xEF` |
| `.env` 的解析只能有**一处实现** | 根目录 `.env` 已有四个消费者（`docker compose`、Go、Python、本地脚本），语义本就容易分叉。Windows 侧不要在 `.bat` 里另写一套解析，而是由 `scripts/load-env.ps1` 统一解析、`.bat` 只执行它打印的 `set` 行。其中「`#` 前有空白才当行内注释」是最容易写错的一条（`.env.example` 里就有这种写法） |
| 工作目录必须解析成**绝对路径**，且要尊重显式配置 | Python 的 `sandbox_work_dir` 默认是相对路径，会跟着启动时的 cwd 走 —— `cd ai` 后启动落到 `ai/.data/sandbox`，在仓库根启动又落到 `.data/sandbox`，产物分裂成两棵树。正确做法是把相对路径按仓库根展开，而**不是**无条件覆盖用户的设置 |
| 脚本 source 进当前会话时，改过的全局设置必须**还原** | `dev-env.ps1` 用 `. ` source，若留下 `$ErrorActionPreference='Stop'`，之后**所有**原生命令写到 stderr 的内容（`go build` 的下载进度、`git fetch` 进度、`npm install` 的 warning）都会被当成终止性错误，构建当场中断 |
| 模型调用的意图必须**显式传参**（`llm.Task.*`），禁止从提示词文本嗅探关键词 | mock 客户端曾因导演提示词含「审查」二字而返回错误结构，被静默解析成「空分镜表」——这类「看起来成功」的失败形态比抛异常危险得多 |
| 派生的模拟数据必须**结构上等价**于真实产出 | 否则 mock 模式会掩盖真实的解析/校验问题，联调通过但上线即挂 |
| 仓库内自带的依赖目录（`.pylibs`）必须置于 `PYTHONPATH` **末尾** | 它可能包含被深度依赖的包（如 `typing_extensions`），置于前面会遮蔽版本更完整的同名包 |
| 图节点只**声明**下一跳（`route_hint`），条件边只做读取与校验 | 把判断散在边函数里会让「带反馈的循环」难以推理；节点负责决策，边负责路由 |
| 「引擎工具链缺失」必须映射为**不可重试**失败并熔断转人工 | 重试一个本机根本不存在的引擎只会烧满 attempt 计数。真实跑测已验证：缺 manim/d3 的两个镜头熔断为 `AWAITING_HUMAN`，其余镜头照常完成，任务终态 `PARTIAL` |
| 跨平台机制差异（POSIX `RLIMIT_DATA` vs Windows Job Object）必须**归一化**成同一个可观测结果 | 上层只认 `killed_reason == "memory"`，否则熔断逻辑要在两套平台上各写一遍。注意**机制和阈值语义都要对齐**，不能只对齐字段名：`RLIMIT_AS` 限制的是虚拟地址空间，比常驻内存大一个数量级，照搬内存上限会在远未超限时误杀正常进程（见 v0.4.2） |
| 传给子进程的路径必须 `.resolve()`；ffmpeg 的 `fontfile` 需要**两级转义**（`C\\:`），`%` **不要**转义 | 渲染器会切换 cwd，相对路径会失效；drawtext 的转义层级与常规 shell 不同，且 Windows 的 gyan 构建没有 fontconfig，需三级降级 |
| 交叉编译不了的测试不要写进默认目标 | Windows 缺 race 运行时 DLL（`0xc0000139`），`Makefile` 的 `RACE ?= -race` 必须可覆盖 |
| 长耗时单测与真实联调分离 | `pytest` 全量约 2 分钟（沙盒超时用例本身就要等待），不要塞进 pre-commit |
| **`.gitignore` 里按名字忽略的目录规则必须带前导斜杠锚定** | 未锚定的 `media/` 会匹配任意深度的同名目录，把 `backend/internal/media/` 整个 Go 包吞掉。本项目真实踩过：该包 6 个文件从未入库，clone 下来直接构建失败，而本地因文件都在磁盘上完全看不出问题。凡「只在仓库根出现」的运行时目录一律写成 `/<name>/` |
| FFmpeg 并发限制必须分**两层**，且职责要写在注释里 | 单任务内用 Worker Pool 限制在飞的 goroutine 数（内存 O(limit) 而非 O(分镜数)）；整个进程用一个**单例** Runner 的信号量限制子进程数（真正的 OOM 防线）。做成「每任务一个信号量」会让全局上限随并发任务数成倍放大，等于没有限制 |
| 外部进程一律设 `cmd.WaitDelay`，并用自定义 `cmd.Cancel` 杀**进程树** | 孙进程若继承 stderr 管道，`cmd.Wait()` 会永久阻塞；只杀直接子进程则后代继续吃 CPU。二者都表现为「任务没了但机器还在满载」，极难排查 |
| 单条外部媒体命令必须有硬超时 | 卡死的 ffmpeg 会永久占住并发槽位；占满后整条流水线**静默停摆且无错误可报**，比崩溃更难恢复 |
| 转场（`xfade`）无法 `-c copy`，是否启用必须先由**纯函数**判定并把原因写进事件 | `PlanTransitions` 是纯函数，offset 数学因此可单测；「配置了转场却没生效」必须能从事由里读到原因 —— 静默降级是排查噩梦 |
| `xfade` 的 offset 必须基于**累积游标**，不能用前一个片段的时长直接算 | 第 1 个连接点两种算法结果相同，从第 2 个开始分道扬镳。用错会让后续转场位置逐段漂移，而 ffmpeg 多数情况下**不报错**，只产出一部位置错乱的成片 |
| 音频必须用与视频**相同**的转场时长做 `acrossfade` | 否则每经过一个转场音轨就比画面多 T 秒，几个镜头之后音画彻底错位 |
| 统一规格必须显式**转换并打标**色彩范围，不能只统一分辨率 | 「Manim 与浏览器录制质感割裂」的首要原因是色彩范围不匹配（矢量输出有限范围 vs 截图全范围），而非艺术风格差异。只打标不转换等于没修，只转换不打标则播放器各自猜 |
| 「规格已匹配就跳过转码」的判定必须包含 `HasAudio` | 无音轨片段混进合成会让 concat 错位、让 `acrossfade` 直接报错。宁可多转一次码 |
| `eq` 滤镜未配置的参数必须**省略**而不是写 0 | `eq` 的默认值是 saturation/contrast/gamma = 1.0，写成 0 会让画面直接变黑。配置项的零值表示「不调整」，拼参数时必须逐项判断 |
| `-map` 的输入下标必须**动态跟踪**，不能写死 | `MuxFinal` 早期把字幕写成 `-map 2:s:0`（假设顺序是视频/音频/字幕）。TTS 未接入时 `audioPath` 恒为空，字幕实际是 1 号输入 —— 于是软字幕**在生产路径上永远封不进去**，而画面音轨都正常，很难怀疑到映射上 |
| 有字幕时慎用 `-shortest` | 当某条字幕恰好结束于视频末尾（字幕对齐逻辑**总是**这样），`-shortest` 会把该字幕包时长压成 0：字幕流仍存在、ffprobe 也能探到，但内容是空的，抽回 SRT 得到 0 字节文件。这类「流在、内容没了」的失败比报错难发现得多。现已仅在混入外部音轨时启用，并给字幕留尾部边距 |
| 字幕时间轴必须建立在**转场之后**的时间轴上 | 转场是交叠而非插入，成片比片段之和短 (n−1)×T。按原始时长累加定位会让字幕每过一个转场就后移 T 秒，越往后越明显 —— 而片头几秒看起来完全正常，极易漏掉 |
| 跨语言文本的时长估算不能用字符数 | 中英混排时按字符数会把英文段严重高估（12 字母的单词只占约 1.5 个汉字的时间）。统一用「语音权重」：汉字 1.0、英文单词 1.6 |
| 断句必须处理「句点兼作小数点」 | 「准确率 3.14」若在 3 与 14 之间断开，字幕会变成两条无意义碎片。句点后紧跟数字时不作为句子边界 |
| SRT 时间戳的毫秒必须**先整体取整再拆分** | 直接对秒取整会产出 `00:00:59,1000` 这种非法时间戳（毫秒字段必须有且仅有 3 位）。先转成毫秒整数再拆时分秒毫秒 |
| 单条字幕要有**最短显示时长**，窗口装不下时合并而不是硬拆 | 低于约 0.8 秒的字幕观众来不及读，等于没有。窗口容不下时合并**文本**（条数变少、每条更长），绝不丢内容 |
| 入队去重必须用 `asynq.TaskID`，不能只靠 `asynq.Unique` | `Unique` 的键由「队列 + 任务类型 + **载荷**」算出，而载荷里带着 `human_comment` —— 同一次 attempt 换个意见就变成另一个键，去重当场失效。`TaskID` 由 `job+shot+attempt` 直接拼出，才是真正表达「这是同一次尝试」的键 |
| 幂等占位必须在**真失败**时释放（含 `UNIMPLEMENTED`） | 只占位不释放的后果很隐蔽：一次瞬时故障（Redis 抖动 / AI 超时）就让该 attempt 在 TTL 内永久失去重试机会，任务卡在 GENERATING 而队列里空空如也。反过来成功路径**不能**释放 —— 那正是拦住 Asynq「执行成功但确认失败」重复投递的屏障 |
| 幂等用例必须带**反向对照** | 只断言「重复投递被跳过」的话，「永远返回已在处理中」这种退化实现同样能通过 —— 而那等于人工打回只能生效一次。必须同时断言「不同 attempt 各自渲染」 |
| 集成测试独占的 Redis 库号要**逐包分配** | `go test ./...` 会让不同包**并行**跑，共用库号时一方的 `FlushDB` 会清掉另一方正在断言的键，表现为随机失败的假阳性。偶发红比没有测试更糟 —— 它会训练人忽视红灯 |
| 故障注入必须 `SIGKILL`，不能用优雅关闭 | 优雅关闭会让 Redis 主动落盘，从而掩盖 `appendfsync everysec` 到底够不够用。而且杀掉之后要**确认端口真的连不上了**，否则后续断言可能连到残留进程上，结论完全失真 |
| 断线注入必须持续**超过一个租约长度**（Asynq 硬编码 30s，心跳 15s 一次） | B5 用例第一版断开不到 1 秒就把 Redis 拉回来，下一次心跳立刻把租约续上，正在执行的任务根本没被打断、正常跑完 —— 测试看着「恢复了」，但 recoverer 从头到尾没参与，等于什么都没验证。要让它真正留在 active 集合里，断线得压在 30s + 15s 以上 |
| POSIX 的「内存上限」必须用 `RLIMIT_DATA`，**不能**用 `RLIMIT_AS` | RLIMIT_AS 限制虚拟地址空间：映射的共享库、每个线程的栈、编解码器预留的缓冲都算在里面。实测 ffmpeg 抽一帧 RSS 仅 56MB、地址空间却要约 2GB（1.75GB 建不起 swscale 图）。把 `max_memory_mb` 当 AS 上限，正常进程会在远未触及内存上限时被杀；而 ffmpeg 这种情况下**退出码仍是 0、产物为空** —— 于是抽帧「全部失败」，Critic 对每个镜头都降级转人工，整条链路没有一处会报错 |
| 同一平台内的多条限制路径也必须收敛成同一个 `killed_reason` | `RLIMIT_CPU` 到点发 SIGXCPU，而墙钟兜底的截止是「超时 + 宽限期」；manim 侧 CPU 上限 = 超时×2，小超时下两者会精确撞在一起，谁先开火看调度。走 CPU 那条路时监控线程不会设置 `kill_flag`，`killed_reason` 就成了空串，上层把「资源耗尽」读成「未知失败」。只按 SIGXCPU 判定即可 —— 它只可能来自我们自己设的 `RLIMIT_CPU`，不存在误判；**不要**用 SIGKILL 推断原因（来源太多，会掩盖真问题） |
| 用 `minio-go` 的 `ListObjects` 断言「键在不在前缀下」必须显式 `Recursive: true` | 它默认按 `/` 分隔返回**公共前缀**，拿到的是 `jobs/etc/` 这类目录条目而不是对象键。用它来验证「清洗后的键仍落在 `jobs/` 之下」等于什么都没验证 —— 而且用例会以「键未出现」的形式失败，看起来像键清洗逻辑坏了（实际清洗是对的） |
| 对象存储的失败用例**不要**用「错误凭据」构造 | 模拟器（moto 等）**不校验凭据**，用错密钥照样上传成功，于是「错误必须被暴露」这条断言在模拟器上不成立，换个端点结论就翻。要用**端点不可达**构造失败 —— 那是任何实现下都必须失败的情形，因此可移植 |
| 归档/清理这类「不可逆 + 静默」的行为，必须把成功与失败两条路都钉住 | `finalizeArtifacts` 在补测试之前**只有调用点、没有任何测试**。它承载「归档失败就一个本地文件都不删」这条不变式，而其被破坏的症状不是报错，是**产物凭空消失**：任务成功、事件正常，用户点开成片才发现 404 |
| 环境变量名「文档写一套、代码读另一套」是静默失效的重灾区 | `.env.example` 与 compose 公共块一直写 `SCID_S3_*`，而 Go 侧只读 `SCID_MINIO_*` —— **没有任何代码读 `SCID_S3_*`**。照着模板配 S3 的人会设一堆完全不起作用的变量；而「端点是空串」与「压根没配置」在归档路径上表现一样，极难归因。改名统一时必须**保留旧名作为回退**：直接改名会让老部署的配置一夜失效，且同样是静默的。`config` 包为此补了迁移语义的用例（此前该包零测试） |
| 换掉一个基础设施组件时，要连带检查**镜像里的工具链假设** | RustFS 镜像里**没有 bash**（有 sh/curl/wget/nc），而健康检查写成 `timeout 2 bash -c '</dev/tcp/127.0.0.1/9000'` 会让容器永远停在 `starting` —— 现象是「服务明明在跑，但依赖它的容器一直不起」。改用组件自带的健康端点（RustFS 沿用 MinIO 的 `/minio/health/live`） |
| 用的基础镜像可能有「版本已归档停更」的问题，选型要定期复核 | MinIO 开源版**已归档停更**：`dl.min.io` 返回 410，GitHub 上最后一个 release 没有任何可下载产物，官方声明不再提供安全更新。compose 已从 `minio/minio` 换成仍在维护的 **RustFS**（S3 兼容、端口布局一致），并删掉了依赖已归档 `minio/mc` 的 init 容器。详见 `docs/ROADMAP.md` |
| 拉不到镜像不等于网络全断：可先用**显式镜像站主机名**拉取再打 tag | Docker Hub 在部分网络下不可达，daemon 里配的加速器也未必生效（表现为 `registry-1.docker.io` 连接超时）。此时 `docker pull <mirror>/library/redis:7.4-alpine` 再 `docker tag` 成本文件引用的名字即可，**不必改 daemon.json**（改它要重启 Docker）。这条已写进 compose 头部注释 |
| 来源允许列表里的 `*` 必须显式当成「放开」，不能当普通字面量比对 | 两处实现（WS 的 `CheckOrigin` 与 REST 的 CORS 中间件）都犯过这个错。后果是配 `SCID_CORS_ALLOWED_ORIGINS=*` 时**每一次 WebSocket 升级都 403**、实时通道整个死掉；而 REST 轮询仍在更新页面，看起来只是「有点卡」。REST 侧在本机联调时还被 Vite 代理掩盖了（同源，压根不需要 CORS） |
| 实时推送不含的字段，必须由**实时信号**触发回源 | `onNeedJobRefresh` 原先只在**断线**时触发，而事件里不含分镜明细、快照又是在「刚提交、还没拆解」那一刻生成的 ⇒ 分镜表**永远空着**，连接指示器却一路显示「实时」。更糟的是：一旦 WS 因配置问题全 403，`onclose` 反而会反复触发回源，界面**看起来是好的** —— 「通道坏掉」掩盖了「通道没被正确消费」 |
| 回源要合并**整个信封**（job + stat + progress），不能只合并 job | `deriveStat` 优先用服务端 `stat`（它确实是权威的），但 `stat` 只在快照里到达过 —— 于是界面长期显示「表格 4 行、统计写着共 0 个分镜、进度 0%」。这种自相矛盾的界面比单纯的空白更伤信任：`stream.ts` 的注释早就警告过它，而它恰恰由「只合并一半」制造出来 |
| HTTP 客户端的 Endpoint 约定必须与文档一致，否则「照着文档配」等于服务起不来 | `minio-go` 的 `Endpoint` **不接受带 scheme 的 URL**，而 `.env.example` 与 compose 都写 `http://host:9000` ⇒ worker 启动即报「归档配置非法: Endpoint url cannot have fully qualified paths」而**退出**。这类问题只在真正启动的那一刻才暴露，代价远高于在适配层多写十行归一化代码 |
| `payload_json` 是两侧唯一**没有 proto 约束**的通道，字段名写错不会有任何报错 | 它只表现为「那个功能一直是 0」——本次成本核算的真实经历：Python 改了字段、Go 仍读旧名，两侧单测全绿而线上成本恒为 0。因此：解析失败**不得**让整条事件失败（那会丢状态迁移），但必须把错误放进 payload（`raw_parse_error`）随事件流可见，绝不静默吞掉 |
| 成本的**推导项**（渲染时长/配音字数）不进持久化状态，一律读取时现算 | 存起来就得同步，而「两个口径不一致」在成本数字上格外难查：没有报错、没有告警，只是一个数字悄悄偏了。现算则天然与分镜表一致。为此用**两个类型**分开：`LLMUsage`（Python 上报、持久化）与 `Cost`（面向调用方的快照）。合成一个「部分字段有值」的类型，就等于让某处顺手把推导值写回存储 —— 两个来源的开端；分成两个类型，编译器替我们守住「谁能写进存储」 |
| 任务级数据（如成本）在事件处理里必须放在 `shot_id` 的**提前返回之前** | 否则只有带镜头的事件才会被记账，任务级事件里的数据被静默丢弃。成本这类数据不挂在任何镜头上，很容易写错位置，且写错后表现为「有时记得上、有时记不上」 |
| TTS 成本必须按**字符数**统计，且只算**真的产出了配音**的镜头 | 一是中文一个字 3 字节，用 `len()` 会把成本算成三倍；二是若把「所有镜头」都计入，TTS 整体失败时成本看起来照样正常，正好掩盖了真正的问题 —— 而「没配上音」恰恰是最该被看见的那件事 |
| **改了 Python 代码必须重启 AI 服务**，否则 Go 侧只会看到旧格式 | 本次真实踩到：`cost` 字段加好后跑真任务，final 事件里**根本没有 `cost` 键**，看起来像 Go 侧解析坏了。真实原因是 AI 服务进程启动于改动之前。观测手段是看事件流里的 `payload_json` 原文 —— 它一眼就能区分「上游没发」和「下游没解」 |
| `config.AIConfig.StreamTimeout` 的**零值**会让 `RunPipeline` 立刻报 `DeadlineExceeded` | `context.WithTimeout(ctx, 0)` 立即到期，报出来的是「ai: Python 大脑不可达 … DeadlineExceeded」—— 与真实原因（配置缺字段）毫无关系，排查方向被完全带偏。手写 `Config` 的测试与部署都必须显式给值 |
| 集成测试里构造的跨语言 payload 必须**照抄真实形状**，尤其是数字枚举 | 分镜表里 `tag`/`engine` 是**数字**；写成 `"MATH"` 会让 `encoding/json` 整体反序列化失败，表现为「导演跑完了但一个镜头都没有」，而两侧单测都还是绿的（`pbconv` 对 `ShotSpec` 的解析是 all-or-nothing）。构造这类 payload 时先去看 `processor_test.go` 里固化的真实样例 |
| 用例必须**自己清理**它依赖的环境变量 | `TestArchiveEnvDefaultsWhenUnset` 漏清 `SCID_ARCHIVE_BACKEND`，于是它读的是**开发者当前 shell** 的配置：本地 `source .env`（`ARCHIVE_BACKEND=s3`）时必然失败，干净 CI 上却是绿的。只在别人机器上红的测试比没有测试更浪费时间 —— 「缺省值」类用例必须把所有相关变量显式清空 |
| 「网络隔离」必须**实跑一条最小命令**探测可用性，不能只检查命令存在或看到 Linux 就假定可用 | 本机 `unshare -n` 返回 `Operation not permitted`（非 root 无权建网络命名空间），只有 `unshare -rn` 可以。若只 `shutil.which("unshare")` 就宣称已隔离，安全假设会在最需要它的机器上悄悄失效 —— 而**谎报「已加固」是安全代码里最危险的错误**（与 `memory_limit_enforced_by` 同一条原则） |
| 要了隔离却拿不到时，`require` 模式必须**拒绝执行**（fail closed） | 静默降级成「不隔离」的后果是一切的都正常：渲染成功、任务完成、界面无异常，只是沙盒里的代码能随便外联。这种失败没人会发现，所以必须让它在配置层面就响 |
| 用 `unshare` 包裹命令时**绝不能加 `--fork`** | 它默认用 `exec` 换成目标命令（**同一 PID**，已实测），内存探针读 `/proc/<pid>/status` 的 `VmHWM`、杀进程树都指向真进程。加了 `--fork` 就多一个父进程，峰值内存变成读 unshare 自己 —— 数字小到离谱，且**没有任何报错** |
| 新的网络命名空间里 **loopback 是 DOWN 的** | `connect("127.0.0.1", ...)` 直接报 `Network is unreachable`。实测结论是**不影响现有引擎**：Playwright 用 `--remote-debugging-pipe`（管道而非 TCP）与 Chromium 通信，隔离前后截图逐字节相同。因此刻意不折腾 loopback —— 多一步就多一处会坏的地方；同时留一条用例把这个事实钉住，将来真有引擎需要它时能给出指向原因的失败，而不是让人对着「连接被拒」猜 |
| `RLIMIT_AS` 的「兜底」对**浏览器进程是致命的** | Chromium 会预留极大的虚拟地址空间：逐项二分确认 `RLIMIT_DATA` 2GB/8GB 正常、`RLIMIT_NPROC` 与 `setsid` 也正常，而 **`RLIMIT_AS` 8GB/32GB 都让它瞬间 SIGTRAP**（`exitCode=null, signal=SIGTRAP`）。这条目前不构成线上故障（HTML 引擎的 Chromium 不经过 runner，是在服务进程内直接起的），但谁要是为了「把浏览器也沙盒化」而把它交给 runner，就会遇到**看起来像浏览器崩溃、实际是资源限制**的失败 |
| 断言「外联被挡住」必须带**反向对照** | 少了它，这条断言在一个本来就上不了网的机器（CI 常见）上**永远为真** —— 用例是绿的，却只是证明了这台机器没网，而不是隔离生效。必须同时断言：同一段代码、同一台机器、只是不隔离，就**能**连出去 |
| 复用「某个动作在沙盒里能不能成功」做安全结论前，先确认这条路径**真的走了沙盒** | 我加的第一版用例断言「Chromium 在隔离下仍能渲染」，失败后才发现 `HtmlRenderer._capture` 是在服务进程里直接 `sync_playwright()`，**根本不经过 `SandboxRunner`** —— 于是这条用例既没测到隔离，也暴露了一个真实缺口（HTML 引擎的生成代码目前仍在有网络的浏览器里跑）。断言之前先读调用链，别按名字猜 |
| 带 span 的 ctx 必须在 `c.Next()` **之前**装回请求 | 我第一版把它放在 `c.Next()` 之后，于是 handler 里 `c.Request.Context()` 拿到的仍是没有 span 的上下文 —— 入队时 traceparent 为空，worker 那条链路**自成一根**。这个错误格外隐蔽：两侧各自都有完整可看的链路，只是不在一棵树上，没有报错也没有失败。因此 worker 侧专门标了 `scidirector.trace_continued`：让「断链」变成一眼可见的布尔值，而不是靠人对着两个 trace ID 猜 |
| 链路上下文必须随**队列载荷**传递，并且收口在 queue client 上 | worker 与 api 是两个进程，中间隔着 Redis，Asynq **不传递任何上下文**。入队点有六处（三个 handler、WS 路径、worker 内部补偿入队），逐个去记得填一定会漏，而漏掉的表现是「这条链路的后半段没了」——不报错、不失败，只是查不到。把 `carryTrace` 放进 `EnqueueXxx` 内部就消除了一整类「忘了传」的缺陷 |
| 传的是完整 `traceparent` 而不是只传 trace ID | `traceparent` 同时带 span ID 与采样标志：worker 的 span 因此能挂在**入队那一刻的 span** 之下，采样决定也能继承。只传 trace ID 就得自己拼一个假 parent，采样标志也无从继承 |
| Grafana 里 trace_id、日志里的 trace_id 与 Tempo 的 trace ID **必须是同一个值** | 项目原本已有一套 `tr-` 前缀的 trace_id 语义（X-Request-ID 头、日志字段）。再加一层自动埋点就会出现「两个都叫 trace_id 的东西」：日志里写一个、Tempo 里存另一个。而「拿日志里的 ID 去查链路」是最常用的排查动作，查不到时人只会以为「链路没采到」，不会怀疑是 ID 不一致。因此 `TraceMiddleware` **自己**承担服务端 span 职责（不叠 otelgin），`TraceIDFromContext` 是唯一来源 |
| Tempo 的 search 接口会返回**去掉前导零**的 trace ID | 32 位里首位是 0 时，search 返回 31 位，而按原 ID 查又是能查到的。拿它跟日志里的 trace_id 做字符串比对会失败 —— 很容易误判成「链路丢了」。要按 ID 查就补零到 32 位 |
| 高频抓取端点（`/metrics`、`/healthz`）**不要**建 span | Prometheus 每 5 秒抓一次，每条留一个 trace 会把真正的业务链路淹掉：查 trace 时看到的全是 `/metrics`。后果不只是噪声 —— 它会把 Grafana 表格的 limit 占满，让真正要看的跨服务 span **挤不进来**，看起来就像「链路断了」。这是本轮真实踩到的误判 |
| Grafana 的 Explore 链接格式必须**照抄它自己生成的** | 新版是 `schemaVersion=1&panes={...}`，而且 `datasource` 要同时出现在 pane 与**每个 query 内部**。少了 query 内部那个，页面会静默退化成「没有选中数据源」的空白编辑器 —— 不报错，只是什么都不显示，极易被误判成「链路没采到」。正确做法是在 UI 里点一次、把 `page.url` 读出来当模板 |
| 现代 Grafana 的 Tempo 搜索是**前端插件**执行的，不走 `/api/ds/query` | 拿 `/api/ds/query` 去试 TraceQL 会得到 `unsupported query type: 'traceql'`（那里只认 `traceId` 等少数后端查询类型），从而误判成「配置坏了」。而后端 API 通不通，**证明不了面板能渲染** —— 验收标准的主语是 Grafana，就必须在浏览器里验 |
| Tempo 的 `Spans Limit` 默认只有 3 | 它会把每条链路截到只剩根 span 附近几个，于是「跨服务」这件事在表格里根本看不出来（worker 的消费 span 直接被截掉），看起来又像断链 |
| 容器里读挂载的配置文件报 `permission denied`，先看**文件权限** | 工具写出的文件可能是 `0600`，而容器里的进程不是 root，于是 Tempo/collector 起不来（`failed to read configFile ... permission denied`）。compose 挂载成 `:ro` 不改变可读性要求 |
| 观测栈的 `depends_on` 不要指向需要**构建**的服务 | Prometheus 抓一个稍后才出现的目标是完全正常的（先显示 down，应用起来后自动转 up），但 `depends_on: api` 会逼 compose 去构建 api 镜像 —— 观测栈不该把应用镜像的构建拖进来。本轮就是因此撞上不可用的基础镜像而启动失败 |
| 宿主上常被占用的端口要允许覆盖 | 本机 9090 已被 Clash 的 API 占用（这类冲突很常见），Prometheus 因此起不来。compose 里所有端口都应写成 `${SCID_XXX_PORT:-默认值}`，并在 `.env.example` 里说明 |
| 整份 JSON 存 Redis + 多进程读改写 ⇒ **加字段不是向后兼容的** | 任务是整份 JSON 存的，而 api 与 worker **都会**读改写它。旧版本进程把 JSON 反序列化进「不认识新字段」的结构体再整份写回，字段就被静默抹掉了。本项目真实踩到：新增 `tenant_id` 后只重启了 api，仍在跑的旧 worker 一碰任务就把它擦掉 —— 而读取时「缺失 = default 租户」，于是**任务的主人反而读不到自己的任务**（404），且它对 default 租户变得可见。这是**授权降级**，不只是数据丢失。两道防线：① `store` 写回时保留未知字段（`mergeUnknownFields`），让以后加字段真正滚动兼容；② 但这对**已经部署出去的旧二进制无效** —— 它们的写回逻辑里没有这段代码，所以「改结构体的字段就必须 api/worker 一起重启」这条部署纪律依然成立 |
| 越权与「不存在」必须**完全不可区分**，包括 `detail` 与错误码 | 返回 403 等于确认「这个 job_id 存在，只是不属于你」，可用来枚举。本轮的路由表用例抓出一个真实缺陷：approve/reject 曾经返回 **500 且 detail 里写着「任务不属于该租户」** —— 状态码与文案都把存在性暴露了。正确做法是两条路径共用同一个 `abortWith(...404, "任务不存在", nil)`（detail 传 nil，不把内部错误带出去），只在**服务端日志**里区分（否则运维查不到越权尝试） |
| 隔离必须放在**存储层**、并且有**路由表驱动**的用例 | 按 job_id 的接口有八个（含 WS）。逐个 handler 手写检查必然会漏一个，而漏掉的表现是「某个接口能读到别人的任务」——不报错、不失败，只会在某次审计里被发现。因此：读取收口到 `loadJobForTenant`、写入收口到 `UpdateJobForTenant`；用例则**从 `router.Routes()` 里取出所有带 `:jobID` 的路径**逐个以他人身份访问。手写清单会在新增接口时悄悄过期，而"悄悄过期"正是隔离最容易失效的方式 |
| 归属校验必须在**锁内**，不能「先查再改」 | 两步之间存在 TOCTOU 窗口：检查通过之后任务可能已被删除或改归属，操作对象已不是被检查的对象。`UpdateJobForTenant` 把校验放在同一把锁内、`mutate` 之前 |
| 多租户上线时，**历史任务**必须有明确归属 | 旧任务没有 `tenant_id` 字段。若判成「不属于任何人」就是一次"上线即数据丢失"；因此缺失按 `default` 处理。但**兼容不等于放开**：其它租户依然读不到它们（用例同时断言这两点） |
| 配额要做成**可自愈**的，不能用「创建 +1、结束 -1」的计数器 | 计数器要求两侧都别忘了；worker 崩溃、任务被清理、进程重启都会让它永久偏高，表现是「这个租户再也提交不了任务」而没有任何日志说明原因。改为维护在跑集合、每次检查时按任务**真实状态**剔除，集合就自愈了。代价是每次创建多读几次 Redis（上限 = 配额 + 少量），相对一次视频生成可以忽略 |
| 配额这类「保护性能力」自己坏掉时应当**放行**而不是拒绝 | 读配额失败就返回 500，等于让一次 Redis 抖动把整个服务变成不可用 —— 保护机制不该比被保护的东西更脆。正确行为是记警告后放行（并且把「少算一个」和「多算一个」的代价想清楚：误拒的表现是用户提交不了任务且不知为何，比偶尔放宽一次严重得多，所以 `GetJob` 失败时按"已结束"处理） |
| `config.AIConfig.MaxRecvMsgSizeMB` 的**零值**会让每个 gRPC 调用失败 | 它被原样当成 0 字节上限，报错是 `received message larger than max (14 vs. 0)` —— 看起来像"消息太大"，实际是"上限没配"。手写 `Config` 的调用方（测试、脚本、别的二进制）很容易漏这个字段；config 的缺省值是 8，所以线上正常，只有程序化构造时会踩。已在 `NewClient` 里给防御性缺省 |
| 双写状态的对账，判断规则必须做成**纯函数** | 「什么算不一致」一旦算错，表现是「对账报告全是误报」，而误报会让人干脆不再看这份报告 —— 那比没有对账更糟。因此比较逻辑不碰 IO，只吃两侧快照，可以由用例完整钉住（含"不该报的没报"这类反向控制） |
| 「后端不持久」必须单独成类（`unknown`），**不能**算成不一致 | 内存 checkpoint 在进程重启后什么都没有，此时「checkpoint 里没有该线程」是**预期**行为。把它当差异并自动"修复"，会让本地开发环境里每个在跑的任务都被误改状态；而在报告里，它还会让对账永远是红的 —— 永远红的报告等于没有报告 |
| 自动修复只允许做**一类**，且依据必须完全来自被修的那一侧 | 只修「Redis 非终态 + checkpoint 显示图已跑完」（最后那次状态迁移丢了）。终态由 **Redis 自己的镜头状态**推出，绝不引入 checkpoint 的信息去改写对外状态 —— 后者等于"用内部状态改写用户看到的东西"，风险远大于它解决的问题。其余各类只报告：它们的正确做法依赖运维判断（该不该重投？checkpoint 是不是被误删？） |
| 「Redis 已终态而 checkpoint 还没跑完」**不能**自动修 | 图可能继续推进并发出新事件，把 Redis 拉回非终态是危险的 —— 用户可能已经看到"已完成"。这一类单独成一个种类，并显式断言它 `repairable=false`（也做了变异验证：把它标成可修，用例立刻变红） |
| 修复必须**幂等**，并且必须写事件 | 周期扫描每 30 秒跑一次，不幂等会把事件流刷满，并让"修复了几次"这个信息失去意义 —— 因此写入前要二次确认（从读到写之间有窗口，任务可能已自己走到终态）。**必须写事件**：修复改变了用户看到的状态，而事件流是前端唯一的推送来源；不写就会出现"接口读出来已完成、页面还停在 100% 转圈" |
| 读 checkpoint 失败必须**向上返回错误**，不能当成"没有差异" | 把读失败当成一致，会让对账在 AI 服务挂掉的时候永远显示"一切正常"——而那正是最需要它工作的时刻 |
| 「有哪些任务在跑」这个问题**没有现成答案**：任务是以单键（`scid:job:<id>`）存的，没有索引 | `SCAN` 可以回答，但与键总数成正比、还会扫到大量带 TTL 的历史任务，不适合周期性执行。改为维护一个**全局在跑集合**（与租户配额集合同一套"现算自愈"机制：写入时登记、读取时按任务真实状态剔除）。**要理解的限制**：集合在任务进入终态时会被剔除，因此它覆盖的是"从未变成终态"的任务 —— 而那正是真实故障（最后一次状态迁移丢失）的形状。我第一版故障注入造的是"终态又变回非终态"，那在现实中不会发生，也就测不到该路径 |
| 周期对账不要用 Asynq 的 Scheduler | Scheduler 会让对账与业务任务共享并发槽位：一次积压（几十个渲染排队）就会让它迟迟不跑 —— 而那恰恰是最可能产生不一致、也最需要它的时刻。改用一个独立的 ticker goroutine，并给每轮扫描独立超时 |
| 逐任务调外部服务的能力必须有**单轮上限**与**独立超时** | 对账要逐任务调 Python 读 Postgres，任务多时一轮可能很久。设上限（50）并分多轮扫完，好过让一个 goroutine 长时间占着、日志间隔变得不可预测 |
| 把对账做成**共享包**而不是塞进 worker | worker 与 api 都要用它（周期扫描 vs 按需接口）。留在 worker 里会让 api 反向依赖一个背着媒体/归档/队列的包；独立成 `internal/reconcile` 后 api 只需要 store + ai 客户端。接口定义在**使用方**（httpapi 声明一个方法的接口），实现放在哪、内部怎么组织都与它无关 |
| 只读根**不能**只重挂 `/`：`mount -o remount,ro,bind /` 只作用于根那一个挂载 | 本机 `/vol1`、`/vol2` 是独立的 btrfs 挂载，`/boot/efi` 是 vfat —— 只重挂 `/` 之后，往 `/vol1/...` 写文件**照样成功**。我第一版就是这么写的，测试当场把 `blocked.txt` 写进了宿主机的项目目录。正确做法是遍历 `/proc/self/mountinfo`，把**每一个真实文件系统**都重挂为只读（按**挂载类型**跳过伪文件系统，而不是按路径——路径会变、类型不会），再单独把工作目录绑定回可写。拿不准的一律按"没保护上"记进 gaps |
| 只读根会让 `/tmp` 也不可写，而 ffmpeg / Playwright 都要写临时目录 | 表现为"渲染莫名失败"。因此要在内层挂一个私有 tmpfs。**必须带 `size=`**：不设上限的 tmpfs 能吃掉整机内存，等于用一个新 OOM 风险换掉只读加固 |
| 把 tmpfs 挂到 `/tmp` 会**遮蔽**位于其下的工作目录 | 写进去的东西落在私有 tmpfs 上，命名空间外**看不到** —— 表现为"渲染成功但产物凭空消失"。本项目的 pytest `tmp_path` 正在 `/tmp` 下，这个坑是被测试当场逼出来的。现在检测到工作目录位于 `/tmp` 之下就**不**替换 `/tmp`，并如实记一条 gap，而不是假装 `/tmp` 可用 |
| 隔离设置的包装进程必须**用绝对脚本路径**启动，不能用 `python -m 包.模块` | runner 会把子进程环境裁剪到白名单（不含 `PYTHONPATH`），而子进程的 cwd 是工作目录 —— `-m` 因此找不到包，包装器直接 ModuleNotFoundError 退出、命令根本没跑。而它的表现只是"状态报告缺失 ⇒ read_only_enforced=false"，**看起来像"只读没生效"而不是"整个包装器没起来"**。`exec_guard` 只依赖标准库，按脚本路径执行最稳 |
| 隔离的每一层都要**如实上报**，且"配置上启用了"不等于"实际生效" | 网络隔离由"配置 + 能力"决定（探一次即可）；只读则是**逐次执行**才知道结果（某个挂载点重挂失败就失效）。因此 `read_only_enforced`/`read_only_gaps` 来自子进程写回的状态报告，读不到一律按"未生效"处理（保守失败），并且**不生效时要打日志** —— 否则它只体现在一个没人看的字段里 |
| 只读根与 `$HOME` 缓存天然冲突，默认不能开 | matplotlib 字体缓存、LaTeX 缓存都写在 `$HOME` 下，而 manim/LaTeX 依赖它们。因此 `SCID_SANDBOX_READ_ONLY` 默认 **off**，开启前必须逐个引擎验证 —— 不能替使用者默认打开一个会让 manim 静默失败的开关 |
| 加了第二种隔离机制后，要及时删掉被取代的旧类 | `NetworkIsolator` 在 `isolation.Isolator` 接手包装职责后成了死代码，而且它的存在让"网络包装归谁"变成两个答案。删掉并把它的用例迁到新类上 —— 两个都叫"隔离器"的类迟早会被改歪一个 |
| 需求写「白名单」时，要先问**这份名单能不能被验证** | seccomp 的允许名单要求把目标程序用到的每个系统调用都列全。本机只装得起 ffmpeg（manim/LaTeX/Chromium 全部不可用），因此那份名单**无法被验证** —— 没验证过的允许名单比不加更危险：漏掉一个系统调用就让渲染静默失败，而"缺了哪一个"极难从现象反推。故实现为**拒绝名单**（拦沙盒里没有正当用途的那批：进程注入、内核模块、挂载/命名空间、密钥环、io_uring、perf），默认动作为 ALLOW、被拦的返回 **EPERM 而不是杀进程**（意外的调用会得到一个普通错误，程序往往还能降级继续）。偏离需求措辞这件事必须写在文档里，而不是默默换个做法 |
| 在用户命名空间里**我们是 root**，所以提权类系统调用并非天然不可达 | exec_guard 自己就用 `mount` 做只读设置 —— 说明沙盒里的代码同样可以重新挂载、甚至 `pivot_root` 换根。因此 `mount`/`umount2`/`pivot_root`/`setns`/`unshare` 必须显式拦掉，不能想当然认为"没有权限" |
| seccomp 必须**最后**安装 | 它不可逆（装上卸不掉），而只读设置本身要用 `mount` —— 而 `mount` 就在拒绝名单里。顺序反了会让 exec_guard 把自己拦住 |
| seccomp 只需要**一个包装进程**，不需要命名空间 | 它是 per-process 的属性，因此"只开 seccomp"时不必经过 `unshare`：少一层进程、少一次 unshare，就少一处会坏的地方。`Isolator.wrap` 因此按需组合：全关→不包装；只 seccomp→直接挂 guard；需要网络/只读→才加 `unshare` |
| 两个加固手段必须能**独立启用**（用显式 flag，而不是"看有没有传某个参数"） | 我第一版让 guard 用"有没有传 --workdir"来推断是否做只读，于是"只想要 seccomp"的人被迫接受只读 —— 而只读会破坏 `$HOME` 缓存。现在用显式 `--readonly` / `--seccomp` 开关 |
| 被 seccomp 终止必须归成**单独一类** `killed_reason` | 我们的过滤器返回 EPERM 不杀进程，正常不会走到；但一旦真的出现，`returncode` 是 -31，而"信号 31 是什么"没人能一眼认出来 —— 现象会变成"渲染莫名失败"。归类成 `seccomp` 后，日志与上层能直接指出是沙盒的系统调用策略拦下了它 |
| 只报"拦了 N 条"是不够的，必须同时报**未解析**的条目 | 名单里某个系统调用在当前内核上不存在时（例如旧内核没有 io_uring），那条规则**根本没装上**。只报条数会让人以为每条都生效了。用例直接断言"装上 + 未解析 = 名单总数"，不允许有"既没装上也没记录"的条目 |
| 被拒绝名单拦下的调用要能被**反向验证** | 与只读那层同理：断言"ptrace 被拦住"必须配一条"不启用时 ptrace 成功"的对照，否则在某些默认禁止 ptrace 的容器里，这条断言会永远为真 —— 那时它证明的只是环境如此 |
| 「沙盒覆盖了渲染」这句话必须逐个引擎核对**执行路径**，不能看名字猜 | 三个 HTML 引擎（d3/echarts/code_anim）此前是在 **AI 服务进程内**直接 `sync_playwright()` 起 Chromium 的 —— 完全不经过 `SandboxRunner`，于是网络隔离/只读/seccomp 对它一律无效，LLM 生成的 JS 可以在**有网络的浏览器**里跑。这是沙盒覆盖面上最大的一个洞，而它藏在一个"看起来已经接入沙盒"的项目里。判定方法只有一个：**读调用链**，看目标进程到底是谁起的 |
| 按**脚本路径**执行的模块会把自身目录塞进 `sys.path[0]`，从而**遮蔽标准库同名模块** | `scidirector_ai/` 里有本项目自己的 `logging.py`。以脚本路径运行 `html_capture.py` 时，playwright 内部的 `import logging` 拿到的是**我们的模块**，报 `partially initialized module 'logging' has no attribute 'Formatter'` —— 错误信息完全指不到真正的原因（目录遮蔽）。修法是在脚本开头把自身目录从 `sys.path` 摘掉 |
| `RLIMIT_AS` 兜底的下限必须**实测**，不能凭感觉取小 | 原值 2GB（"够 ffmpeg 抽帧"），而 Chromium 预留的地址空间极大：逐项二分的结果是 **32GB 崩、64GB 正常**。于是任何经过 runner 的浏览器进程都会在启动瞬间 `SIGTRAP`，报错里没有任何可读信息 —— 看起来像"浏览器坏了"，实际是资源限制。现取 128GB（留一倍余量），它仍拦得住"预留 TB 级地址空间"这种真正病态的行为 |
| 环境覆盖（`SCID_CHROME`）要放在**策略层**，不能塞进可注入的**机制函数** | 我第一版把覆盖判断加进 `_probe_browser_with()`，而那个函数是被测试注入假工厂的地方 —— 于是真实环境变量盖掉了注入的对象，三条既有用例当场变红。`browser_ready()` 是策略（覆盖优先 or 去问 Playwright），`_probe_browser_with()` 是机制（问 Playwright 要路径），分开之后两者都能单独测 |
| 用例必须自己清掉会影响它的环境变量 | 缓存用例依赖"真正走到探测分支"，而 `SCID_CHROME` 会**短路**探测 ⇒ 不带该变量的机器上绿、带了的机器上红。与上一轮 `SCID_ARCHIVE_BACKEND` 是同一类问题，第二次踩到说明这条规矩值得写进检查清单 |
| 反向对照的目标要选**确定可达**的，不要依赖公网 | 浏览器外联的反向对照最初用公网地址（example.com），而本机公网可达性时好时坏 ⇒ 对照时而跳过，而"时跳过的对照"等于没有对照。改用**本机回环 HTTP 服务**后完全确定：不隔离时连得上、隔离时连不上（回环在新的网络命名空间里是 DOWN 的） |
| `page.evaluate` 里的代码是在**浏览器**里求值的 | 把 URL 写成 Python 变量、在 JS 里引用 `target` 会得到 `ReferenceError`；把 Python 风格的 `#` 注释写进 JS 字面量会得到 `SyntaxError: Invalid or unexpected token`。两处都是我这次真实撞到的 —— 参数要么内联进字面量，注释要么留在 JS 之外 |
| Playwright 的路径类环境变量要进沙盒的**环境白名单** | `PLAYWRIGHT_BROWSERS_PATH` 被裁掉时浏览器找不到自己，表现为"截图全部失败"，而原因（环境变量被裁）从错误信息里完全看不出来。白名单是按"是否是密钥"筛的，路径类配置容易被顺手漏掉 |
| 「在 `.env.example` 里写了」不等于「配置得上」：Compose 只注入 `docker-compose.yml` 里**显式列出**的变量 | `.env` 里的值只参与 `${...}` 插值，**不会**自动进容器。阶段五的九个开关（多租户 3、状态对账 1、沙盒 5）在代码与 `.env.example` 里都齐全，却**没有一个**出现在 compose 的服务 env 块里 —— 照着模板配置容器部署会完全不生效，且没有任何报错。这与 `SCID_S3_*`/`SCID_MINIO_*` 那次是同一类问题（本项目第二次踩到）。修法：在三个应用服务共用的公共锚点里统一透传（一次配置、处处生效），并加一条**可执行的检查**：清单里的每个开关必须**同时**出现 `NAME:`（保证被注入）与 `${NAME}`（保证 `.env` 的值读得到），缺一即为静默失效。变异验证：从 compose 删掉一行，用例立刻报「容器拿不到它」 |
| 容器里跑沙盒的命名空间隔离，需要先放宽容器自己的 seccomp | `unshare` 是 Docker 默认 seccomp 配置明确拦截的系统调用，容器内调用会得到 `Operation not permitted` —— 于是「开关配了也不生效」，而且看起来像代码问题。放宽（`security_opt: seccomp=unconfined` 或 privileged）等于用**容器层**的安全性换**进程层**的隔离，是一笔要想清楚的交易；compose 里**刻意不默认放宽**，只写明取舍与验证命令（`docker compose exec ai unshare -rn true`） |
| 多服务商下**模型名不能写死在部署文件里** | compose 里原先是 `SCID_LLM_MODEL: ${SCID_LLM_MODEL:-gpt-4o}`，于是选 deepseek 时会把 `gpt-4o` 发给 DeepSeek，报"模型不存在"—— 那个错误看起来像"模型下线了"。改成留空 = 用该服务商的出厂默认（`providers.py`），各家默认值集中一处 |
| 环境变量少一个前缀 = 功能静默失效 | compose 里设的是不带前缀的 `OPENAI_API_KEY`/`OPENAI_BASE_URL`，而 Python 侧 `env_prefix="SCID_"` 读的是 `SCID_OPENAI_API_KEY` ⇒ **容器部署永远进 mock 模式**，且没有任何报错（只是内容全是占位）。这类"部署写一套、代码读另一套"在本项目已经第三次出现，因此加了**可执行的检查**：清单里的变量必须在 compose 里同时出现 `NAME:` 与 `${NAME}`，且必须在 `.env.example` 里有据可查 |
| **真文本 + 未配视觉时绝不能退回 mock 审查** | mock 审查会返回**伪造的"审查通过"**——未经审查的画面进成片且无任何报错，比"审查失败"危险得多。规则：只有**整体** mock（文本目标本身是 mock）才用 mock 响应；否则视觉不可用一律抛错，由 Critic 按既有设计降级转人工（它从不伪造通过）。已用变异验证：把这条改回"退回 mock"，用例立刻变红 |
| 错误信息里的重试次数必须报**实际值** | 原先写死 `已重试 {llm_max_retries} 次`。加了"配置类错误不重试"之后，实际只发了 1 次请求却仍报"已重试 3 次"——一条谎报次数的错误信息会把人引去查网络抖动或限流，而真因是密钥/模型名写错。现按实际尝试次数分两种措辞（"配置类错误，未重试" / "已尝试 N 次"） |
| 401/403/404/400 这类**配置类错误不该重试** | 重试三次只会让失败晚 7 秒出现、把日志刷满同样的信息。判据用 `status_code` 属性而不是 SDK 异常类名：类名在版本间变过，而任何兼容客户端都会带这个属性 |
| 能力判断必须**先于**客户端复用 | 我第一版先做"视觉与文本同源就复用同一个客户端"，而"同源"恰恰包括**文本 DeepSeek + 视觉跟随 DeepSeek**（没有视觉能力）这种组合 ⇒ 视觉客户端非空、可用性检查被绕过，请求带着**空模型名**发了出去。顺序必须是：先判断能力，再决定建/复用 |
| 代理（agent）不要去读客户端的内部结构 | 我曾让 agent 用 `self.llm.text.model` 取模型名，于是所有替身 LLM 都得复刻 `.text`——13 条用例因此变红。**模型由客户端按 role 解析，agent 不传 model**：`chat_json(role="vision")` 会自动用视觉模型（我第一版这里用文本模型，服务端只会报"模型不支持图片"）。需要记录模型名时经 `Settings` 的 `text_target()/vision_target()` 取，那是配置层的稳定接口 |
| 各家模型名迭代很快，默认值只代表"当时可用" | `providers.py` 里的默认（gpt-4o / deepseek-chat / qwen-plus / qwen-vl-max）**不是契约**，`SCID_LLM_MODEL`/`SCID_VLM_MODEL` 永远优先。文档里要写明这一点，否则运维会以为必须用这些名字 |
| **`go.mod` 的 go 指令与 Dockerfile 里的 Go 镜像版本必须一致**，否则镜像根本构建不出来 | 加入 OpenTelemetry（otel v1.34 要求 Go 1.24）后 `go.mod` 升到 1.24，而 `backend/Dockerfile` 还钉着 `golang:1.23-alpine` ⇒ `go mod download` 直接失败：`go.mod requires go >= 1.24.0 (running go 1.23.12; GOTOOLCHAIN=local)`，api/worker 镜像**构建不出来**。而本地开发用的是宿主 Go（比镜像新），`go build`/`go test` 一切正常 —— **这类不一致在本地永远看不出来**，只有真正 `docker compose build` 的人才撞得到。已加用例（`internal/config/dockerfile_go_test.go`）把两者关系钉住，并做了变异验证 |
| 容器里没有 `make`，也**不该有** | `make` 是开发机上的工具，三个镜像（python-slim / alpine / nginx-alpine）都没装它 ⇒ 在容器里执行 `make ...` 得到 `bash: make: command not found`，看起来像 Makefile 坏了。文档要给出**等价的直接命令**，且诊断脚本本身要用纯 sh 实现（`scripts/doctor.sh`）—— 需要 make 才能跑的诊断工具，恰恰在"没有 make"时不可用 |
| 构建镜像与运行容器是**两条独立的依赖路径**，要分别验 | 我这一轮之前所有验证都是"宿主机跑进程 + 容器只跑中间件"，因此**从未构建过 api/ai/web 镜像** —— 上面那条 Go 版本不一致就是这样漏掉的。凡是"只在 CI/部署时才走"的路径，必须有至少一次真实执行 |
| Docker Hub 不可达时，`docker compose build` 会在 **load metadata** 阶段失败（401），而不是在拉层的时候 | 报错形如 `failed to resolve source metadata ... 401 Unauthorized`，看着像鉴权问题、实际是加速器（本机是 `docker.fnnas.com`）对 Docker Hub 也不通。修法是**用可用的镜像站显式拉取再打成本地 tag**（本例五个基础镜像：python/nginx/node/alpine/golang），之后构建直接命中本地镜像 |
| `context.set_offline(True)` **不会**断开已经建立的 WebSocket | 做「断网演练」时实测断网期间连接指示器仍是「实时」，演练等于没做。要真正切断实时通道，用「停掉 api 进程 N 秒再拉起」更可靠，而且顺带覆盖了「服务端重启」这个代码里明确设计过的场景。**脚本必须如实报告「本次演练其实没断线」**，而不是假装通过 |
| 就绪探测必须检查**二进制/可执行文件**，不能只看包能否 import | `playwright` 是两步安装：`pip install playwright` 只装 Python 包，浏览器要另跑 `playwright install chromium`。只做第一步时「导入探测」会报可用，健康检查于是宣称 d3/echarts/code_anim 都就绪 —— 一个会说谎的就绪探测比没有探测更糟，它把「环境没准备好」伪装成「内容不达标」，而两者该做的处置完全不同 |
| 同一件事的判定**只能有一份实现** | 「这个引擎能不能渲染」原先在 `config.toolchain_report()`（健康检查读它）与 `renderer.HtmlRenderer.available()`（渲染前自检）各写了一套，于是必然各说各话。现在共用 `config.browser_ready()` |
| 画面时长 ≠ 旁白时长，字幕不能按画面窗口铺满 | 一个 8 秒镜头可能只有 5 秒旁白，剩下 3 秒是留白。按窗口铺满会把最后一句字幕**拉伸着挂满那 3 秒** —— 声音早停了、观众早读完了，字幕还在，这是音画不同步最典型的形态。有配音时字幕应当只覆盖配音那一段（`PlanCuesWithNarration`）；而**没有配音的镜头必须回退成铺满窗口**，否则新逻辑会误伤原本正确的路径 |
| 同一个探测函数未必适用于所有输入：`Probe` 是**产物校验器**，不是通用探测器 | 它要求存在视频流（这是对的，空产物/纯音频产物都该被判无效），因此对纯音频文件必然失败。配音时长要另走一条只读 `format.duration` 的路径。第一版用例就是因为复用了 `Probe` 而失败 |
| 拼接音频时，没有配音的段要补**等长静音**，不能跳过 | 跳过会让整轨比画面短，其后每个镜头的声音整体前移 —— 「某段没声音」只是小瑕疵，「全片音画错位」是废片 |
| 自动化的「检查」必须带**负向对照**，否则它可能什么都没检查 | 断言「没有布局问题」的检查器一旦选择器写错，就会恒返回「通过」—— 比没有检查更糟，因为它让人以为已经验证过了。做法：故意注入一个坏元素、确认检查器会报、再移除并确认恢复干净。这与「只断言不超过会漏掉根本没并发」是同一条纪律 |
| 「能拿到时间戳吗」是 TTS 选型的**决定性**差异，不是音质 | 有句级时间戳（Edge TTS 实测返回 `SentenceBoundary` 带 offset/duration）时字幕可按真实句子起止对齐；只有音频（OpenAI TTS）就只能按镜头时长对齐、镜头内部仍按文本比例分。把这一点写进配置说明与验收标准，免得有人以为「接了某家字幕就一定准」 |
| 第三方接口的「未验证」必须写进代码与文档，且把可变部分做成配置 | 豆包/OpenAI/Fish 三家本机无凭据且端点不通，适配器只覆盖到「请求形状 + 错误分类」。做法：端点/cluster/成功码/模型名全部可配置，响应严格校验并把服务端 `code`/`message` 原样带出，并在适配器顶部明确标注「未经真实验证」—— 否则读代码的人会以为它跑通过 |
| 跨进程传路径必须 `resolve()`：生产者看着没问题，消费者那边就是「文件不存在」 | ai 服务跑在 `ai/`、Go worker 跑在 `backend/`，Python 侧最初把 `audio_path` 报成相对路径，worker 找不到文件 —— 而 worker **只降级不报错**（成片照出、只是没声音），极易被误判成「TTS 没配好」。§9 早有同源约定（子进程路径必须 resolve），这是跨进程场景的变体；修在写入方（统一 `write_audio`），四个适配器一起受保护 |
| 跨语言契约的测试 fixture 必须是**对方真实产出**的样本，不能自己编 | 自己编一份「看起来对」的 JSON 只能证明我们和自己的想象一致。而契约坏掉时的表现往往是**静默降级**（这里：字幕退回按时长对齐、成片照样出），不会有任何报错。`narration_marks_test.go` 用的就是 Python 真实写出的 sidecar 内容 |
| 「没有」与「用不了」必须分成两种返回，别都塞回 nil | sidecar 不存在 → 正常回退（该服务商不给时间戳）；sidecar 在但解析失败/版本不认识 → **两侧契约对不上**，必须让调用方留痕。都返回 nil 会让后者永远静默地用旧精度 |
| 网络调用**必须先重试再降级**：只试一次 + 只降级不报错 = 静默失去功能 | 实测访问 Edge TTS 会「第一次成功、紧接着连接被 reset」；而流水线原先只试一次，第一个镜头直接失去配音 —— 更糟的是失去配音只降级不报错，成片是「莫名没声音」，排查方向完全跑偏。降级本身是对的（画面是主体），但降级之前必须先重试几次。重试要：只重试**可重试**错误、退避带**抖动**（否则同批镜头一起重试成惊群）、**次数有限** |

---

## 10. 迭代日志

| 版本 | 阶段 | 变更 |
| --- | --- | --- |
| v0.1.0 | 阶段一 | 建立目录骨架、跨语言 proto 契约与生成流水线、docker-compose 全栈、Go 骨架（配置/日志/状态机/仓储/队列/gRPC 客户端/WS Hub/编排处理器/ffmpeg 封装）、Python 骨架（配置/日志/Schema/LLM 客户端/LangGraph 状态/导演智能体/沙盒策略/gRPC+FastAPI 双栈）、四份文档与构建脚本；Go 与 Python 测试全部通过 |
| v0.1.1 | 阶段一（修订） | **回退阶段二的提前实现**，把仓库收敛到经过验证的阶段一状态：移除编码/审查智能体、沙盒运行器、渲染与媒体工具、RAG、图拓扑与 checkpointer；`RunPipeline` / `GenerateShot` / `CritiqueShot` / `ReviseShot` 恢复为返回 `UNIMPLEMENTED`。保留阶段一两处前置能力（导演智能体、沙盒静态安全策略）。修复 `scripts/dev-env.ps1` 的 `PYTHONPATH` 顺序缺陷（`.pylibs` 必须置于**末尾**，否则会遮蔽版本更完整的同名包，表现为 pydantic 导入时莫名的 `cannot import name`） |
| v0.1.2 | 阶段一（修复） | 手工联调实测发现并修复两项语义缺陷：① `UNIMPLEMENTED` 不再被 Asynq 重试（新增哨兵 `ai.ErrNotImplemented`，worker 返回 `asynq.SkipRetry` 直接归档）；② `sandbox_ready` 改为「至少一个渲染引擎可用」并新增结构化 `engines` 字段，使实现与文档一致。新增 `scripts/smoke-grpc.py`、`scripts/smoke-ws.py` 两个可复用冒烟脚本与 `make smoke*` 目标；README 新增「手动测试」章节。Go 测试 +1 包，Python 测试 71 → 83 |
| v0.2.0 | 阶段二 | **Python 多智能体核心落地，Go 侧全链路打通**。沙盒执行器（超时 30s 强杀 + 内存上限，Windows Job Object / POSIX rlimit 双实现，留痕 `killed_reason`）→ Manim 沙盒（四层防御）→ 媒体层（抽帧含首末帧、lavfi 环境镜头、drawtext 三级降级）→ 渲染器抽象（Manim/Html/Ambient 确定性路由 + 就绪探测 + HTML 契约校验）→ 编码智能体（按标签选提示词、RAG 召回、策略校验后重写）→ 审查智能体（分维度加权 + 硬性下限 + 程序侧复核 + VLM 不可用降级转人工，中文强约束 JSON 提示词）→ RAG（可解释打分，中文走 CJK 二元组）→ LangGraph 图（带反馈循环、`route_hint` 路由、重试上限熔断转 `AWAITING_HUMAN`、Postgres checkpointer 显式降级）→ `pbconv` 双向转换 → 5 个 RPC 全部实现。拆分顺序按依赖自底向上，每层落地即测。Python 测试 83 → 368；`go vet` / `gofmt` / `go test ./internal/...` 全绿；`scripts/smoke-grpc.py` 25/25 通过；真实端到端任务 `job-799a0041b7ad3434` 产出 2 个可播放 MP4、2 个镜头按预期熔断。验收明细见 `docs/ROADMAP.md` |
| v0.3.0 | 阶段三 | **Go 编排与媒体处理**。①**FFmpeg 并发收敛**为两道防线：任务内 Worker Pool（`errgroup.SetLimit`，内存 O(limit) 而非 O(分镜数)）+ 进程级全局单例闸门（真正的 OOM 防线），并修掉「每次合成新建信号量导致全局上限随任务数成倍放大」与「`Probe` 完全绕过闸门」两个缺陷；进程生命周期加固（`WaitDelay` 兜住孙进程占用管道导致的 `Wait` 永久阻塞、自定义 `Cancel` 杀整棵进程树、单命令硬超时）。②**转场与统一规格**：`PlanTransitions` 纯函数（offset 必须基于累积游标）、音频同长 `acrossfade`、Normalize 显式转换并打标色彩范围（解决 Manim 与浏览器录制的质感割裂）。③**字幕**：窗口建立在转场后的时间轴、跨语言语音权重、最短显示时长、SRT 输出；软字幕 mov_text 封装。④**局部重渲染**：`PlanSplice` 纯函数 + 单次 filter_complex 拼接，按引擎能力如实降级（Manim 按动画序号渲染，无法按秒截取）。⑤**产物归档**：none/local/s3 三后端 + 保留策略（纯函数）。⑥**队列可观测**：`GET /api/v1/queue/stats`。测试：Go media 包 0 → 68 项、archive 包 27 项、queue 包 6 项；Python 368 → 377。**同时修复三处端到端才能发现的缺陷**：`MuxFinal` 的 `-map` 下标写死导致软字幕永远封不进去、`-shortest` 把末尾字幕压成零长、`filepath.ToSlash` 破坏 concat 引号转义；以及两处仓库级问题：`.gitignore` 未锚定的 `media/` 吞掉整个 Go 包、含中文的 `.ps1` 缺 UTF-8 BOM 导致脚本完全不可解析。**待办**：全片 TTS 配音、B4/B5 验收、s3 归档路径的端到端验证 |
| v0.4.0 | 阶段四 | **反馈闭环与前端**。web/ 从「连通性自检页」做成可用的分镜审核台：分镜表（含预览信息与三种人工操作）、事件时间线（倒序）、进度条、**始终可见的连接状态**；WS 断线重连（快照为权威基线 + seq 去重 + 空洞检测触发 `resync` + 带抖动的指数退避）；`PATCH /jobs/:id/shots/:id` 成分镜编辑（改 narration/visual_brief，可选立即重做）。前端把 C2/C3 的全部逻辑抽成**纯函数**（`stream.ts`）并单测 16 项。Go 侧新增 httpapi 集成测试 14 项（真实 Redis）：C4「打回只影响目标镜头」逐一断言其余镜头未变、C5「最后一个镜头放行触发合成」与「仍有镜头未通过时不触发」、PATCH 的指针语义与边界、**响应形状契约**。**联调中发现并修复 4 个缺陷**：放行后未入队合成导致任务永远停在 COMPOSING、前端未拆响应信封导致所有字段 undefined（页面静默空白）、`applyEvents(state,[])` 清空 job、无产物镜头被放行后成片静默缺镜头却标 COMPLETED。验收：**C4/C5 已用真实服务端到端验证**（放行 → `compose_enqueued:true` → worker 合成 → `COMPLETED`，成片含 h264(tv)/aac/mov_text 三路流）；C1/C3 的数据通路已验证，浏览器人工目视验证待做 |
| v0.4.1 | 阶段三（验收补齐） | **B4 / B5 补上自动化验证**，阶段三的待办只剩 TTS 与 s3。①**B4**：查清「重复投递只渲染一次」实际有**两道**闸门 —— 入队侧 `asynq.TaskID`（`Unique` 的键含载荷，换个 `human_comment` 就失效，因此不能只靠它）与执行侧 `AcquireIdempotencyKey`（专防 Asynq「执行成功但确认失败」后的重复投递，此时入队侧帮不上忙）。6 个用例：执行侧起**真实 gRPC server** 冒充 Python 大脑（只负责数 `ReviseShot` 调用次数）+ 真实 Redis，覆盖并发重复投递、完成后再投、**不同 attempt 必须各自渲染**（反向对照 —— 少了它，「永远跳过」这种退化实现也能过）、真失败后释放占位、`UNIMPLEMENTED` 释放占位且 `SkipRetry`；入队侧覆盖同 attempt 去重（含换意见）与不同 attempt 必须入队。②**B5**：用测试自己拉起的**私有 `redis-server`**（独立端口/目录）做真实故障注入，`SIGKILL` 模拟崩溃 —— 优雅关闭会主动落盘，掩盖 `appendfsync everysec` 到底够不够用，并在杀进程后确认端口真的连不上；同时把 `deploy/redis/redis.conf` 的 `appendonly`/`appendfsync` 纳入断言，防止「测试用显式参数、部署配置被改坏」这种分叉。两个用例：**排队中**的任务靠 AOF 原样恢复；**执行中**的任务靠租约过期 + recoverer 重投（受 Asynq 硬编码的 30s 租约 / 60s 轮询 / 30s 过期余量所限需 2~4 分钟，默认跳过，`make test-failover` 显式运行）。③**踩坑两处**：断线注入必须持续超过一个租约长度 —— 第一版只断开 1 秒就恢复，下一次心跳把租约续上，任务根本没被打断、正常跑完，recoverer 全程没参与，属于「看着恢复了其实什么都没验证」；集成测试的 Redis 库号必须**逐包分配** —— `go test ./...` 并行跑包，共用库号会互相 `FlushDB`，产生随机假阳性，而偶发红比没有测试更糟。Go 侧 `gofmt` / `go vet` / `go test ./internal/...` 全绿。**待办**：全片 TTS 配音、s3 归档路径端到端验证 |
| v0.4.2 | 阶段二/三（缺陷修复） | **修掉两处「静默失败」的资源限制缺陷** —— Python 侧此前 4 项失败全部转绿（370 → 376 passed）。①**内存上限用错了机制**：POSIX 侧把 `max_memory_mb` 直接映射成 `RLIMIT_AS`，而它限制的是*虚拟地址空间*（实测 ffmpeg 抽一帧 RSS 仅 56MB、地址空间却要约 2GB；1.75GB 建不起 swscale 图，低于 1GB 时连不做缩放的抽帧也失败）。后果不是「限制得严」而是**抽帧全部失败**：ffmpeg 退出码仍是 0、产物却是空的，于是 `frame_samples` 恒为空、Critic 对**每一个**镜头都降级转人工 —— 项目的核心闭环（VLM 视觉审查）在 Linux 上等于不存在，而全程没有任何错误日志，只有一句「抽帧失败，审查将降级」。这也解释了阶段二的 A3 等验收当时为什么能过：那是在 Windows 上，Job Object 限制的是提交内存而不是地址空间。现改为 `RLIMIT_DATA`（语义与 Job Object 一致），`RLIMIT_AS` 降级为留有余量的兜底。②**CPU 上限与墙钟超时在同一瞬间相撞**：`RLIMIT_CPU` 到点发 SIGXCPU（manim 侧把 CPU 上限设为超时×2），而墙钟兜底的截止是「超时 + 宽限期 5s」，小超时下两者精确撞在一起（实测 timeout=5 时 CPU 10s vs 墙钟 10s），谁先开火取决于调度；走 CPU 那条路时监控线程**不会**设置 `kill_flag`，`killed_reason` 于是成了空串，上层把「资源耗尽」读成「未知失败」。现按 SIGXCPU 归一化成 `timeout`（它只可能来自我们自己设的 `RLIMIT_CPU`，不存在误判），并让提示同时指出超时与 CPU 两个开关。③新增回归测试：抽帧在沙盒内存上限下必须真出帧、CPU 预算耗尽必须报 `timeout`（用 60s 墙钟 + 2s CPU 上限做确定性覆盖，不赌两个定时器谁先开火）。**这两处都是「看起来成功」的失败**：没有异常、没有错误日志，只有效果悄悄消失 —— 与 v0.1.2 / v0.3.0 记录的那类缺陷同源。踩坑记录见 §9 |
| v0.4.3 | 阶段三（验收补齐） | **s3 归档路径补上端到端验证**，此前它只被「构造期不报错」覆盖过，真刀真枪的建桶 / 上传 / 取回一次都没跑过。补法是两半合起来测：①**归档器本身**（`archive/s3_e2e_test.go`）—— 按需建桶、上传后用**独立客户端**读回并逐字节比对（只断言「没报错」会漏掉「传了个空对象」）、Content-Type（决定浏览器内联播放还是下载）、敌意文件名（jobID `../../etc` + 文件名 `..\..\evil file.mp4`）清洗后的键在**真实存储里**确实落在 `jobs/` 之下、端点不可达时如实报错；②**接进流程**（`worker/archive_test.go`）—— 归档成功才清理本地、**归档失败一个本地文件都不删**、未配置归档时清理照常生效，以及**真实归档器 + 真实 `finalizeArtifacts`** 的联合路径。第 ② 组此前**完全不存在**（`finalizeArtifacts` 只有调用点、没有任何测试），而它守护的不变式一旦被破坏，症状是**产物凭空消失**而不是报错：任务显示成功、事件流一切正常，用户点开成片才发现 404。新增 `make test-s3`，端点未配置时自动 SKIP。②**踩坑两处，都是用例自身的错，但都会导致误判**：`minio-go` 的 `ListObjects` 默认不递归、返回公共前缀（`jobs/etc/`），用它断言键落点等于断言空气，必须显式 `Recursive: true`；moto 等模拟器**不校验凭据**，所以「错误密钥必须失败」在模拟器上根本不成立，改用**端点不可达**来构造失败 —— 那是任何实现下都必须失败的情形。③**发现 MinIO 开源版已归档停更**：`dl.min.io` 返回 410，GitHub 最后一个 release（`RELEASE.2025-10-15`）没有任何可下载产物，官方声明不再提供安全更新。compose 里钉的 `minio/minio:latest` 可用性需重新确认，产线宜换仍在维护的 S3 兼容实现（RustFS / SeaweedFS / Garage）—— 选型属部署决策，**未擅自改 compose**。④验证边界已如实写进代码注释与 ROADMAP：本次跑在 **moto**（一个 S3 API 实现）上，尚未对生产将选的对象存储复跑；用例只依赖 S3 API、不绑定产品，换端点重跑一条命令即可 |
| v0.4.4 | 阶段五（基础设施） | **基础设施改由 Docker 承载，并把对象存储从 MinIO 换成 RustFS**。①`docker compose up -d redis postgres rustfs` 起齐 infra，Go 全量测试**对着容器里的 Redis 与 RustFS** 跑通（134 PASS / 0 FAIL / 1 SKIP）；此前 Redis 是我从 Debian 包里解出来跑在内存盘上的临时实例，重启即无。②**s3 端到端验证补上第二轮**：第一轮跑在 moto（S3 API 实现）上，只证明「对 S3 API 正确」；第二轮跑在**真实 RustFS 容器**上全部通过，至此「生产将用的对象存储」这一层才算真的补上。③**MinIO 开源版已归档停更**（`dl.min.io` 返回 410，最后一个 release 无可下载产物，官方声明不再提供安全更新），经确认换成同为 S3 兼容、仍在活跃维护的 RustFS；同时**删掉 `minio-init` 容器** —— 它依赖同样已归档的 `minio/mc`，而建桶本就在代码里（`S3Archiver.ensureBucket` 按需创建，正是端到端用例覆盖的路径），少一个容器也少一处「init 静默失败没人看」的隐患。④**修掉一处静默错配**：`.env.example` 与 compose 公共块写 `SCID_S3_*`，而 Go 侧只读 `SCID_MINIO_*` —— 没有任何代码读前者，照模板配 S3 等于没配。统一为 `SCID_S3_*` 并保留旧名回退，补齐 `config` 包的迁移语义用例（该包此前零测试）。⑤**两处只有真起容器才会暴露的问题**：RustFS 镜像**没有 bash**，健康检查用 `</dev/tcp/>` 会让容器永远停在 `starting`，改用其 MinIO 同名端点 `/minio/health/live`；compose 默认把 Postgres 发布到宿主 5432，而本机 5432 已被宿主自带 Postgres 占用（`address already in use`），用 `SCID_PG_PORT` 让开。⑥Docker Hub 在本机不可达（daemon 里的加速器也未生效，回退到 `registry-1.docker.io` 连接超时），改用**显式镜像站主机名拉取再打 tag** 解决，不必改 daemon.json；该做法已写进 compose 头部注释。踩坑记录见 §9 |

| v0.4.5 | 阶段四（验收补齐）+ 缺陷修复 | **C1/C3 用真实浏览器驱动真实全栈验证**，并因此暴露、修复了 4 个此前从未被发现的缺陷 —— 它们的共同特征是「页面看起来在工作」。①**来源允许列表里的 `*` 没被当成放开**：WS 的 `CheckOrigin` 与 REST 的 CORS 中间件都把 `*` 当普通字面来源比对，于是 `SCID_CORS_ALLOWED_ORIGINS=*`（最自然的「全放开」写法）让**每一次** WebSocket 升级都 403，实时通道整个死掉，而 REST 轮询仍在更新页面、看起来只是「有点卡」；REST 侧在本机还被 Vite 代理掩盖（同源，不需要 CORS）。`internal/ws` 此前**零测试**，补了 4 项来源校验用例，CORS 补了 4 项。②**实时推送不含的字段没人触发回源**：`onNeedJobRefresh` 只在**断线**时触发，而事件不含分镜明细、快照又是在「刚提交、还没拆解」那一刻生成的 ⇒ 分镜表**永远空着**而连接指示器一路显示「实时」。讽刺的是缺陷 ①（WS 全 403）会让 `onclose` 反复回源，从而**掩盖**这一条 —— 「通道坏掉」掩盖了「通道没被正确消费」。现按 300ms 节流在事件到达时回源。③**回源只合并 `job`**，丢掉同一信封里的 `stat`/`progress`：`deriveStat` 优先用服务端 `stat`，而 `stat` 只在快照里到达过（那时 `total=0`）⇒ 界面长期显示「表格 4 行、统计写着共 0 个分镜、进度 0%」—— 正是 `stream.ts` 注释里警告过的自相矛盾界面。新增 `applyEventsWithDetail` 三样一起合并。④**文档里的 S3 端点写法会让 worker 起不来**：`.env.example` 与 compose 写 `http://host:9000`，而 `minio-go` 的 `Endpoint` 不接受带 scheme 的 URL，worker 启动即「归档配置非法」而退出 —— 即**照着文档配置就无法启动服务**；新增 `splitEndpoint` 归一化两种写法并按 scheme 推断 TLS。验证产物：`scripts/smoke-web.py` + `make smoke-web`（C1 断言分镜出现与状态推进、进度单调；C3 真切断实时通道 10 秒后断言**补齐**与**不回跳**），并产出截图供人工目视。踩坑另记两条：`context.set_offline(True)` **不会**断开已建立的 WebSocket（断网演练会变成空跑，脚本因此支持 `--api-restart-cmd` 并在未真正断线时如实报告）；截图只证明观感、断言只证明通路，两者不可互相替代。**人工目视那一半仍待做** —— 自动化证明不了「画面看起来对不对」。 |
| v0.4.6 | 阶段二（缺陷修复） | **修掉「引擎就绪探测谎报可用」**。装上 `playwright`（只有 Python 包、没装浏览器二进制）后，`/healthz` 把 `engine:d3/echarts/code_anim` 全报成 ok，而实际渲染必失败。根因有两处、且健康检查走的是前者：`config.toolchain_report()` 与 `renderer.HtmlRenderer.available()` 都只做 `import playwright` 探测，都不检查浏览器二进制 —— 同一件事写成两套判定，必然各说各话。现抽出**唯一一份** `config.browser_ready()`（真正解析 `pw.chromium.executable_path` 并确认存在，带缓存以免健康检查反复起 driver 子进程），两处共用；修复后本机如实报告 `d3/echarts/code_anim=False, stock=True`。**顺带更正上一版的一处错误归因**：上一版把「DATA 镜头烧满 3 次 attempt」也算在这次探测头上，复核后发现那 3 次来自**编码智能体的契约修复循环**（`check_html_contract` 在 `agents/coder.py` 生成代码时调用，不在渲染路径上；mock LLM 产不出带 `window.__seek` 的 HTML，按设计重写 3 次后熔断，即阶段二 A7 的行为）—— 修好探测后重跑同一任务，`#2 DATA` 仍是 `attempt=3`，因为那条路径与引擎可用性无关。**探测说谎确实存在且已修，但「它导致 3 次 attempt」是把两个现象错误地归到了一起**；错误的归因比没有归因更危险，它会让人以为「修好探测就不浪费 attempt 了」。已同步更正 ROADMAP。验证：Python **380 passed / 3 skipped**（此前 375/4 —— 新增 4 项探测用例，且原本因「已装 Playwright」而跳过的 `test_unavailable_environment_fails_non_retryably` 现在真的跑起来了，这正是修复生效的自证）。 |
| v0.4.7 | 阶段三（配音链路的可并行部分） | **把 TTS 链路里「与服务商无关」的那一半做完并验证**，选型定下来后只剩「产生音频」一步。①`Runner.ProbeAudio`（只读 `format.duration`；**不复用 `Probe`** —— 后者是产物校验器、要求视频流）；②`Runner.BuildNarrationTrack`：逐镜头对齐到各自画面时长（短则补静音、长则截断）后 concat 成整轨；③`PlanCuesWithNarration`：字幕只覆盖配音那一段，**末尾留白不再被字幕占满**；④合成路径把 `narration.m4a` 传给 `MuxFinal`（此前恒传空串），无配音时不改变任何行为。三条语义：画面比旁白长→字幕在旁白结束处收尾（旧行为会把最后一句拉伸着挂满多出来的时间）；旁白比画面长→夹到窗口末尾（并提示该镜头画面需加长）；没有配音→回退铺满窗口，且整轨里补**等长静音**（跳过会让其后所有镜头的声音整体前移）。验证全部用**合成的正弦音**，不依赖任何 TTS 服务商：media 包 4 项纯函数 + 4 项真实 ffmpeg 用例（用 `volumedetect` 证明音频真的被拼进去了，而非整轨静音）；worker 包 1 项**端到端**用例（真片段 + 真配音 → 真 `HandleComposeJob` → 断言成片有音轨、时长等于两段画面之和、1.2s 旁白的镜头字幕确实在 1.2s 收尾、而无配音的镜头仍铺满窗口）。Go 侧 144 → **153 PASS / 0 FAIL / 1 SKIP**。**仍待办**：音频合成本身（需先定服务商；关键差别不在音质而在能否拿到时间戳）。 |
| v0.4.8 | 阶段四（验收补齐） | **给 C1/C3 的浏览器验证补上程序化布局断言**。数据断言（进度、统计、补齐、不回跳）全绿，并不代表页面能用：元素越出视口、文本被裁切、行与行重叠、进度条与统计对不上，这些既不报错也不让数据断言失败，却能让人根本用不了这个页面。新增的检查：整页无横向溢出；关键元素（统计行/进度条/连接状态）存在、有可见尺寸且不越界；分镜行高度合理、不越界、互不重叠；长文本未被裁切（仅在 `overflow:hidden` 时算问题，省略号是有意设计）；进度条宽度与统计里的百分比自洽（同一份状态的两种呈现必须一致）；统计说「共 N 个分镜」时表格行数必须等于 N。**关键是这组检查自带负向对照**：脚本故意注入一个 2px 高的分镜行，确认检查器确实会报（实测识别出 1 个问题），再移除并确认恢复「无问题」—— 一个恒返回「通过」的检查器比没有检查更糟，它会让人以为布局已经验证过了（与 v0.4.6 的「探测谎报可用」同类）。**边界照旧如实说明**：这仍然**不能**替代人工目视 —— 好不好看、信息密度是否合适，机器判不了；截图在 `.data/smoke-shots/`，需要人看或换支持图像输入的模型。 |
| v0.4.9 | 阶段三（TTS 适配层） | **按用户要求接入四家 TTS 服务商的适配层**（`ai/scidirector_ai/tts/`）：统一接口 + 按需导入（用 Edge 的人不必装豆包/OpenAI 的 SDK）+ 工厂（缺省关闭，不影响既有行为）。四家的**决定性差异不是音质而是能否拿到时间戳**：**edge** ✅ **真实验证** —— 免费无密钥，实测返回**句级** `SentenceBoundary`（三句话拿到 3 条 offset/duration，音频 7.15s），音频与 sidecar 落盘回读全通；**doubao** ⚠️ 未验证（无凭据，官方文档页是 JS 壳抓不到正文）；**openai** ⚠️ 未验证且**不返回时间戳**（`audio.speech` 无时间信息）；**fish** ⚠️ 未验证，另有 `/v1/tts/stream/with-timestamp`（SSE）但可获取的规范未定义事件结构，故不臆造、暂未实现。时间戳经**音频旁的 sidecar JSON**（`<音频>.marks.json`，带版本号）交给 Go —— 与 `payload_json` 同一取舍（结构仍在演进，走 JSON 不必每次重生成两侧代码），且是**可选增强**：没有 sidecar 时 Go 回退到「按镜头真实音频时长对齐」（v0.4.7 已实现并验证）。错误按本项目约定分类（缺密钥/密钥无效/文本超长 = 不可重试；网络/5xx/429 = 可重试），并拦下两类「看起来成功」的失败：返回 0 字节音频、以及把 JSON 错误体当音频落盘。Python 侧 380 → **411 passed / 3 skipped**（新增 31 项：sidecar 契约、工厂、三家请求形状与错误分类、以及 Edge 的真实合成与句级时间戳）。**未做（下一步）**：图节点还没有调用适配层，因此镜头目前仍不产出 `audio_path` —— 适配层是库级别的「支持」，真正生效需要接进渲染节点并用真实 Edge TTS 跑一遍端到端。 |
| v0.5.0 | 阶段三（TTS 接入完成） | **把 TTS 接进流水线并用真实 Edge TTS 端到端验证**。渲染节点对每个镜头合成配音、写进 `RenderArtifact.audio_path` 并落句级时间戳 sidecar；失败**只降级不抛出**（画面才是主体，没旁白的镜头仍是可用产物），但一定留 warning —— 「成片没声音」是可见的质量差异，静默降级会让人误判方向。缺省 `SCID_TTS_PROVIDER` 为空 ⇒ 不合成、不产生额外文件，与接入前逐字节一致。**真实任务实测**（`job-d71b2f4265d6b760`）：镜头拿到 3.768s/4.344s 的真实配音；整片 `narration.m4a` 由两段拼成；**成片音轨 -22.5 dB（配音轨 -23.1 dB）证明真的不是静音**；**第二条字幕结束于 7.944s = 3.6 + 4.344（该镜头配音时长），而不是窗口末尾 8.5s** —— 也就是「声音停了字幕就停」，末尾留白不再被字幕占满；任务终态如实为 `PARTIAL`（两个无产物镜头被放行）。**踩到并修掉一个只有端到端才会暴露的坑**：ai 服务跑在 `ai/`、worker 跑在 `backend/`，Python 侧最初把 `audio_path` 报成**相对路径**，worker 那边就是「文件不存在」，而 worker 只降级不报错（成片照出、只是没声音），极易误判成「TTS 没配好」；修在写入方（统一 `write_audio` 做 resolve 并返回绝对路径），四个适配器一起受保护，补了 2 条回归用例。**仍待办（增量）**：句级时间戳 sidecar 已写出并落盘，但 Go 侧尚未消费 —— 当前字幕按「镜头真实音频时长」对齐，镜头内部仍按文本比例分配；再进一步需在 Go 侧读 sidecar。Python 侧 411 → **413 passed / 3 skipped**。 |
| v0.5.1 | 阶段三（句级时间戳对齐） | **Go 侧开始消费句级时间戳**，字幕从「按镜头时长比例分配」升级为「按真实句子起止」：`media.ReadNarrationMarks`（读 sidecar，**区分「没有」与「用不了」**：前者静默回退、后者返回 error 让调用方记 warn）、`media.PlanCuesWithMarks`（句数与时间戳条数一致时按句锚定，对不上退回比例分配）、`HandleComposeJob` 逐镜头读 marks 并在命中时记日志。价值在于**句间停顿不再被均摊**：比例分配下语音停一秒、字幕仍匀速推进，越往后越偏；用例 `TestPlanCuesWithMarksKeepsPauseBetweenSentences` 钉住「停 1.5s 时第二条字幕必须在 3.5s 出现，而不是比例算出的约 2.75s」。刻意**不做** `mergeToAtMost` —— 合并等于丢掉刚拿到的精度，短句一闪而过本就是语音的真实形态。**跨语言契约用 Python 真实产出的 sidecar 作为测试 fixture**，因为契约坏掉时是**静默降级**（成片照样出、只是字幕退回旧精度），不会有任何报错。Go 侧 153 → **165 PASS / 0 FAIL / 1 SKIP**（唯一 SKIP 是显式门控的慢用例 B5）。另：本轮机器重启过一次，`/dev/shm` 里的临时环境（venv、日志、解包的 redis）全丢，容器靠 `restart:unless-stopped` 自行恢复；重新拉取了 redis 二进制以让 B5 用例继续真跑。 |
| v0.5.2 | 阶段三（TTS 重试 + 真实复验） | **修掉「TTS 瞬时失败直接降级」**：实测本机访问 Edge TTS 会「第一次成功、紧接着连接被 reset」（`Connection reset by peer`，分类为可重试），而流水线原先**只试一次** ⇒ 第一个镜头直接失去配音；更糟的是失去配音**只降级不报错**，成片是「莫名没声音」，排查方向完全跑偏。新增 `synthesize_with_retry`（四个适配器共用）：只重试**可重试**错误、指数退避带 **±30% 抖动**（同批镜头同时失败时不至于一起重试成惊群）、**次数有限**（TTS 是增强，不该拖住任务）；新增 `tts_max_attempts`/`tts_retry_backoff_sec` 配置。修复后同一条流水线上两个镜头都拿到了配音。**真实任务复验**（`job-b754208870947990`）：两个镜头都命中句级时间戳，字幕起点都是 **0.100**（即 sidecar 里的 start_sec），镜头 3 的 cue 结束于 **7.938** = 3.6 + 4.3375（与 sidecar 逐项对上），成片音轨 -22.5 dB（确有声音）。另：本轮机器重启过一次，`/dev/shm` 临时环境全丢（容器靠 `restart:unless-stopped` 自行恢复），我重建了 venv 与日志目录、重新拉取 redis 二进制，并**重建了两个 Go 二进制**（否则跑的仍是旧版本 —— 上一轮就因此误判过一次「功能没生效」）。Python 侧 **415 passed / 5 skipped**；Go 侧 **165 PASS / 0 FAIL / 1 SKIP**。 |
| v0.5.3 | 阶段五（成本核算） | **按任务核算资源用量**：新增 `GET /api/v1/jobs/:jobID/cost`，任务详情里也带 `cost`。口径上有一条主线决策 —— **报用量不报金额**（换算成钱要单价表，而单价随服务商/模型/时段变，写死在代码里等于制造一个「看起来精确但已过时」的数字），以及**该谁报就谁报**：`llm.*` 只有 Python 知道（它持有 LLM 客户端）故由它上报并持久化，`render_sec`/`tts_chars` 能从事务状态推导故由 Go **读取时现算**。现算是刻意的：存起来就要同步，而「两处口径不一致」在成本数字上没有报错、没有告警，只是一个数字悄悄偏了。为把「谁能写进存储」变成编译期约束，用**两个类型**分开 —— `domain.LLMUsage`（上报、持久化）与 `domain.Cost`（面向调用方的快照）；合成一个「部分字段有值」的类型就等于给「某处顺手把推导值写回存储」留了门。另两处口径细节：TTS 只统计**真的产出配音**的镜头（否则 TTS 整体失败时成本看起来照样正常，正好掩盖真正的问题），中文按**字符**计而非字节（一个汉字 3 字节，用字节数会把成本算成三倍）。**测试 23 项新增**：domain 9（含反向控制：无产物时推导项为 0、重复上报是覆盖而非累加 —— 断点续跑会重放 final 事件）、pbconv 4（照抄 Python 线上格式的 `payload_json` 断言 `cost`→`llm_usage`；坏 JSON 必须让事件照常迁移并留下 `raw_parse_error`）、Python 侧对称的 4 项（`tests/test_final_event_cost.py`，用字面量逐字钉住键名与「值必须是数字」——**补这组用例的原因正是契约测试的不对称**：`payload_json` 两侧都没有 proto 约束，若只有 Go 侧钉着，改 `builder.py` 的键名会让两侧单测全绿而线上成本恒为 0；已做变异验证：把 `llm_total_tokens` 改名后该用例确实变红）、worker 4（真 Redis；**专门覆盖「任务级事件无 shot_id」** —— 记账若写在提前返回之后就会被吞掉，并对该实现做过**变异验证**：短路记账后用例确实变红）、worker 传输层 2（起真实 gRPC server 经 `RunPipeline` 发真实形状 payload 再由 Go 真实客户端接收落库）、httpapi 4。**真实任务端到端复验**（`job-db02acefed80f53d`）：接口返回 `render_sec=2.549 / tts_chars=40 / tts_shots=2 / shots=4 / approved=2`，从分镜原文独立重算**逐项一致**；两个 `AWAITING_HUMAN` 镜头贡献 0（反向控制在实际数据上成立），`tts_chars=40` 对应两份真实存在的配音文件（各带 marks sidecar）。**踩坑三处**：①「改了 Python 必须重启 AI 服务」再次应验 —— `cost` 字段加好后跑真任务，final 事件里**根本没有该键**，看起来像 Go 侧解析坏了，真因是 AI 服务进程启动于改动之前（看事件流里 `payload_json` 的原文能一眼区分「上游没发」与「下游没解」）；②`config.AIConfig.StreamTimeout` 的零值让 `RunPipeline` 立刻报 `DeadlineExceeded`，错误信息是「Python 大脑不可达」——与真因（配置缺字段）毫无关系；③修掉一处我上一轮自己引入的测试缺陷：`TestArchiveEnvDefaultsWhenUnset` 漏清 `SCID_ARCHIVE_BACKEND`，于是它读的是开发者当前 shell 的配置，本地 `source .env` 时必然失败而干净 CI 上却是绿的。**未验证**：真实 token 数走完全链路（本机 mock LLM **不产生用量**，故真实任务 `llm.*` 恒为 0；已用「usage 累加路径单测」+「非零数字过真实 gRPC 通道」两条证据补缺，但接上真 LLM 后仍需一次带 key 的实测）；**配额与限流未做**（验收只要求可查询，且它与多租户耦合）。Python **421 passed / 3 skipped**；Go **188 PASS / 0 FAIL / 1 SKIP**（另有 2 项 B5 故障注入用例经 `make test-failover` 显式跑通，含 62s 的「执行中任务」恢复用例）。踩坑记录见 §9 |
| v0.5.4 | 阶段五（沙盒加固） | **沙盒里的渲染代码现在真的连不出去了**：`SandboxRunner` 把命令包进 `unshare -r -n -- <原命令>`（`sandbox/netns.py`），新建的网络命名空间里没有路由也没有 DNS。**三条实测出来的事实决定了实现**：①本机 `unshare -n` 返回 `Operation not permitted`，只有 `unshare -rn` 可以 —— 所以可用性必须**实跑一条最小命令探测**，不能只查命令存在、更不能看到 Linux 就假定可用；②`unshare` 默认 `exec` 换成目标命令（**同一 PID**，已实测），内存探针读 `/proc/<pid>/status` 的 `VmHWM` 与杀进程树都仍指向真进程，因此**绝不能加 `--fork`**（加了峰值内存就变成读 unshare 自己，小到离谱且无任何报错）；③新命名空间里 loopback 是 DOWN 的，但**不影响现有引擎** —— Playwright 用 `--remote-debugging-pipe`（管道而非 TCP）与 Chromium 通信，隔离前后截图**逐字节相同**，因此刻意不去折腾 loopback（多一步就多一处会坏的地方），并留一条用例把这个事实钉住。**诚实汇报，而不是想当然的「已隔离」**：`ExecResult.network_isolation` 与 `/healthz` 的 `capabilities` 报**实际生效**的机制（`netns`/`none`），与 `memory_limit_enforced_by` 同一条原则；`SCID_SANDBOX_NETWORK_ISOLATION=require` 拿不到隔离就**拒绝执行**（fail closed）—— 静默降级的后果是一切的都正常、只是沙盒能随便外联，这种失败没人会发现。测试 11 项，**关键是带反向对照**：隔离下外联与 DNS 都必须 BLOCKED，而**同一台机器不隔离时必须 REACHABLE**（本次运行实际走到）—— 少了这条，断言在一个本来就断网的机器上会永远为真，那只是证明了这台机器没网，不是隔离生效。真实任务 `job-0479b151dae53242` 在隔离开启下照常渲染成功（`render_sec=2.782`、2 个镜头通过），且 Edge TTS 仍能合成（`tts_chars=40`），恰好说明**边界是对的**：沙盒里没网，而服务进程自身的对外调用（LLM/TTS）不受影响。**踩坑两处**：①`RLIMIT_AS` 这个「兜底」对浏览器是致命的 —— 逐项二分确认 `RLIMIT_DATA` 2GB/8GB、`RLIMIT_NPROC`、`setsid` 都正常，而 `RLIMIT_AS` 8GB/32GB 都让 Chromium 瞬间 SIGTRAP；目前不构成线上故障（HTML 引擎的 Chromium 不经过 runner），但谁要「把浏览器也沙盒化」就会撞上，且现象**看起来像浏览器崩溃、实际是资源限制**；②我加的第一版用例断言「Chromium 在隔离下仍能渲染」并因此失败，才发现 `HtmlRenderer._capture` 是在服务进程里直接 `sync_playwright()`、**根本不经过 `SandboxRunner`** —— 断言之前先读调用链，别按名字猜。**回归修复**：命令被包裹后「找不到可执行文件」的报错变成 unshare 的退出码 127、文案也变成 unshare 的，而既有约定是 **-1** + 一句中文说明（上层据此区分「部署缺工具链」与「渲染真的失败」），改为**先确认可执行文件再包裹**，并补上两种模式都要成立的契约用例。**未做到（这一项不算全部完成）**：HTML 引擎的 Chromium **未被隔离**（LLM 生成的 HTML/JS 目前仍在有网络的浏览器里执行；要补需把截图移进沙盒，而直接移会立刻撞上 `RLIMIT_AS` 那条）；容器级 `--network=none --read-only` 未做；`require` 的 fail closed 仅经单测覆盖。Python **432 passed / 3 skipped**；Go 188 PASS / 0 FAIL / 1 SKIP。踩坑记录见 §9 |
| v0.5.5 | 阶段五（可观测性） | **一次生成请求现在是一条跨语言的完整链路**：HTTP 入口（`scid-api`）→ 队列 → worker 消费（`scid-worker`）→ gRPC → Python 图节点（`scidirector-ai`），25 个 span 在同一棵树上，含各段耗时。落地方式：Go 侧新增 `internal/obs`（OTLP/gRPC 导出 + W3C `traceparent` 传播 + Prometheus 注册表）；`httpapi.TraceMiddleware` **自己**承担服务端 span 职责而**不叠 otelgin** —— 项目原本已有 `tr-` 前缀的 trace_id 语义（X-Request-ID 头、日志字段），再加一层自动埋点就会出现「两个都叫 trace_id 的东西」，于是「拿日志里的 ID 查链路」必然失效且不会报错；`TraceIDFromContext` 是唯一来源。链路跨进程靠两件事：入队时把完整 `traceparent` 存进载荷（**收口在 queue client 的 `carryTrace`**，六个入队点逐个去记一定会漏，而漏掉的表现是「后半段没了」）、出队时还原远端父上下文再起 consumer span；`otelgrpc` 负责把它写进 gRPC metadata，Python 侧用官方 `GrpcInstrumentorServer` 解出。Python 侧新增 `obs.py`（OTLP 导出 + FastAPI/gRPC 埋点 + 关闭时冲刷 span）与 7 个图节点的 `traced_node` span（span 名与事件流里的 `node` 字段同名，两套视图能直接对上）；日志里的 `trace_id` 回退到 OTel span 的 trace ID，保证两侧是同一个值。基础设施：`docker compose --profile observability` 起 collector → Tempo + Prometheus → Grafana，数据源与看板全部 provisioning（**看板是验收对象，必须能被一条命令复现**，手工点的配置在别人机器上复现不出来）。**验证**：`scripts/verify-observability.py` 用真实浏览器驱动 Grafana —— 断言 DOM 里真的出现三个服务、跨服务 span 名与耗时数值（判定依据是文本而非像素，因为模型读不了图；截图另存供人复核），并额外从 Tempo 程序化地取出整棵树作为第二份独立证据。**本轮踩坑（都是同一类：不报错、只是查不到）**：①带 span 的 ctx 装回请求的时机必须在 `c.Next()` **之前** —— 我第一版放在之后，导致入队时 traceparent 为空、worker 自成一根；这个错误极隐蔽（两侧都有完整链路，只是不在一棵树上），因此 worker span 上专门标了 `scidirector.trace_continued`，把「断链」变成一眼可见的布尔值；②`/metrics` 这类每 5 秒一次的高频抓取**不能建 span** —— 它会把 Grafana 表格的 limit 占满，让真正要看的跨服务 span 挤不进来，看起来就像断链（我因此误判过一次）；③现代 Grafana 的 Tempo 搜索由**前端插件**执行、不走 `/api/ds/query`（那里只认 `traceId`），拿它试 TraceQL 会得到 `unsupported query type`，差点误判成「配置坏了」——而后端 API 通不通本就证明不了面板能渲染；④Explore 链接格式必须照抄 Grafana 自己生成的（少了 query 内部的 `datasource` 会静默退化成空白编辑器）；⑤Tempo 的 search 会返回**去掉前导零**的 32 位 trace ID，字符串比对会失败，要补零；⑥容器读挂载配置报 `permission denied` 是文件权限（工具写出的文件可能是 0600）；⑦观测栈的 `depends_on` 不该指向需要构建的服务，否则会逼 compose 去构建 api 镜像；⑧本机 9090 被 Clash 占用，端口一律要可覆盖。测试新增 Python 8 项 + Go 11 项（含**变异验证**：把 `carryTrace` 改成「取到但不写回」后用例确实变红）。另补了一处**自己给自己挖的坑**：opentelemetry 之前没写进 `requirements.txt`/`pyproject.toml`，而配了 `SCID_OTEL_ENDPOINT` 却没装包时 `init_tracing` 会 ImportError —— 等于把「加了个可选能力」变成「部署可能起不来」。现改为**软失败**（记一条清楚的中文警告并返回 False），并要求 `tracer()` 也降级成 no-op：图节点每次渲染都会调它，在那里抛异常会让「没装可观测性依赖」变成「渲染全挂」。依赖同时补进 `pyproject.toml` 的 `telemetry` 可选组。Python **439 passed / 4 skipped**（第 4 项是 Edge TTS 网络抖动导致的按设计跳过，不是回归）；Go **199 PASS / 0 FAIL / 1 SKIP**。踩坑记录见 §9 |
| v0.5.6 | 阶段五（多租户） | **任务归属与隔离落地，并顺带把配额做完**（此前记为"与多租户耦合、待做"）。①**归属**：`Job.TenantID` 在创建时落定、之后不可更改（归属可变的话"谁有权看"就不可推理）；旧任务没有该字段，读取时按 `default` 处理 —— 否则上线即"数据丢失"，但**兼容不等于放开**（其它租户仍读不到）。②**隔离**：读取收口到 `store.GetJobForTenant` / `httpapi.loadJobForTenant`，写入收口到 `store.UpdateJobForTenant`（**校验在锁内**，否则是 TOCTOU）；WS 是另一个入口，同样校验（不能因为"REST 查过了"就跳过）。越权与不存在**完全不可区分**（同一个 404、`detail` 传 nil），只在服务端日志里区分以保证越权可审计。③**配额**：按租户限制在跑任务数（`SCID_TENANT_MAX_ACTIVE_JOBS`，缺省 0 = 不限制），用**在跑集合 + 现算自愈**而不是"创建 +1/结束 -1"的计数器 —— 后者在 worker 崩溃、任务被清理、进程重启后会永久偏高，表现为「这个租户再也提交不了任务」且无日志可查；配额检查放在 AI 探活**之前**（本地、确定、便宜且更具体的判断先做）。④**验证**：核心用例从 `router.Routes()` 里取出**所有**带 `:jobID` 的路径（8 条）逐个以他人身份访问，并断言响应与"不存在"逐字节一致（`trace_id` 除外）—— 手写清单会在新增接口时悄悄过期，而"悄悄过期"正是隔离最容易失效的方式；另带正向对照（本租户必须 200）与**变异验证**（把 `JobBelongsTo` 改成恒真后 8 条全红）。真实环境复验：acme 建任务后自己 200、globex 404、无租户头回落 default 404；配额 limit=2 时第 3/4 次提交返回 **429 且消息含确切数字 2/2**、另一租户不受影响。⑤**本轮抓出并修掉一个真实缺陷**：approve/reject 曾返回 **500 且 detail 里写着「任务不属于该租户」** —— 状态码与文案都泄漏了 job 的存在性（可被用来枚举有效 ID）。这正是路由表驱动的用例的价值：它是被测出来的，不是被想到的。⑥**另一个更严重的真实现象（授权降级）**：任务是整份 JSON 存 Redis，api 与 worker 都会读改写 ⇒ 加了 `tenant_id` 却只重启 api 时，仍在跑的旧 worker 一碰任务就把它**静默抹掉**，而"缺失 = default"导致**任务主人读不到自己的任务**、且它对 default 租户可见。两道防线：`store` 写回时保留未知字段（`mergeUnknownFields`，让以后加字段真正滚动兼容）+ 明确记录「对已部署出去的旧二进制无效，改结构体字段必须一起重启」这条纪律。修复后真实复验：任务跑完（PARTIAL）后 Redis 里 `tenant_id` 仍为 `acme`，acme 200 / globex 404。⑦**顺带修掉一个配置陷阱**：`AIConfig.MaxRecvMsgSizeMB` 的零值被当成 0 字节上限，让每个 gRPC 调用失败并报 `received message larger than max (14 vs. 0)`（看起来像"消息太大"，实际是"上限没配"）；已在 `NewClient` 给防御性缺省。⑧测试基建：`httpapi` 的 harness 起**真实假 AI gRPC server**（只实现 Health），使"创建任务的真实路径"（归属落库/配额/入队）不再被 `t.Skip` 掩盖。测试新增 Go 25 项（domain 5、httpapi 13、store 3 等）。Go **219 PASS / 0 FAIL / 1 SKIP**；Python **440 passed / 3 skipped**。踩坑记录见 §9 |
| v0.5.7 | 阶段五（状态对账） | **双写不一致现在能被发现并自动修一类**：新增 `GetCheckpointSnapshot` RPC（Python 侧只读、无副作用）+ `internal/reconcile`（Go）+ `GET /api/v1/jobs/:id/reconcile`（默认只读，`?repair=true` 才改状态）+ worker 的周期扫描（`SCID_RECONCILE_INTERVAL`，缺省关闭）。**边界划得很清楚**：只自动修「Redis 非终态 + checkpoint 显示图已跑完」这一类（最后那次状态迁移丢失），终态由 **Redis 自己的镜头状态**推出，绝不引入 checkpoint 的信息去改写对外状态；「Redis 已终态而图还没跑完」这类**不修**（图可能继续推进并发出新事件，把已显示的"已完成"拉回进行中是危险的），并在用例里显式断言它 `repairable=false`（含变异验证）。**为什么由 Python 读 checkpoint**：表结构与序列化是 LangGraph 的实现细节，让 Go 直接读 Postgres 等于把两个项目的内部实现焊在一起，一次 LangGraph 升级就会静默读出错数据。**判断规则是纯函数**（`domain.Reconcile`）：算错的表现是"报告全是误报"，而误报会让人不再看这份报告 —— 比没有对账更糟；因此 12 项用例里大量是反向控制（一致时不报、终态不报 stuck、非持久后端只报 unknown 不算不一致）。**真实环境端到端复验**：把一个已完成任务的状态改回 RENDERING 并放回在跑索引（**忠实复现**"最后一次状态迁移丢失"——任务从未变成终态，所以索引里也从未被剔除），周期扫描在下一个 tick 检出并修复 `RENDERING → PARTIAL`，写出 `node=reconcile` 事件（带 reason/from/to/backend，前端据此更新）；只读模式检出差异但**不改**状态、**不写**事件；连续三次 repair 只产生 1 条事件（幂等）；跨租户访问该接口 404。**踩坑三处**：①第一版故障注入造的是"终态又变回非终态"，而真实故障里任务从未变过终态 —— 注入不忠实就测不到真正要测的路径；②「有哪些任务在跑」这个问题**没有现成答案**（任务是以单键存的、没有索引），`SCAN` 与键总数成正比且会扫到大量 TTL 历史任务，不适合周期性执行，改为维护全局在跑集合（复用配额那套"现算自愈"）；③对账不用 Asynq 的 Scheduler —— 它会与业务任务共享并发槽位，一次积压就让对账迟迟不跑，而那恰恰是最需要它的时候。另修掉一处**自己上一轮留下的假绿**：collector 的 healthcheck 写了 `wget`，而该镜像是 distroless（连 sh 都没有），容器因此永久 `unhealthy`（而它其实在正常收 span）——已删除该 healthcheck 并注明原因，「永远为红的健康检查比没有更糟」。测试新增 Go 19 项（domain 12 + reconcile 6 等）、Python 5 项。Go **238 PASS / 0 FAIL / 1 SKIP**；Python **444 passed / 4 skipped**。踩坑记录见 §9 |
| v0.5.8 | 阶段五（沙盒加固·只读） | **补上「只读文件系统」这半**：`SCID_SANDBOX_READ_ONLY`（off/auto/require，**默认 off**）用「非特权挂载命名空间 + 把每个真实文件系统重挂为只读」实现，等价于容器级 `--read-only`；网络那半由上一轮的 `unshare -r -n` 负责，两者落在**同一次 unshare**（`-r -n -m`）上。实现要点：①只读设置需要一个**包装进程**（`exec_guard.py`）——不能在 `preexec_fn` 里做那一串 mount（多线程父进程的 fork 副本里做内存分配有死锁风险，本项目已因此把 rlimits 保持最小集合），也不能让 unshare 自己做；包装进程做完设置后 **exec 掉自己（PID 不变）**，否则内存探针会读到包装器自己、峰值内存小到离谱且无任何报错。②**`mount -o remount,ro,bind /` 只作用于根那一个挂载** —— 本机 `/vol1`/`/vol2` 是独立 btrfs、`/boot/efi` 是 vfat，只重挂 `/` 之后往 `/vol1/...` 写文件**照样成功**；我第一版就这么写，测试当场把 `blocked.txt` 写进了宿主机的项目目录。现遍历 `/proc/self/mountinfo` 重挂**每一个**真实文件系统（按挂载类型跳过伪文件系统），再单独把工作目录绑定回可写。③只读根会让 `/tmp` 不可写（ffmpeg/Playwright 都要写临时目录，表现为"渲染莫名失败"），故挂**有界**私有 tmpfs（`size=256m`，不设上限的 tmpfs 能吃掉整机内存）；且检测到工作目录位于 `/tmp` 之下时**不**替换 `/tmp` 并如实记 gap —— 否则工作目录会被 tmpfs 遮蔽，产物"凭空消失"（本项目的 pytest tmp_path 正在 /tmp 下，这个坑是测试当场逼出来的）。④**如实上报**：`ExecResult.read_only_enforced`/`read_only_gaps` 来自子进程写回的逐次状态报告（网络隔离是"能力+配置"，只读是**逐次**才知道），读不到一律按未生效处理，且不生效时**打警告日志**（否则只体现在没人看的字段里）；`/healthz` 报 `sandbox:read_only=mountns`。**验证**：17 项测试（含反向对照：关闭只读时写父目录必须**成功**，否则"被挡住"在无写权限的机器上会永远为真 —— 本次运行该对照实际走到；含"只读下 ffmpeg 真实出片"这条"不把功能弄坏"的证据）；**变异验证**：把重挂改成"只计数不真挂"（即谎报已加固）后用例立刻变红。真实任务端到端（`job-71adc30456569c66`、`job-75cc560e4760950e`）在只读开启下照常渲染出片（158KB/793KB 真实文件），且日志里「沙盒只读未生效」出现 **0 次**（即每次执行都真的保护上了）。另删掉被 `isolation.Isolator` 取代的死代码 `NetworkIsolator` 并迁移其用例 —— 两个都叫"隔离器"的类迟早会被改歪一个。**仍未做**：seccomp 白名单（已确认本机有 `libseccomp.so.2`，可经 ctypes 使用，下一轮做）；HTML 引擎的 Chromium 仍不经 runner（未被隔离）——且只读与 `$HOME` 缓存冲突，开启前需逐个引擎验证，这也是默认 off 的原因。Python **450 passed / 4 skipped**；Go 238 PASS / 0 FAIL / 1 SKIP（本轮未动 Go）。踩坑记录见 §9 |
| v0.5.9 | 阶段五（沙盒加固·seccomp） | **补上第三块：seccomp 系统调用过滤**（`SCID_SANDBOX_SECCOMP=off\|deny\|require`，缺省 off）。**刻意实现为拒绝名单而不是需求里写的"白名单"**，理由必须写明：允许名单要求把目标程序用到的每个系统调用都列全，而本机只装得起 ffmpeg（manim/LaTeX/Chromium 全部不可用），那份名单**无法被验证** —— 没验证过的允许名单比不加更危险（漏一个系统调用就让渲染静默失败，且"缺了哪一个"极难反推）。拒绝名单拦的是沙盒里没有正当用途的那批：进程注入（ptrace/process_vm_*/kcmp/perf_event_open/userfaultfd/pidfd_getfd）、内核模块与内核操作（init_module/kexec_load/reboot/swapon/adjtimex 等）、**挂载与命名空间**（mount/umount2/pivot_root/chroot/setns/unshare —— 注意在用户命名空间里我们**就是 root**，这些并非天然不可达，exec_guard 自己就用 mount 做只读设置）、密钥环、bpf、io_uring、open_by_handle_at，共 39 条。默认动作 ALLOW、被拦返回 **EPERM 而非杀进程**（意外的调用得到普通错误，程序往往还能降级继续）。实现要点：①seccomp **必须最后装**（不可逆，而只读设置要用 mount，而 mount 在名单里）；②它只需要一个包装进程、**不需要命名空间**，因此"只开 seccomp"时不经过 unshare（少一层就少一处会坏的地方），`Isolator.wrap` 按需组合；③两个加固手段必须能**独立启用**——我第一版让 guard 用"有没有传 --workdir"推断是否做只读，于是"只想要 seccomp"的人被迫接受只读（而只读会破坏 $HOME 缓存），现改为显式 `--readonly`/`--seccomp`；④SIGSYS 单独归成 `killed_reason="seccomp"`（`returncode=-31` 没人认得出，现象会变成"渲染莫名失败"）；⑤只报"拦了 N 条"不够，必须同时报**未解析**条目——名单里的系统调用在当前内核不存在时那条规则根本没装上，用例直接断言"装上+未解析=名单总数"。**验证**：23 项隔离用例（含反向对照：不启用时 ptrace 必须成功，否则在默认禁止 ptrace 的容器里该断言永远为真；含「seccomp 下 ffmpeg 真实出片」；含"三层同时开启都要如实上报且命令照常跑完"）；**变异验证**：把 seccomp_load 去掉只置标志位（谎报已加固）后用例立刻变红。真实任务 `job-64ed5499d525e2cf` 在**三层全开**下照常渲染出片（视频 598KB/950KB、配音 22.6KB/26KB 均为真实文件），日志里「未生效」出现 **0 次**。Python **457 passed / 3 skipped**；Go 238 PASS / 0 FAIL / 1 SKIP（本轮未动 Go）。**仍未做**：真正的容器/cgroup 层；HTML 引擎的 Chromium 不经 runner（未被隔离）；seccomp 只在 stock/ffmpeg 上验证过（manim/LaTeX/Chromium 本机装不起来），这也是默认 off 的原因。踩坑记录见 §9 |
| v0.6.0 | 阶段五（沙盒加固·补上 HTML 引擎） | **堵掉沙盒覆盖面上最大的一个洞**：三个 HTML 引擎（d3/echarts/code_anim）此前是在 **AI 服务进程内**直接 `sync_playwright()` 起 Chromium 的 —— 完全不经过 `SandboxRunner`，因此网络隔离/只读/seccomp 对它一律无效，**LLM 生成的 JS 可以在有网络的浏览器里跑**。现把逐帧截图搬进**沙盒子进程**（新模块 `html_capture.py`，经 runner 执行），三块加固自动生效；`HtmlRenderer._capture` 只负责拼参数与把退出码映射成渲染失败，边界更窄。**两个前置修复**：①`RLIMIT_AS` 兜底下限从 2GB 提到 **128GB** —— 逐项二分实测 Chromium **32GB 崩、64GB 正常**，原值让任何经过 runner 的浏览器启动即 `SIGTRAP`（报错里没有任何可读信息，看起来像"浏览器坏了"）；②`browser_ready()` 现在认 `SCID_CHROME` 覆盖（仍**验证文件存在**，不是无条件放行），理由是 Playwright 按 build 号装浏览器，包升级后期望的号会变、默认路径报 `Executable doesn't exist` —— 本机就是这样，因此不认覆盖就**无法验证**这条链路。**验证**：新增 `tests/test_html_sandbox.py` 共 8 项 —— ①**核心安全断言**：沙盒里浏览器内的 JS 对公网 `fetch` 必须失败；②**确定性反向对照**：本机回环 HTTP 服务在**不隔离**时连得上（用公网做对照会时而跳过，而"时跳过的对照"等于没有对照）；③隔离下**连回环也不通**（证明命名空间真的建起来了）；④整条 HTML 路径在沙盒下出片（截图→编码→probe，校验时长/分辨率）；⑤缺 `window.__seek` 以**专用退出码 3** 报告（内容问题 vs 环境问题必须能分开）；⑥runner 真的接上了；⑦**渲染器确实走 runner**（变异验证：把它改回进程内直接调用，用例立刻变红）；⑧覆盖策略的独立性用例。真实环境复验：`SCID_CHROME` 生效后 `/healthz` 的 `d3/echarts/code_anim` 从 missing 变为 **ok**（之前因为 build 号不匹配一直报 missing），任务 `job-e2d2cacf057b04c1` 里 d3 镜头仍在 `code` 节点因 mock 生成的 HTML 缺 `window.__seek` 而转人工（**渲染之前**就熔断，与本次改动无关，日志里没有任何截图/浏览器失败）。**踩坑**：①按脚本路径执行的模块会把自身目录塞进 `sys.path[0]`，而 `scidirector_ai/logging.py` **遮蔽了标准库 logging**，playwright 报 `partially initialized module 'logging' has no attribute 'Formatter'`，错误信息完全指不到真因；②把环境覆盖判断写进可注入的 `_probe_browser_with()`，真实环境变量盖掉了注入的假对象，三条既有用例当场变红 —— 策略与机制要分层；③缓存用例没清 `SCID_CHROME`，变成"只在带该变量的机器上红"（与上一轮 `SCID_ARCHIVE_BACKEND` 同类，第二次踩到）；④`page.evaluate` 的代码在浏览器里求值，Python 变量引用 → `ReferenceError`、`#` 注释 → `SyntaxError`；⑤`PLAYWRIGHT_BROWSERS_PATH` 这类**路径配置**容易被环境白名单顺手漏掉，漏了表现为"截图全失败"且看不出原因。Python **466 passed / 4 skipped**（含新增 8 项）；Go 238 PASS / 0 FAIL / 1 SKIP（本轮未动 Go）。**仍未做**：真正的容器/cgroup 层；manim/LaTeX 本机不可用因此 manim 路径未验证；seccomp 仍只在 stock/ffmpeg 与 HTML 路径上验证过。踩坑记录见 §9 |
| v0.6.1 | 阶段五（配置透传修复） | **修掉一处「照文档配了却不生效」的静默失效**：阶段五的九个运行时开关（`SCID_TENANT_MODE` / `SCID_TENANT_HEADER` / `SCID_TENANT_MAX_ACTIVE_JOBS` / `SCID_RECONCILE_INTERVAL` / `SCID_SANDBOX_NETWORK_ISOLATION` / `SCID_SANDBOX_READ_ONLY` / `SCID_SANDBOX_SECCOMP` / `SCID_SANDBOX_PYTHON_BIN` / `SCID_CHROME`）在代码与 `.env.example` 里都齐全，却**没有一个**出现在 `docker-compose.yml` 的服务 env 块里 —— 而 Compose 只把显式列出的变量注入容器，`.env` 的值仅参与 `${...}` 插值。也就是说：**容器部署时这九个开关全部无效，且没有任何报错**。这与 `SCID_S3_*` / `SCID_MINIO_*` 那次是同一类问题（本项目第二次踩到，第一次已写进 §9）。修法：在三个应用服务共用的 `x-common-env` 锚点里统一透传（一次配置、处处生效），并补一条**可执行的检查**（`backend/internal/config/compose_env_test.go`，3 项）：①清单里每个开关必须**同时**出现 `NAME:` 与 `${NAME}`（前者保证被注入、后者保证 `.env` 的值读得到）；②每个开关都要在 `.env.example` 里有据可查（只在代码里悄悄加开关 = 该开关只存在于读过源码的人脑子里）；③Go 侧读到的环境变量必须都是 `SCID_` 前缀（前缀统一才让「搜 SCID_ 就能看全可配项」成立）。**变异验证**：从 compose 删掉 `SCID_SANDBOX_SECCOMP` 一行，用例立刻报「容器拿不到它，在 .env 里配置将静默失效」。另在 compose 注释里写明一个极易误判的点：**容器内**启用沙盒的网络/只读隔离需要先放宽容器自身的 seccomp（`unshare` 被 Docker 默认策略拦截，容器内调用得到 `Operation not permitted`，看起来像代码坏了）——而那等于用容器层的安全性换进程层的隔离；compose **刻意不默认放宽**，只写明取舍与验证命令 `docker compose exec ai unshare -rn true`。验证：`docker compose config` 实测九个变量均到达 ai/api/worker 三个服务；Go **241 PASS / 0 FAIL / 1 SKIP**；Python **467 passed / 3 skipped**。另：本轮恢复环境时发现基础设施容器已停（会话跨了两天），`redis`/`postgres`/`rustfs` 未运行时 Go 套件里依赖它们的用例会**按设计跳过**（`241 → 182 PASS` 且 S3 失败），已重新拉起并复跑到 241 |
| v0.6.2 | 阶段五（多服务商支持） | **LLM 层支持多服务商，并接入 DeepSeek 与阿里云百炼**。新增 `providers.py`：一张服务商注册表（base_url / 密钥变量 / 默认文本模型 / 默认视觉模型），三家全走 **OpenAI 兼容协议**，因此只需一份客户端实现，差别只在 base_url/model/key —— 自建一套 SDK 抽象只会增加要维护的面。**文本与视觉可分别选服务商**（`SCID_LLM_PROVIDER` + `SCID_VLM_PROVIDER`，后者留空则跟随），因为 DeepSeek **没有视觉模型**：于是"DeepSeek 写代码（便宜）+ 百炼 qwen-vl-max 审画面"成为一个正常且推荐的组合。`llm_model`/`vlm_model` 默认改为**留空 = 用服务商出厂默认**（gpt-4o / deepseek-chat / qwen-plus / qwen-vl-max），并保留 `SCID_LLM_BASE_URL`/`SCID_LLM_API_KEY` 作为通用覆盖（自建网关）。**一条关键安全规则**：真文本 + 未配视觉时**绝不**退回 mock 审查 —— mock 审查会返回伪造的"审查通过"，让未审查的画面进成片且无任何报错；正确行为是抛错并由 Critic 降级转人工（变异验证：改回退回 mock 后用例立刻变红）。另把 401/403/404/400 归为**不可重试**的配置类错误（重试只会晚 7 秒失败并刷满日志），并按实际尝试次数如实措辞（原先写死"已重试 3 次"，在不重试时就是谎报）。**验证**：新增 `tests/test_providers.py` 18 项 —— 解析（各家默认/base_url/密钥不串、显式模型覆盖、通用覆盖优先、非法 provider 启动即失败）；**本地 OpenAI 兼容假服务端**接住请求，断言 Authorization、model、以及视觉请求里的 image_url 分片（"配置对象对"证明不了字段真的上了线）；**真实端点**：本机实测 `api.deepseek.com` 与 `dashscope.aliyuncs.com` 可达（用无效密钥得到 401 且只发 1 次请求），而 `api.openai.com` 不通因此显式记下"验不了"。健康检查把文本与视觉**分开报**（`llm:text=deepseek/deepseek-chat`、`llm:vision=bailian/qwen-vl-max` 或 `llm:vision=none`），mock 时报 `llm:text=mock(配置为 openai)` 而不是冒充实服务商。顺带修掉两处部署侧静默失效：①compose 里 `SCID_LLM_MODEL` 写死 `gpt-4o`（选 deepseek 时会把它发过去，报"模型不存在"）；②compose 只设了**不带前缀**的 `OPENAI_API_KEY`，而 Python 读 `SCID_OPENAI_API_KEY` ⇒ 容器部署**永远进 mock 模式**且无报错（这类问题第三次出现，已并入 compose 透传守卫用例的清单）。Python **485 passed / 3 skipped**；Go **241 PASS / 0 FAIL / 1 SKIP**。**未验证**：真实补全需要有效密钥（本机无），因此"模型能否正确产出 JSON"未实测；OpenAI 端点在当前网络下无法验证。踩坑记录见 §9 |
| v0.6.3 | 部署（镜像构建修复） | **修掉"api/worker 镜像根本构建不出来"**：用户执行 `docker compose up --build` 失败，追下去是两件独立的事。①**我的回归**：v0.5.5 引入 OpenTelemetry 时 `go.mod` 的 go 指令升到 1.24（otel v1.34 要求），而 `backend/Dockerfile` 仍钉着 `golang:1.23-alpine` ⇒ `go mod download` 报 `go.mod requires go >= 1.24.0 (running go 1.23.12; GOTOOLCHAIN=local)`。**本地完全看不出来**（宿主 Go 是 1.26，`go build`/`go test` 全绿），只有真正构建镜像才暴露 —— 根因是我此前所有验证都是"宿主跑进程 + 容器只跑中间件"，**从未构建过应用镜像**。已把镜像升到 `golang:1.24-alpine`，并加用例 `internal/config/dockerfile_go_test.go` 钉住"镜像里的 Go 版本必须满足 go.mod"（含变异验证：改回 1.23 立刻红）。②**Docker Hub 不可达导致的 load metadata 失败**：`failed to resolve source metadata ... 401 Unauthorized`（本机加速器 `docker.fnnas.com` 对 Docker Hub 也不通）。修法是用可用的镜像站显式拉取再打本地 tag——本轮拉了五个基础镜像（python:3.11-slim / nginx:1.27-alpine / node:22-alpine / alpine:3.20 / golang:1.24-alpine），此后 `docker compose build` 命中本地镜像。compose 头部把这条命令补全成可直接复制的循环。**验证**：`docker compose build web` 与 `build api` 均成功（api 在修掉 Go 版本后从 `go mod download` 失败转为 Built），`ai` 镜像（含 TeXLive，体量大）在后台构建。另补 README 依赖表（Go ≥ 1.24）与"没有 make 时"的等价命令表。**仍未做**：`ai` 镜像构建完成前不声称它可构建；容器内启用沙盒命名空间隔离需放宽容器 seccomp（未改默认） |
