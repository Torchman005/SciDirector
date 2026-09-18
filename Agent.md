# Agent.md — SciDirector 智能体协作与工程规约

> 本文件是本仓库的**唯一权威协作契约**。任何人类开发者或 AI 编码智能体在修改本仓库前，
> 必须先读本文件；修改架构、契约、状态机、目录职责后，**必须在同一次提交中更新本文件**。
>
> 版本：v0.4.8 · 阶段：阶段五（生产加固）· 最后更新：见文末「迭代日志」

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
  - [ ] OpenTelemetry 链路追踪、Prometheus 指标、Grafana 看板
  - [ ] 多租户与配额、成本核算（token/渲染时长）

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
| `context.set_offline(True)` **不会**断开已经建立的 WebSocket | 做「断网演练」时实测断网期间连接指示器仍是「实时」，演练等于没做。要真正切断实时通道，用「停掉 api 进程 N 秒再拉起」更可靠，而且顺带覆盖了「服务端重启」这个代码里明确设计过的场景。**脚本必须如实报告「本次演练其实没断线」**，而不是假装通过 |
| 就绪探测必须检查**二进制/可执行文件**，不能只看包能否 import | `playwright` 是两步安装：`pip install playwright` 只装 Python 包，浏览器要另跑 `playwright install chromium`。只做第一步时「导入探测」会报可用，健康检查于是宣称 d3/echarts/code_anim 都就绪 —— 一个会说谎的就绪探测比没有探测更糟，它把「环境没准备好」伪装成「内容不达标」，而两者该做的处置完全不同 |
| 同一件事的判定**只能有一份实现** | 「这个引擎能不能渲染」原先在 `config.toolchain_report()`（健康检查读它）与 `renderer.HtmlRenderer.available()`（渲染前自检）各写了一套，于是必然各说各话。现在共用 `config.browser_ready()` |
| 画面时长 ≠ 旁白时长，字幕不能按画面窗口铺满 | 一个 8 秒镜头可能只有 5 秒旁白，剩下 3 秒是留白。按窗口铺满会把最后一句字幕**拉伸着挂满那 3 秒** —— 声音早停了、观众早读完了，字幕还在，这是音画不同步最典型的形态。有配音时字幕应当只覆盖配音那一段（`PlanCuesWithNarration`）；而**没有配音的镜头必须回退成铺满窗口**，否则新逻辑会误伤原本正确的路径 |
| 同一个探测函数未必适用于所有输入：`Probe` 是**产物校验器**，不是通用探测器 | 它要求存在视频流（这是对的，空产物/纯音频产物都该被判无效），因此对纯音频文件必然失败。配音时长要另走一条只读 `format.duration` 的路径。第一版用例就是因为复用了 `Probe` 而失败 |
| 拼接音频时，没有配音的段要补**等长静音**，不能跳过 | 跳过会让整轨比画面短，其后每个镜头的声音整体前移 —— 「某段没声音」只是小瑕疵，「全片音画错位」是废片 |
| 自动化的「检查」必须带**负向对照**，否则它可能什么都没检查 | 断言「没有布局问题」的检查器一旦选择器写错，就会恒返回「通过」—— 比没有检查更糟，因为它让人以为已经验证过了。做法：故意注入一个坏元素、确认检查器会报、再移除并确认恢复干净。这与「只断言不超过会漏掉根本没并发」是同一条纪律 |

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
