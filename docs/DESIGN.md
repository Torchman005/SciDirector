# SciDirector 设计思路文档

> 本文回答三个问题：**为什么这么做**、**技术上如何做到**、**代价与取舍是什么**。
> 它随项目迭代持续更新；任何架构级改动都必须同步修订本文（见 `Agent.md` §9）。

---

## 一、问题的本质：为什么 Sora/Runway 讲不好科学

把"生成一段 60 秒相对论科普视频"直接交给文生视频模型，失败是结构性的，不是"模型还不够强"：

| 失败模式 | 根因 | SciDirector 的应对 |
| --- | --- | --- |
| 公式写错、符号乱码 | 扩散模型在**像素空间**逼近，没有符号系统的约束 | 数学镜头不用文生视频，用 **Manim 程序化渲染**（LaTeX 排版天然正确） |
| 数据图表张冠李戴 | 同上，模型不理解数值与坐标轴语义 | 数据镜头用 **D3/ECharts**，数值来自脚本，坐标轴由代码决定 |
| 画面之间没有逻辑衔接 | 每段独立生成，缺少全局规划 | **Director Agent 先出分镜表**，全局时长/节奏/标签统一分配 |
| 一遍成型，错了无法定位 | 缺少可验证的中间表示 | 中间表示是**代码 + 抽帧**，可 diff、可审查、可局部重做 |
| 幻觉与不严谨无人发现 | 没有审稿环节 | **Critic Agent(VLM) 抽帧审查**，不合格自动打回，闭环重做 |

**核心洞察**：知识科普视频的难点不在"画得像"，而在"讲得对、讲得顺"。
因此正确架构不是"更好的文生视频"，而是**把自然语言意图编译成确定性图形程序的编译器**，
并在编译管线末端加一个**基于视觉反馈的验证器**。

一句话：**SciDirector 是一个带视觉回归测试的"脚本 → 分镜程序"编译器。**

---

## 二、总体设计原则

1. **确定性优先（Determinism first）**
   凡是能用程序精确表达的（公式、图表、代码），绝不交给扩散模型。
   模型只负责"理解与规划"（Director）与"生成代码"（Coder），像素由渲染器产生。
   → 收益：可复现、可局部重做、成本低（一次渲染 vs 一次视频生成）。

2. **契约先行（Contract first）**
   Go 与 Python 之间的所有交互由 `proto/scidirector/v1` 定义，先改契约再改实现。
   → 收益：跨语言重构安全；未来可替换任一侧语言而不动另一侧。

3. **可验证的中间表示（Verifiable IR）**
   分镜的中间产物不是"一段视频"，而是 `{代码, 渲染产物路径, 抽帧, 审查意见, 尝试次数}`。
   → 收益：任何失败都能定位到"哪一步、哪一版、为什么"。

4. **人机协同而非全自动（Human-in-the-loop by default）**
   自动化有上限（重试 3 次）。超过上限不是继续烧钱，而是**交给人类**（`AWAITING_HUMAN`）。
   → 收益：成本可控，审核员始终在环。

5. **失败是常态，重试是一等公民（Retry as a feature）**
   队列层（Asynq）负责任务级重试；图编排层（LangGraph）负责语义级重试（带反馈的重新生成）。
   两层重试语义不同，不能混为一谈。

---

## 三、为什么是 Go + Python 混合

单一语言都做不好这件事：

| 维度 | Go 的优势 | Python 的优势 |
| --- | --- | --- |
| 并发与长连接 | Goroutine + 原生 WebSocket，万级连接成本低 | asyncio 生态相对弱 |
| 进程与媒体编排 | `os/exec` + `errgroup` 并发 ffmpeg，进程管理稳 | 子进程管理易泄漏 |
| 长链路任务可靠性 | Asynq（基于 Redis）成熟的重试/超时/死信机制 | Celery 较重且状态调试困难 |
| AI 生态 | 几乎没有 | LangGraph / LangChain / VLM SDK 全在这里 |

**结论**：Go 做"骨架与肌肉"（编排、状态、媒体、并发、对外接口），Python 做"大脑"（语义、生成、审查）。
分界线是 **`proto` 契约 + gRPC**。

**代价**：需要维护两套构建、两套依赖、跨语言调试成本。
**缓解**：统一 `Makefile` 入口、统一 `docker-compose`、统一结构化日志（同一 `trace_id`）、契约只有一个真源。

---

## 四、跨语言契约设计（gRPC）

### 4.1 为什么用 gRPC 而不是 HTTP/JSON
- 需要**服务端流式**（`RunPipeline` 持续推送 导演→编码→渲染→审查 的实时事件），
  HTTP/JSON 需要 SSE 或轮询，且弱类型。
- proto 是**强类型契约**，一次定义两侧生成，字段演进有兼容规则。
- 未来 Python 侧渲染 worker 可以横向扩容成独立服务，gRPC 天然支持负载均衡。

### 4.2 接口切分：粗粒度流水线 + 细粒度单镜操作

```proto
service AiDirectorService {
  rpc Health(HealthRequest) returns (HealthResponse);

  // —— 粗粒度：交给 LangGraph 跑完整条图，Go 只消费事件流 ——
  rpc RunPipeline(RunPipelineRequest) returns (stream PipelineEvent);

  // —— 细粒度：HITL 单点重做，Go 可以只重跑某一步 ——
  rpc PlanScript(PlanScriptRequest)       returns (PlanScriptResponse);     // 仅导演
  rpc GenerateShot(GenerateShotRequest)   returns (GenerateShotResponse);   // 仅编码+渲染
  rpc CritiqueShot(CritiqueShotRequest)   returns (CritiqueShotResponse);   // 仅审查
  rpc ReviseShot(ReviseShotRequest)       returns (ReviseShotResponse);     // 人工意见回灌
}
```

**关键决策：谁持有状态？**
- LangGraph 用 Postgres checkpointer 持有**图内部状态**（用于中断续跑、断点恢复）。
- Go 用 Redis 持有**对外的任务/分镜状态**（WS 推送、UI 展示、幂等去重）。
- 二者通过 `job_id / shot_id` 对齐，事件流是单向同步源（Python → Go）。
- 为什么不把状态全放 Python？因为 WS 推送、并发 ffmpeg、任务重试都在 Go，
  状态若在 Python，Go 每次都要回查，链路变长且 Python 成为单点。
- 为什么不把状态全放 Go？因为 LangGraph 的中断/恢复需要自己的 checkpoint 语义。

> **取舍**：状态"双写"是有意的。以 Go 的 Redis 状态为**对外真源**，
> Python 的 checkpoint 为**内部恢复真源**；两者最终一致，靠事件流驱动。

### 4.3 大文件不过 gRPC
proto 中只传 `video_path / frame_samples[] / duration / width / height`。
视频二进制走共享卷（本地/dev）或 MinIO（生产），避免 gRPC 消息膨胀与内存放大。

---

## 五、Python 侧：LangGraph 多智能体编排

### 5.1 为什么是 LangGraph
- 本流程天然是**带环的有向图**：`critic → coder` 的回边是核心特征，不是异常路径。
- 需要**状态持久化**：Manim 渲染可能几分钟，进程重启要能续跑。
- 需要**条件路由 + 重试上限 + 中断转人工**，LangGraph 原语（conditional edges / checkpointer / interrupt）直接对应。
- 对比：如果只用 LangChain Agent，环与状态要靠手写 while 循环，可观测性和恢复能力都差。

### 5.2 图结构

```
            ┌─────────────┐
   START ──▶│  plan       │  Director Agent：脚本 → 分镜表
            └──────┬──────┘
                   ▼
            ┌─────────────┐
            │  code       │  Coder Agent：按 tag 路由生成源码
            └──────┬──────┘
                   ▼
            ┌─────────────┐
            │  render     │  Sandbox：执行 Manim/D3 → MP4 + 抽帧
            └──────┬──────┘
                   ▼
            ┌─────────────┐
            │  critique   │  Critic Agent(VLM)：审查抽帧
            └──────┬──────┘
                   │
        passed? ───┼───────────────────────────────┐
          no │                                     │ yes
             ▼                                     ▼
      ┌─────────────┐                      ┌─────────────┐
      │  revise     │ 注入 feedback；      │  advance    │ 下一镜头 / 结束
      │ (attempt+1) │ attempt 达上限则转人工└──────┬──────┘
      └──────┬──────┘                             │
             │  attempt < MAX                      │ 还有镜头
             └──────────▶ code                    ▼
                                              (下一镜头 → code)
```

### 5.3 状态定义（`graph/state.py`）

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `job_id` | str | 任务标识，与 Go 对齐 |
| `raw_script` | str | 原始脚本 |
| `style_guide` | dict | 风格约束（配色、字体、语速） |
| `shots` | list[ShotSpec] | 分镜表 |
| `cursor` | int | 当前处理到第几个镜头 |
| `current_code` | str | 当前镜头生成的源码 |
| `artifacts` | dict[shot_id, Artifact] | 渲染产物（含抽帧路径） |
| `feedback` | dict[shot_id, CriticFeedback] | 审查意见 |
| `attempt` | int | 当前镜头已尝试次数（**熔断依据**） |
| `human_feedback` | dict[shot_id, str] | 人类打回意见（HITL 注入点） |
| `events` | list[PipelineEvent] | 待推送事件缓冲 |
| `errors` | list[str] | 非致命错误，供观测 |

> **状态设计要点**：`attempt` 是**每镜头独立**的计数器，进入下一镜头时归零。
> 这是防止"一个坏镜头把整个任务拖死"的关键。

### 5.4 沙盒安全（`sandbox/`）
渲染任意 LLM 生成的代码是**最大的风险面**。分层防御：

1. **静态白名单**：AST 扫描，仅允许 `manim / numpy / math / json` 等白名单模块；
   禁止 `subprocess / socket / requests / os.system / open(写模式)`。
2. **进程隔离**：独立子进程 + `resource` 限制（CPU 时间、RSS 上限 `SCID_SANDBOX_MAX_MEMORY_MB`）。
3. **超时熔断**：`SCID_SANDBOX_TIMEOUT_SEC` 到点 SIGKILL，判定该次渲染失败（计入 attempt）。
4. **容器加固**（生产）：`--network=none --read-only`，只挂载一个临时输出目录。
5. **输出校验**：产物必须存在、非空、可被 ffprobe 解析，否则视为渲染失败。

> 明确认知：沙盒是**纵深防御**，不是绝对安全边界。生产环境必须跑在无网络容器内。

### 5.5 Critic（VLM）审查设计
- **抽帧策略**：按镜头时长等间隔取 3~6 帧 + 首尾帧（首尾帧能捕捉"入场/收尾"问题）。
- **提示词结构**：`[角色] + [镜头意图] + [画外音] + [图] + [评分维度 rubric] + [输出 JSON Schema]`。
- **rubric 维度**（可解释、可打分）：
  `逻辑一致性 / 文字可读性(字号、对比度、是否截断) / 信息密度 / 节奏 / 美观度`。
- **输出强约束 JSON**，解析失败则重试一次，再失败降级为"人工复核"而非直接判失败。
- **可执行性约束**：suggestion 必须能被转换为代码级修改（见 `Agent.md` §5.3）。

### 5.6 RAG（Few-shot 检索）
- 语料：精选的 Manim/D3 优秀科普片段（代码 + 效果描述 + 标签）。
- 用途：Coder Agent 生成前召回 2~3 条同标签范例注入上下文。
- **意义**：把"模型自由发挥"变成"模仿经过验证的范式"，显著提升一次通过率、降低成本。
- 存储起步用轻量方案（本地向量索引 / pgvector），避免过早引入重型向量库。

---

## 六、Go 侧：编排、状态与媒体

### 6.1 任务模型
```
Job (一次生成请求)
 └── Shot[] (N 个分镜)
      └── Attempt[] (该镜头的每次尝试，含 feedback)
```
- **Job 级任务**：`task:generate_job`，负责跑通全流程。
- **Shot 级任务**：`task:render_shot`，供 HITL 单点重做（只重做被打回的那一镜，不重跑全片）。
- 这是"成本控制"的关键：人类打回一个镜头，不应触发全片重渲染。

### 6.2 为什么用 Asynq
- 基于 Redis，无额外中间件；支持**重试 + 指数退避 + 超时 + 唯一性 + 死信 + 优先级**。
- 相比自研 goroutine 池：任务可持久化、进程崩溃后不丢、可观测（asynqmon）。
- 相比 Celery：Go 原生、类型安全、部署简单。
- **两层重试的边界**：Asynq 重试 = **基础设施故障**（网络、进程崩溃、Redis 抖动）；
  LangGraph 重试 = **内容不合格**（画面错、字太小）。二者不互相掩盖。

### 6.3 ffmpeg 并发合成
- 每个镜头独立渲染出 MP4；合成阶段用 `errgroup` 控制并发上限 `SCID_FFMPEG_MAX_PARALLEL`。
- 流程：`分镜视频 → concat → 混入 TTS 音轨 → 烧入/挂载字幕 → 加转场 → 输出成片`。
- 关键工程点：
  1. 每个 ffmpeg 进程必须绑定 `context`，取消时 `Process.Kill()`，避免僵尸进程。
  2. 统一 `-r 30 -pix_fmt yuv420p`，避免不同引擎产出的片段无法 concat。
  3. concat 前用 `ffprobe` 校验每个片段的**分辨率/帧率/编码一致性**，不一致先归一化。
  4. 中间产物全部落 `SCID_MEDIA_WORK_DIR`，任务结束按策略清理。

### 6.4 状态机与幂等
- 所有状态迁移经 `domain.Transition(from, to)` 校验，非法迁移报错并记录（早暴露 bug）。
- Redis 中用 `SETNX` 做「同一 shot 同一 attempt 只处理一次」的幂等锁，防止队列重复投递导致重复渲染。
- 事件写入 Redis List（`job:{id}:events`），WS 断线重连后可**从 last_event_id 重放**。

---

## 七、人类反馈闭环（HITL）

### 7.1 时序

```
审核员                 web                api(Go)              worker(Go)           ai(Python)
  │  打开任务    ────▶  │                    │                     │                    │
  │                      │  WS /ws/jobs/:id ─▶│                     │                    │
  │                      │◀── 事件流（含历史重放）─────────────────────┤                    │
  │  看到镜头3不满意       │                    │                     │                    │
  │  填写"坐标轴标签重叠" ▶│  POST .../shots/3/reject ─▶│                     │                    │
  │                      │                    │ 入队 task:render_shot │                    │
  │                      │                    │────────────────────▶│                    │
  │                      │                    │                     │ gRPC ReviseShot ──▶│
  │                      │                    │                     │   (human_feedback)  │
  │                      │◀── RETRYING→…→APPROVED 事件流 ────────────┤◀── 事件流 ──────────┤
```

### 7.2 关键设计点
- **重放能力**：WS 不只是推送，还负责"加入即重放历史"，否则刷新页面就丢失上下文。
- **意见结构化**：人工意见与 VLM 意见走**同一个 `CriticFeedback` 结构**，
  Coder 侧无需区分来源（`source=human|vlm` 字段仅用于展示与统计）。
- **不阻塞**：人工审核不影响其他镜头推进（per-shot 状态机）。
- **可追溯**：每次 Attempt 都记录触发者（自动重试 / 人工打回）。

---

## 八、可观测性与成本

| 维度 | 方案 |
| --- | --- |
| 日志 | Go `slog` + Python `structlog`，统一 JSON 字段：`trace_id / job_id / shot_id / attempt / node` |
| 指标 | 一次通过率（first-pass rate）、平均尝试次数、单镜头渲染耗时、token 成本、VLM 调用次数 |
| 追踪 | OpenTelemetry：Go span ↔ gRPC ↔ Python span 串联（阶段五） |
| 成本控制 | 单镜头 attempt 上限；渲染前静态校验（省一次渲染）；VLM 只在渲染成功后调用 |

> **一次通过率**是这套系统最重要的北极星指标：它同时反映提示词质量、RAG 质量、路由正确性。

---

## 九、关键取舍速查表

| 决策 | 选择 | 放弃 | 理由 |
| --- | --- | --- | --- |
| 数学/数据镜头 | 程序化渲染（Manim/D3） | 文生视频 | 正确性 > 观感 |
| 状态存储 | Redis（对外）+ Postgres（图内） | 单一真源 | 各取所长，事件流对齐 |
| 通信 | gRPC（含流式） | REST/JSON | 强类型 + 流式 + 未来扩容 |
| 重试 | 基础设施重试(Asynq) 与语义重试(LangGraph) 分离 | 单一重试层 | 两种失败性质不同 |
| 失败兜底 | 3 次后转人工 | 无限重试 | 成本与质量的可控性 |
| 视频传输 | 路径/对象存储 | gRPC 传二进制 | 内存与效率 |
| 前端 | 极简审核台 | 全功能编辑器 | 先跑通闭环，再谈体验 |

---

## 十、已知风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| LLM 生成的 Manim 代码编译失败率高 | 一次通过率低、成本高 | RAG few-shot + 静态校验 + 把编译错误回灌给 Coder 重写 |
| Manim 渲染慢（LaTeX 首次编译极慢） | 任务时延长 | 预热 TeX 缓存、低质量草稿渲染（`-ql`）先验证再高清重渲 |
| VLM 审查"幻觉式通过" | 质量漏检 | rubric 强约束 + 抽帧覆盖首尾 + 人工抽检 + 阈值可调 |
| 沙盒被绕过 | 安全 | 白名单 AST + 容器 `--network=none` + 非 root |
| 状态双写不一致 | UI 状态错乱 | 事件流单向驱动、幂等键、对账任务（阶段五） |
| 成本失控（大模型 + 渲染） | 预算 | 镜头级 attempt 上限、token 计量、草稿/成片两段渲染 |

---

## 十一、演进路线

1. **阶段一~四**：跑通"脚本 → 分镜 → 渲染 → 审查 → 人工打回 → 成片"的完整闭环。
2. **阶段五**：可观测性、成本核算、多租户。
3. **之后**：
   - 引入**声音侧智能体**（TTS 与画面对齐、语速驱动动画节奏）。
   - **跨镜头一致性**（配色/字体/术语表全局约束，避免每个镜头风格漂移）。
   - **模板库沉淀**：把高通过率的镜头代码反哺 RAG 语料，形成数据飞轮。
   - **局部重渲染**：只重渲某几秒而不是整镜，进一步降本。
