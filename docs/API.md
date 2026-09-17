# SciDirector API 契约

> 本文档描述三层接口：**Go 网关的 REST**、**反馈闭环的 WebSocket**、**Go ↔ Python 的 gRPC**。
>
> 唯一真源是 `proto/scidirector/v1/*.proto` 与 `backend/internal/httpapi/dto.go`。
> 改动契约时**必须**同步更新本文档（见 `Agent.md` §9）。

---

## 一、约定

### 1.1 响应信封

所有 REST 接口统一使用信封格式，便于前端写一份通用处理逻辑：

```jsonc
// 成功
{ "ok": true,  "data": { /* ... */ } }

// 失败
{
  "ok": false,
  "code": "BAD_REQUEST",       // 稳定错误码，前端据此分支（不要匹配 message）
  "message": "请求参数非法",     // 人类可读说明
  "detail": "Key: 'GenerateRequest.RawScript' ...",  // 调试细节
  "trace_id": "tr-1f2e3d4c"    // 用于把用户报错与服务端日志对上
}
```

### 1.2 错误码

| 错误码 | HTTP | 含义 | 前端建议动作 |
| --- | --- | --- | --- |
| `BAD_REQUEST` | 400 | 参数校验失败 | 提示用户修正输入 |
| `NOT_FOUND` | 404 | 任务 / 分镜不存在 | 返回列表页 |
| `CONFLICT` | 409 | 任务被并发修改 | 自动重试一次 |
| `UPSTREAM_UNAVAILABLE` | 503 | AI 大脑不可用 | 提示稍后重试 |
| `INTERNAL` | 500 | 服务内部错误 | 展示 trace_id 供反馈 |

### 1.3 链路追踪

客户端可携带 `X-Request-ID`；服务端会沿用该值，否则自行生成，并在**响应头**中回传。
该值同时出现在服务端所有相关日志的 `trace_id` 字段中。

---

## 二、REST（Go 网关，默认 `:8080`）

### 2.1 运维探针

#### `GET /healthz` —— 存活探针

只要进程能响应就返回 200。**刻意不检查下游依赖**：Redis 挂了就把 api 重启只会雪上加霜。

```json
{ "ok": true, "data": { "status": "ok", "version": "0.1.0-dev", "uptime_sec": 128.4 } }
```

#### `GET /readyz` —— 就绪探针

检查 Redis 与 AI 大脑。Redis 不可用返回 503（硬依赖）；
AI 不可用只标为 `degraded`（已提交的任务仍可查询）。

```json
{
  "ok": true,
  "data": {
    "status": "degraded",
    "version": "0.1.0-dev",
    "uptime_sec": 128.4,
    "components": { "redis": "ok", "ai": "degraded: connection refused" }
  }
}
```

#### `GET /version`

```json
{ "ok": true, "data": { "version": "0.1.0-dev", "env": "dev", "started_at": "...", "uptime_sec": 12.3 } }
```

---

### 2.2 提交生成任务

#### `POST /api/v1/generate`

**快速返回**接口：只做「建任务 + 入队」，结果通过 WebSocket 与查询接口获取。
同步等待生成完成会让 HTTP 连接超时，也会让前端无法展示中间进度。

请求体：

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `raw_script` | string | ✅ | 10 ~ 20000 字符 | 科普脚本原文 |
| `target_duration_sec` | number | | 5 ~ 1800，默认 90 | 目标总时长 |
| `locale` | string | | `zh-CN` / `en-US` / `ja-JP` | 默认 `zh-CN` |
| `style_guide` | object | | | 风格约束（配色、字体、字号下限、术语表） |

```bash
curl -X POST http://localhost:8080/api/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"raw_script":"从勾股定理出发……","target_duration_sec":90,"locale":"zh-CN"}'
```

响应 `202 Accepted`：

```json
{
  "ok": true,
  "data": {
    "job_id": "job-9f3a1c77b2e04d15",
    "status": "CREATED",
    "task_id": "d4f1a2b3c4",
    "created_at": "2026-02-14T08:31:02Z",
    "ws_url": "/ws/jobs/job-9f3a1c77b2e04d15"
  }
}
```

**快速失败**：AI 大脑不可用时立刻返回 503，而不是让任务在队列里干等。

---

### 2.3 查询任务

#### `GET /api/v1/jobs/:jobID`

```json
{
  "ok": true,
  "data": {
    "job": {
      "job_id": "job-9f3a…", "status": "RENDERING", "progress": 0.25,
      "raw_script": "…", "final_video_path": "",
      "shots": [ { "shot_id": "job-9f3a…-s000", "index": 0, "tag": "MATH",
                   "engine": "manim", "status": "APPROVED", "attempt": 1,
                   "artifact": { "video_path": "/data/work/…/shot_000.mp4", "duration_sec": 8.4 },
                   "feedbacks": [] } ]
    },
    "stat": { "total": 4, "approved": 1, "failed": 0, "awaiting_human": 0, "in_progress": 3 },
    "progress": 0.25
  }
}
```

#### `GET /api/v1/jobs/:jobID/shots`

只返回分镜数组（审核台的高频轮询端点，避免重复传整份脚本）。

#### `GET /api/v1/jobs/:jobID/events?after_id=0`

增量拉取事件流。`after_id` 之后的事件按**时间升序**返回。
这是 WebSocket 的降级通道，也用于断线重连补齐。

---

### 2.4 人类反馈闭环

#### `POST /api/v1/jobs/:jobID/shots/:shotID/reject`

打回某个镜头。**只重做这一镜**，不重跑全片。

```json
{ "comment": "坐标轴标签重叠了，把字号调小并旋转 45 度" }
```

响应 `202 Accepted`：

```json
{ "ok": true, "data": { "job_id": "…", "shot_id": "…-s002", "status": "RETRYING", "attempt": 2, "task_id": "…" } }
```

> `comment` 必填且至少 2 字符。这是**质量闸门**：无法转换为代码修改的意见
> （如「不好看」）会让重试必然失败，白白烧掉一次渲染 + 一次 VLM 调用，因此在入口就拦住。

#### `POST /api/v1/jobs/:jobID/shots/:shotID/approve`

人工放行。用于镜头处于 `AWAITING_HUMAN`（自动重试已熔断）时**接受当前效果并继续** ——
没有这个出口，整条片子可能永远无法交付。

```json
{ "ok": true, "data": { "job_id": "…", "shot_id": "…", "status": "APPROVED", "compose_enqueued": true } }
```

> **放行会顺带推进流水线**：当这次放行使**所有**镜头都通过时，服务端会
> **主动入队合成任务**，`compose_enqueued` 为 `true`。
>
> 这一点曾经缺失，后果很隐蔽：只把任务状态改成 `COMPOSING` 是不够的 ——
> 状态本身不驱动任何东西，必须有人把任务放进队列。结果是最后一个熔断镜头
> 放行后任务**永远停在 COMPOSING**，前端看着进度 100% 却等不到成片，
> 而且没有任何错误可报。

#### `PATCH /api/v1/jobs/:jobID/shots/:shotID` —— 成分镜编辑

改文案而不是重写画面。审核员打回时常常发现问题**出在文案**而
不是画面（「画外音说'三种情况'但画面只画了两种」），此时重写整个镜头是浪费。

```jsonc
{ "narration": "改后的画外音",      // 可省略 = 不改
  "visual_brief": "改后的画面说明",  // 可省略 = 不改
  "redo": true,                      // 是否立即重做（消耗一次 attempt 额度）
  "comment": "画面太挤" }             // 随重做回灌的意见，可选
```

> 字段是**可选指针**语义：**不传 = 不改，传空串 = 清空**。
> 用值类型 + 零值判断会把「清空画外音」误判成「不改」，是一个静默不生效的缺陷。
> 三类判断都不给（两个字段都没传且 `redo=false`）时返回 `400` ——
> 空请求返回 200 会让人以为改动生效了。

响应：

```json
{ "ok": true, "data": { "job_id": "…", "shot_id": "…", "status": "RETRYING",
                        "attempt": 2, "task_id": "…", "changed": ["narration"] } }
```

`changed` 列出真正被修改的字段，便于前端提示与审计。

#### 三种人工操作的分工（审核台的核心心智模型）

| 操作 | 什么时候用 | 代价 |
| --- | --- | --- |
| **放行** `approve` | 熔断镜头接受现状，继续走 | 无；全部通过则触发合成 |
| **打回** `reject` | **画面**不对，需要重写代码 | 一次重渲 + 一次 VLM 审查 |
| **编辑** `patch` | **文案**不对，画面逻辑本身是对的 | 改字段后重渲（保留导演原始意图） |

---

## 三、WebSocket（反馈闭环）

### 3.1 连接

```
ws://localhost:8080/ws/jobs/{jobID}
```

任务不存在时**在升级前**返回 404 —— 否则前端会连上一个永远没有事件的空频道，
表现为「一直转圈」，极难排查。

### 3.2 下行消息

统一信封：`{ "type": "...", "seq": 123, "data": ... }`

| type | 时机 | data |
| --- | --- | --- |
| `snapshot` | 连接建立后立即发送 | `{ job, stat, progress, events: [...] }` |
| `event` | 每次状态迁移 | 一条 `Event`（见下） |
| `events` | 响应 `resync` | `Event[]` |
| `ack` | 响应 `feedback` | `{ shot_id, accepted }` |
| `pong` | 响应 `ping` | 服务端时间 |
| `error` | 请求非法 | 字符串说明 |

**加入即重放**：`snapshot` 一次性下发任务快照 + 全部历史事件，
使刷新页面不丢上下文，前端也不需要区分「首次连接」与「重连」。

事件结构：

```json
{
  "event_id": 17,
  "job_id": "job-9f3a…",
  "shot_id": "job-9f3a…-s002",
  "shot_index": 2,
  "node": "critique",
  "status": "REJECTED",
  "message": "审查不通过：字号过小",
  "attempt": 2,
  "progress": 0.25,
  "error": "",
  "payload": { "feedback": { "passed": false, "score": 0.42,
                             "suggestions": ["把字号从 24 提到 48"] } },
  "ts": "2026-02-14T08:33:10Z"
}
```

`node` 取值：`plan` / `code` / `render` / `critique` / `revise` / `advance` / `compose` / `hitl` / `pipeline` / `api`。

### 3.3 上行消息

```jsonc
// 提交人工意见（等价于 REST 的打回接口，走 WS 是为了低延迟交互）
{ "type": "feedback", "shot_id": "job-9f3a…-s002", "comment": "坐标轴标签重叠" }

// 重连后补齐事件
{ "type": "resync", "after_id": 16 }

// 应用层心跳（某些代理会吞掉 WS 控制帧）
{ "type": "ping" }
```

### 3.4 可靠性设计

- **先落地后广播**：事件先写入 Redis 事件流（含 `Publish`），再由 api 侧订阅转发给浏览器。
  因此客户端收到的任何事件，都一定能从历史接口重新读到 —— 否则重连会出现事件缺口。
- **慢消费者隔离**：某个客户端发送缓冲满时**只断开该客户端**，不阻塞整条任务的事件流。
  前端重连后用 `after_id` 补齐。
- **多实例友好**：worker 与 api 可以是不同进程/容器，事件通过 Redis Pub/Sub 跨进程传递。

---

## 四、gRPC（Go → Python）

**服务定义**：`proto/scidirector/v1/ai_service.proto`
**默认地址**：`ai:50051`（容器内） / `localhost:50051`（本地）

### 4.1 方法一览

| 方法 | 类型 | 用途 | 阶段 |
| --- | --- | --- | --- |
| `Health` | Unary | 能力与沙盒就绪探测 | ✅ 已实现 |
| `PlanScript` | Unary | 只跑导演智能体：脚本 → 分镜表 | ✅ 已实现 |
| `RunPipeline` | **Server streaming** | 跑完整条 LangGraph，逐条推送事件 | ✅ 已实现 |
| `GenerateShot` | Unary | 编码 + 渲染单个镜头 | ✅ 已实现 |
| `CritiqueShot` | Unary | VLM 审查单次产物 | ✅ 已实现 |
| `ReviseShot` | Unary | 人工意见回灌 + 单镜头重做 | ✅ 已实现 |

> **为什么 `RunPipeline` 用流式**：一次生成包含 N 个镜头 × M 次尝试 × 4 个节点。
> 轮询既浪费又会延迟数秒；流式让每一步状态变化立刻抵达前端。

> **灰度安全网**：`UNIMPLEMENTED` 分支在 Go 侧**长期保留**。以后新增 RPC 时，
> 老版本大脑返回的 `UNIMPLEMENTED` 仍会被判定为不可重试，不会被 Asynq 反复重投。

### 4.2 各方法契约

#### `Health`

请求：空。响应：

```jsonc
{
  "healthy": true, "version": "0.1.0",
  "llm_provider": "mock", "vlm_model": "gpt-4o",
  "sandbox_ready": true,
  "capabilities": ["plan", "manim", "compose", "vlm", "tool:ffmpeg=ok", "tool:latex=missing"],
  "uptime_sec": 3600
}
```

`capabilities` 中带 `tool:<name>=ok|missing` 的条目用于让编排层**提前知道哪些标签不可渲染**，
而不是等任务跑到那一步才失败。

#### `RunPipeline`

请求 `RunPipelineRequest`：

| 字段 | 说明 |
| --- | --- |
| `job_id` | 任务标识，同时作为 LangGraph 的线程 ID |
| `raw_script` / `style_guide_json` / `target_duration_sec` / `locale` | 生成参数 |
| `max_attempts_per_shot` | 每镜头重试上限（熔断阈值） |
| `resume` / `checkpoint_thread_id` | 断点续跑（需 Postgres checkpointer） |

响应：`stream PipelineEvent`（见 `common.proto`）。

**跨语言约定（重要）**：`plan` 节点的 `PipelineEvent.payload_json` 为

```json
{ "outline": "…", "shots": [ { "shot_id": "…", "index": 0, "tag": "SCENE_TAG_MATH", "engine": "RENDER_ENGINE_MANIM", "duration_sec": 6.5, "attempt": 1, "status": "SHOT_STATUS_PENDING" } ] }
```

Go 侧 `worker.syncShotsFromPayload` 解析它并**整体替换**任务的分镜表（保留已有渲染进度）。
之所以用 JSON 而不是 proto 的 `repeated` 字段：分镜表仍在快速迭代期，
用 JSON 可以让它的结构演进不必每次都重新生成两侧代码。

> ⚠️ **键名必须是 snake_case，枚举必须是数字**。这段 JSON 最终由 Go 的
> `encoding/json` 反序列化进 `pb.ShotSpec`；写成驼峰或枚举名字符串时，
> Python 侧看不出任何问题，到 Go 侧会**静默变成零值**。
> `backend/internal/worker/processor_test.go` 里有一份对称的契约测试守着这条约定。

#### `CritiqueShot` 的强约束输出

审查智能体被要求只输出 JSON（中文系统提示词见
`ai/scidirector_ai/agents/prompts/critic.md`），核心字段：

```json
{ "passed": false,
  "scores": { "logic": 0.7, "readability": 0.4, "pacing": 0.8, "aesthetics": 0.75 },
  "suggestions": [ { "dimension": "readability", "severity": "high", "advice": "…" } ],
  "summary": "…" }
```

`passed` 的最终取值 = **模型判定 AND 程序侧复核**：
分维度加权得分低于 `SCID_CRITIC_SCORE_THRESHOLD`（默认 0.75），
或 `logic` / `readability` 跌破硬性下限，都会被程序改判为不通过 ——
**模型只能更严格，不能更宽松**。两者不一致时事件里会留下 `verdict_disagreement` 痕迹。

VLM 不可用时**降级为转人工**（`degraded=true`），而不是伪造「通过」。

#### 状态码映射

| gRPC 状态 | 触发条件 | Go 侧行为 |
| --- | --- | --- |
| `UNIMPLEMENTED` | 功能尚未实现（灰度/老版本大脑） | **不可重试**，直接失败并告警 |
| `UNAVAILABLE` | 依赖不可用 | 交给 Asynq 重试（指数退避） |
| `DEADLINE_EXCEEDED` | 调用超时 | 同上 |
| `FAILED_PRECONDITION` | 渲染失败等业务拒绝 | 记录详情，按镜头级失败处理（不重投整个任务） |
| `INTERNAL` | 业务/模型错误 | 记录详情，按任务策略处理 |

> 这个区分很关键：把「还没实现」误报成 `INTERNAL`，会让 Asynq 重试同一个注定失败的调用，
> 既浪费资源又污染告警。

> 同理，**「引擎工具链缺失」属镜头级失败**，走熔断转 `AWAITING_HUMAN`，
> 而不是让整个任务失败 —— 一个镜头渲染不出来，不该拖垮其余镜头。

---

## 五、REST（Python 大脑，默认 `:8000`）

Python 侧的 HTTP 是**运维与调试通道**，Go 层通过 gRPC 与它交互。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 存活 + 能力与工具链明细 |
| `GET` | `/readyz` | 就绪：沙盒不可用时返回 503 |
| `GET` | `/version` | 版本 + 配置摘要（密钥已掩码） |
| `POST` | `/v1/plan` | 只跑导演智能体，便于用 curl 直接验证提示词与模型配置 |
| `POST` | `/v1/pipeline` | 完整流水线，NDJSON 逐行流出事件（调试用，便于用 curl 观察） |

---

## 队列可观测（Go 网关）

`GET /api/v1/queue/stats`

```jsonc
{
  "queues": [
    { "queue": "critical", "size": 2, "pending": 1, "active": 1, "scheduled": 0,
      "retry": 0, "archived": 0, "completed": 12, "paused": false, "latency_sec": 0.3 }
  ],
  "totals": { "queue": "total", "size": 2, "pending": 1, "active": 1, "...": 0 },
  "fetched_at_unix_ms": 1789525643415
}
```

字段怎么读：

| 字段 | 含义 | 什么时候要看它 |
| --- | --- | --- |
| `size` | 积压量（pending+active+scheduled+retry） | 判断「系统是否跟不上」最直接的指标 |
| `retry` | 等待重试的任务数 | 持续不为 0 → 有任务在反复失败 |
| `archived` | 已归档（不再重试）的任务数 | **任务级最终失败都在这里**，需要人工介入 |
| `latency_sec` | 最老待处理任务的等待时长 | 比深度更能说明体感：深度 3 但都等了 10 分钟，与深度 300 但只等 1 秒，是两种完全不同的问题。汇总时取**最大值**而非求和，因为「总延迟加起来」没有意义 |
| `paused` | 队列被暂停（任务堆积但不消费） | 任一分队列暂停都会体现在汇总上 |

两个刻意设计：

- **队列不存在时返回全 0，而不是报错。** Asynq 只在队列第一次收到任务时才创建
  元数据，因此全新部署上所有队列都「不存在」。此时若报错，这个接口会在最需要
  它的时刻（确认「没东西卡住」）整个失败。对运维而言「从未有任务」与「空队列」
  本来就是同一件事。
- **采集失败返回 503 而不是 500。** 那是依赖不可用（Redis 抖动），不是服务端
  代码错误，语义更准确，也便于告警分流。

`POST /v1/plan` 请求体：

```json
{ "job_id": "debug-1", "raw_script": "…", "target_duration_sec": 60,
  "locale": "zh-CN", "style_guide_json": "" }
```

响应：

```json
{ "job_id": "debug-1", "outline": "问题 → 原理 → 数据 → 总结",
  "total_tokens": 812,
  "shots": [ { "shot_id": "debug-1-s000", "index": 0, "tag": "AMBIENCE",
               "engine": "stock", "duration_sec": 4.5,
               "narration": "…", "visual_brief": "…", "keywords": ["开场"] } ] }
```
