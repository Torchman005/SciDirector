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
- [ ] **阶段四 · 反馈闭环与前端**
  - [x] WebSocket 服务端（快照重放 + 扇出 + 背压 + 多实例 Pub/Sub）
  - [ ] React 分镜审核台
  - [x] 「打回重做」的服务端链路（REST + WS 上行 → 单镜头任务入队）
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
| 模型调用的意图必须**显式传参**（`llm.Task.*`），禁止从提示词文本嗅探关键词 | mock 客户端曾因导演提示词含「审查」二字而返回错误结构，被静默解析成「空分镜表」——这类「看起来成功」的失败形态比抛异常危险得多 |
| 派生的模拟数据必须**结构上等价**于真实产出 | 否则 mock 模式会掩盖真实的解析/校验问题，联调通过但上线即挂 |
| 仓库内自带的依赖目录（`.pylibs`）必须置于 `PYTHONPATH` **末尾** | 它可能包含被深度依赖的包（如 `typing_extensions`），置于前面会遮蔽版本更完整的同名包 |
| 图节点只**声明**下一跳（`route_hint`），条件边只做读取与校验 | 把判断散在边函数里会让「带反馈的循环」难以推理；节点负责决策，边负责路由 |
| 「引擎工具链缺失」必须映射为**不可重试**失败并熔断转人工 | 重试一个本机根本不存在的引擎只会烧满 attempt 计数。真实跑测已验证：缺 manim/d3 的两个镜头熔断为 `AWAITING_HUMAN`，其余镜头照常完成，任务终态 `PARTIAL` |
| 跨平台机制差异（POSIX `RLIMIT_AS` vs Windows Job Object）必须**归一化**成同一个可观测结果 | 上层只认 `killed_reason == "memory"`，否则熔断逻辑要在两套平台上各写一遍 |
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

---

## 10. 迭代日志

| 版本 | 阶段 | 变更 |
| --- | --- | --- |
| v0.1.0 | 阶段一 | 建立目录骨架、跨语言 proto 契约与生成流水线、docker-compose 全栈、Go 骨架（配置/日志/状态机/仓储/队列/gRPC 客户端/WS Hub/编排处理器/ffmpeg 封装）、Python 骨架（配置/日志/Schema/LLM 客户端/LangGraph 状态/导演智能体/沙盒策略/gRPC+FastAPI 双栈）、四份文档与构建脚本；Go 与 Python 测试全部通过 |
| v0.1.1 | 阶段一（修订） | **回退阶段二的提前实现**，把仓库收敛到经过验证的阶段一状态：移除编码/审查智能体、沙盒运行器、渲染与媒体工具、RAG、图拓扑与 checkpointer；`RunPipeline` / `GenerateShot` / `CritiqueShot` / `ReviseShot` 恢复为返回 `UNIMPLEMENTED`。保留阶段一两处前置能力（导演智能体、沙盒静态安全策略）。修复 `scripts/dev-env.ps1` 的 `PYTHONPATH` 顺序缺陷（`.pylibs` 必须置于**末尾**，否则会遮蔽版本更完整的同名包，表现为 pydantic 导入时莫名的 `cannot import name`） |
| v0.1.2 | 阶段一（修复） | 手工联调实测发现并修复两项语义缺陷：① `UNIMPLEMENTED` 不再被 Asynq 重试（新增哨兵 `ai.ErrNotImplemented`，worker 返回 `asynq.SkipRetry` 直接归档）；② `sandbox_ready` 改为「至少一个渲染引擎可用」并新增结构化 `engines` 字段，使实现与文档一致。新增 `scripts/smoke-grpc.py`、`scripts/smoke-ws.py` 两个可复用冒烟脚本与 `make smoke*` 目标；README 新增「手动测试」章节。Go 测试 +1 包，Python 测试 71 → 83 |
| v0.2.0 | 阶段二 | **Python 多智能体核心落地，Go 侧全链路打通**。沙盒执行器（超时 30s 强杀 + 内存上限，Windows Job Object / POSIX rlimit 双实现，留痕 `killed_reason`）→ Manim 沙盒（四层防御）→ 媒体层（抽帧含首末帧、lavfi 环境镜头、drawtext 三级降级）→ 渲染器抽象（Manim/Html/Ambient 确定性路由 + 就绪探测 + HTML 契约校验）→ 编码智能体（按标签选提示词、RAG 召回、策略校验后重写）→ 审查智能体（分维度加权 + 硬性下限 + 程序侧复核 + VLM 不可用降级转人工，中文强约束 JSON 提示词）→ RAG（可解释打分，中文走 CJK 二元组）→ LangGraph 图（带反馈循环、`route_hint` 路由、重试上限熔断转 `AWAITING_HUMAN`、Postgres checkpointer 显式降级）→ `pbconv` 双向转换 → 5 个 RPC 全部实现。拆分顺序按依赖自底向上，每层落地即测。Python 测试 83 → 368；`go vet` / `gofmt` / `go test ./internal/...` 全绿；`scripts/smoke-grpc.py` 25/25 通过；真实端到端任务 `job-799a0041b7ad3434` 产出 2 个可播放 MP4、2 个镜头按预期熔断。验收明细见 `docs/ROADMAP.md` |
| v0.3.0 | 阶段三 | **Go 编排与媒体处理**。①**FFmpeg 并发收敛**为两道防线：任务内 Worker Pool（`errgroup.SetLimit`，内存 O(limit) 而非 O(分镜数)）+ 进程级全局单例闸门（真正的 OOM 防线），并修掉「每次合成新建信号量导致全局上限随任务数成倍放大」与「`Probe` 完全绕过闸门」两个缺陷；进程生命周期加固（`WaitDelay` 兜住孙进程占用管道导致的 `Wait` 永久阻塞、自定义 `Cancel` 杀整棵进程树、单命令硬超时）。②**转场与统一规格**：`PlanTransitions` 纯函数（offset 必须基于累积游标）、音频同长 `acrossfade`、Normalize 显式转换并打标色彩范围（解决 Manim 与浏览器录制的质感割裂）。③**字幕**：窗口建立在转场后的时间轴、跨语言语音权重、最短显示时长、SRT 输出；软字幕 mov_text 封装。④**局部重渲染**：`PlanSplice` 纯函数 + 单次 filter_complex 拼接，按引擎能力如实降级（Manim 按动画序号渲染，无法按秒截取）。⑤**产物归档**：none/local/s3 三后端 + 保留策略（纯函数）。⑥**队列可观测**：`GET /api/v1/queue/stats`。测试：Go media 包 0 → 68 项、archive 包 27 项、queue 包 6 项；Python 368 → 377。**同时修复三处端到端才能发现的缺陷**：`MuxFinal` 的 `-map` 下标写死导致软字幕永远封不进去、`-shortest` 把末尾字幕压成零长、`filepath.ToSlash` 破坏 concat 引号转义；以及两处仓库级问题：`.gitignore` 未锚定的 `media/` 吞掉整个 Go 包、含中文的 `.ps1` 缺 UTF-8 BOM 导致脚本完全不可解析。**待办**：全片 TTS 配音、B4/B5 验收、s3 归档路径的端到端验证 |
