# SciDirector 路线图与验收标准

> 每个阶段都必须满足两条：**可运行**（能跑起来并演示）与**可验证**（有明确的验收命令与预期输出）。
> 「代码写完了」不算完成，「验收命令能给出预期结果」才算。

---

## 阶段一 · 环境与骨架搭建 ✅ 已完成

### 交付物

| 类别 | 产物 |
| --- | --- |
| 契约 | `proto/scidirector/v1/common.proto`、`ai_service.proto`；Go 与 Python 生成代码均已入库 |
| 编排 | `docker-compose.yml`（Redis / Postgres / MinIO / ai / api / worker / web） |
| 基础设施 | `deploy/redis/redis.conf`（AOF + 内存策略）、`deploy/postgres/init/001_init.sql`（审计表 + 向量扩展兜底）、`deploy/nginx/default.conf` |
| Go 骨架 | 配置、结构化日志、**领域模型 + 状态机**、Redis 仓储（分布式锁 + 事件流 + Pub/Sub）、Asynq 客户端与服务端、gRPC 客户端、ffmpeg 封装、Gin 路由与中间件、WebSocket Hub、worker 处理器、`cmd/api` + `cmd/worker` |
| Python 骨架 | 配置（pydantic-settings）、JSON 日志与 contextvar 链路绑定、领域模型（含业务校验）、LLM/VLM 客户端（重试 / 结构化输出 / 成本 / **mock 模式**）、LangGraph 状态定义、导演智能体、沙盒静态策略、业务门面、gRPC servicer、FastAPI + gRPC 双栈入口 |
| 注入（提前完成） | Go：主链路 worker 处理器（gRPC 流式消费 + 事件落库）、ffmpeg 并发归一化与合成；Python：导演智能体完整实现、沙盒静态安全策略 |
| 文档 | `Agent.md`、`docs/DESIGN.md`、`docs/API.md`、`docs/ROADMAP.md`、`README.md` |
| 工具 | `Makefile`、`scripts/dev-env.ps1`、`scripts/gen-proto.ps1`、`backend/Dockerfile`、`ai/Dockerfile`、`web/Dockerfile` |

### 验收命令与预期

```bash
# 1) Go 静态检查与测试
cd backend && gofmt -l . && go vet ./... && go test -count=1 ./...
# 预期：gofmt 无输出；vet 无输出；domain 包测试 ok
# 注意：CI（Linux）使用 go test -race；Windows 本地若缺少 race runtime DLL
#       会报 exit status 0xc0000139，此时用 `make test-go RACE=` 关闭竞态检测。

# 2) Go 可编译
cd backend && go build ./cmd/api ./cmd/worker
# 预期：无输出，退出码 0

# 3) Python 测试（含 gRPC 端到端集成）
cd ai && python -m pytest -q
# 预期：全部通过（沙盒策略、领域模型、状态、gRPC round-trip）

# 4) 契约生成可复现
make proto        # 或 .\scripts\gen-proto.ps1
# 预期：生成物与仓库中的一致（git diff 为空）

# 5) 全栈可启动
docker compose up -d --build && docker compose ps
# 预期：所有服务 healthy/running
```

### 已知边界（不是缺陷，是有意为之）

- `RunPipeline` / `GenerateShot` / `CritiqueShot` / `ReviseShot` 返回 `UNIMPLEMENTED`，
  这是**正确行为**：Go 侧应当据此判定「不可重试」，而不是把它当成基础设施故障反复重投。
- `Agent.md` 中「阶段二」的检查项当时保持未勾选状态
  （阶段二完成后已全部勾选，见迭代日志 v0.2.0）。

### 手动测试发现并已修复的问题

以下两项由手工联调实测发现，**已修复并补了回归测试**：

| # | 问题 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| 1 | `UNIMPLEMENTED` 被 Asynq 当成可重试错误，与 `Agent.md §9` 的约定不符（实测 `asynq:{critical}:retry` 堆积，`max_retry=5`） | `ai.wrapRPCError` 为 `codes.Unimplemented` 引入哨兵 `ai.ErrNotImplemented`；`worker` 主链路与镜头重做链路遇到它时返回 `asynq.SkipRetry`，任务**直接归档**而不是重试 5 次 | `backend/internal/ai/client_test.go`（可重试性分类 + 哨兵互斥 + 取消还原） |
| 2 | `health()` 的 `sandbox_ready` 判定与自身 docstring 不一致（写「Manim 与 ffmpeg 都可用」，实现却是 `python and ffmpeg`），导致一个渲染引擎都不可用时仍报 ready | 抽出 `config.ENGINE_REQUIREMENTS` + `config.engine_availability()`（纯函数）；`sandbox_ready` 改为**至少一个引擎可用** `any(engines.values())`；`/healthz` 与 `/readyz` 新增结构化 `engines` 字段，逐引擎暴露 `engine:<名>=ok\|missing` | `ai/tests/test_service_health.py`（12 项，含 `sandbox_ready == any(engines.values())` 不变式） |

修复后的实测结果：

```
POST /api/v1/generate  ->  asynq:{critical}:retry = 0, archived = 1   # 不再重试
GET  /api/v1/jobs/<id> ->  status=FAILED
                           error="ai: Python 端尚未实现该能力: RunPipeline ... Unimplemented"

GET  /readyz -> {"sandbox_ready":true,
                 "engines":{"manim":false,"d3":false,"echarts":false,
                            "code_anim":false,"stock":true},
                 "capabilities":[...,"engine:manim=missing","engine:stock=ok"]}
```

> 这两项是**手工联调才能发现**的问题 —— 单测不启动 Asynq 的真实重试，
> 编译检查也不看文档与代码的语义一致性。这也是"必须真的把链路跑一遍"的最好例证。

---

## 阶段二 · Python 多智能体核心 ✅ 已完成

> 五个 RPC 全部实现，Go 侧经 gRPC 调通全链路（`deca482`）。
> 验收结果见下方「阶段二验收结果」。

### 交付物

1. **LangGraph 图**（`graph/builder.py` + `graph/nodes.py`）
   - 节点：`plan` → `code` → `render` → `critique` →（不合格）`revise` → `code`；合格 → `advance` → 下一镜头
   - 条件边实现「带反馈的循环」与「每镜头独立的 attempt 计数器」
   - Postgres checkpointer：进程重启后可从断点续跑
   - 成片合成不在 Python 图内 —— 那是 Go 侧的职责（它才持有 ffmpeg 与产物卷）
2. **编码智能体**（`agents/coder.py`）
   - 按标签确定性路由到 Manim / D3 / ECharts / 代码动画
   - 生成前经 RAG 召回 2~3 条同标签优秀范例注入上下文
   - 生成后经沙盒静态策略校验，违规信息回灌重写
3. **审查智能体**（`agents/critic.py`）
   - 抽帧（等间隔 + **首末帧**，首尾的字幕截断/元素溢出最容易被均匀采样漏掉）
   - rubric 分维度打分：逻辑一致性 / 文字可读性 / 信息密度 / 节奏 / 美观度
   - 输出强约束 JSON；不通过时必须给出**可执行**建议
   - VLM 不可用时**降级转人工**（`degraded=true`），而不是伪造「通过」
4. **沙盒执行器**（`sandbox/runner.py`）
   - 独立子进程 + `resource` 限制（CPU 时间、RSS 上限）
   - 超时 SIGKILL，且判定为「该次尝试失败」而非任务失败
   - 产物校验：文件存在、非空、能被 ffprobe 解析
5. **RAG**（`rag/store.py`）
   - 起步用本地向量索引 / pgvector，避免过早引入重型向量库

### 多租户：做到哪一步、证据是什么

**先说清楚这是什么、不是什么。** 租户身份由请求头 `X-Tenant-ID` 声明，因此：

- **能保证**：一个租户拿不到另一个租户的任务，即使猜到 job_id 也不行。
- **不能保证**：抵御一个能自己伪造请求头的攻击者。在「可信网关已鉴权并把身份透传下来」
  的部署里成立；直接暴露到公网则不成立。生产应把 `TenantMiddleware` 换成从令牌/证书取身份的实现
  （隔离逻辑本身不用改）。把这条写明，是因为**误以为已经安全**比明确没有安全更危险。

**归属**：`Job.TenantID` 在创建时落定、之后不可更改（归属可变的话"谁有权看"就不可推理）。
旧任务没有该字段，读取时按 `default` 处理 —— 否则上线即"数据丢失"；但兼容不等于放开，
其它租户依然读不到它们（有用例同时钉住这两点）。

**隔离的收口点**：

| 路径 | 收口 |
| --- | --- |
| 读取 | `store.GetJobForTenant` ← `httpapi.loadJobForTenant`（所有按 job_id 的 handler 的唯一入口） |
| 写入 | `store.UpdateJobForTenant`（**校验在锁内**，否则是 TOCTOU） |
| WebSocket | 连接时校验；上行消息（打回/重做）同样校验 —— 它是**另一个入口**，不能因为"REST 查过了"就跳过 |

越权与「不存在」**完全不可区分**：同一个 404、`detail` 传 `nil`，只在服务端日志里区分
（否则运维查不到越权尝试）。

**配额**（`SCID_TENANT_MAX_ACTIVE_JOBS`，缺省 0 = 不限制）：按租户限制在跑任务数。
实现在跑集合 + **现算自愈**，而不是「创建 +1、结束 -1」的计数器 —— 后者在 worker 崩溃、
任务被清理、进程重启后会永久偏高，表现为「这个租户再也提交不了任务」且没有任何日志能说明原因。
检查放在 AI 探活**之前**：本地、确定、便宜且更具体的判断先做。

**验证**

- **路由表驱动的隔离用例**：从 `router.Routes()` 取出**所有**带 `:jobID` 的路径（8 条，
  含 WS），逐个以「另一个租户」的身份访问，断言 404 且响应与「任务不存在」逐字节一致
  （`trace_id` 除外）。手写清单会在新增接口时悄悄过期，而"悄悄过期"正是隔离最容易失效的方式。
  配套反向保护：路由数少于 7 条即判失败（防止用例退化成永真）。
- 正向对照：本租户对四个读取接口必须 200（少了它，一个"永远 404"的实现也能让上面全绿）。
- **变异验证**：把 `JobBelongsTo` 改成恒真后，8 条路由全红。
- **真实环境复验**：acme 建任务 → 自己 200 / globex 404 / 无租户头回落 default 404；
  配额 `limit=2` 时第 3、4 次提交返回 **429**，消息含确切数字 `2/2`，另一租户不受影响。

**本轮抓出的两个真实缺陷**（都不是"想到的"，是用例测出来的）

1. **approve/reject 返回 500 且 detail 写着「任务不属于该租户」** —— 状态码与文案都泄漏了
   job 的存在性，可被用来枚举有效 ID。已改为与「不存在」完全一致的 404。
2. **授权降级（更严重）**：任务是整份 JSON 存 Redis 的，而 api 与 worker **都会读改写**它。
   加了 `tenant_id` 却只重启 api 时，仍在跑的旧 worker 一碰任务就把它**静默抹掉** ——
   而「缺失 = default」导致**任务主人读不到自己的任务**，且该任务对 default 租户变得可见。
   两道防线：`store` 写回时保留未知字段（`mergeUnknownFields`），让**以后**加字段真正滚动兼容；
   并明确记录「这对**已经部署出去的旧二进制无效** —— 改结构体字段必须 api/worker 一起重启」
   这条部署纪律。修复后真实复验：任务跑完（PARTIAL）后 Redis 里 `tenant_id` 仍为 `acme`。

**没做到的部分**

- **身份来源不是认证**（见上）。
- **没有按存储做物理隔离**：所有租户的任务仍在同一个 Redis DB、同一个键空间
  （`scid:job:<id>` 不带租户前缀）。隔离靠**访问路径收口**，不是靠存储结构 ——
  这对当前规模是合适的取舍，但意味着"任何绕过 store 的直连读取"都会绕过隔离。
  真要物理隔离需要按租户分库/分键前缀，那是另一轮改动。
- **配额只按"在跑任务数"计**，没有按 token/渲染时长计（成本口径已具备，见成本核算一节）。
- **`/api/v1/queue/stats` 仍是全局视图**，未按租户切分。

### 状态对账：做到哪一步、证据是什么

这是**刻意的双写**（见 `docs/DESIGN.md`）：Redis 保存对外可见的任务/镜头状态，
checkpoint 保存图能从哪里续跑，两者以 job_id 对齐、由事件流单向同步 ——
也就是说 Redis 是**被事件推着走**的。于是只要事件中途丢了（进程被杀、订阅断了、
状态已推进但写事件失败），两侧就会分叉，而且**没有任何一处会报错**：
任务是永远停在非终态，用户看着 100% 却等不到成片。

**接口与开关**

| 入口 | 说明 |
| --- | --- |
| `GET /api/v1/jobs/:id/reconcile` | 按需对账。默认**只读**；`?repair=true` 才改状态。同样受租户归属校验 |
| `SCID_RECONCILE_INTERVAL` | worker 的周期扫描间隔，缺省 **0 = 关闭**（单机开发不需要；多副本部署时每个副本都扫是浪费，但修复幂等所以不会产生错误结果） |
| `GetCheckpointSnapshot` RPC | 只读、无副作用。**为什么由 Python 读**：表结构与序列化是 LangGraph 的实现细节，让 Go 直接读 Postgres 等于把两个项目的内部实现焊在一起，一次 LangGraph 升级就会静默读出错数据 |

**自动修复只做一类，并且依据必须来自被修的那一侧**

只修「Redis 非终态 + checkpoint 显示图已跑完」—— 这是"最后一次状态迁移丢失"的形状。
终态由 **Redis 自己的镜头状态**推出（`domain.TerminalStatusFromShots`），
绝不引入 checkpoint 的信息去改写对外状态：后者等于"用内部状态改写用户看到的东西"，
风险远大于它解决的问题。其余各类**只报告不修**，因为它们的正确做法依赖运维判断。

「Redis 已终态而 checkpoint 尚未跑完」这一类显式**不可修**：图可能继续推进并发出新事件，
把用户已经看到的"已完成"拉回进行中是危险的。

**为什么判断规则是纯函数**

`domain.Reconcile` 不碰 IO，只吃两侧快照。它一旦算错，表现是"报告全是误报"——
而误报会让人干脆不再看这份报告，那比没有对账更糟。因此用例里大量是**反向控制**：
两侧一致时不报、已终态时不报 `stuck_job`、非持久后端只报 `unknown` 且**不算**不一致。
另做了变异验证：把 `checkpoint_ahead` 标成可修、把非持久后端报成 `stuck_job`，
两处都会立刻变红。

**真实环境端到端复验**（本机全真实依赖）

1. 跑一个任务到 `PARTIAL`（checkpoint `finished=true`）；
2. **忠实注入故障**：把 Redis 状态改回 `RENDERING`，并放回在跑索引 ——
   真实故障里任务**从未**变成终态，所以索引里也从未被剔除；
3. 周期扫描在下一个 tick 检出并修复：日志
   `对账修复：任务状态已修正 from=RENDERING to=PARTIAL`、
   `checked=1 diverged=1 repaired=1`；
4. 事件流里出现 `node=reconcile` 的事件，payload 带 `reason/from/to/backend`
   ——**这是前端能更新的唯一途径**，不写事件就会出现"接口读出来已完成、页面还在转圈"；
5. 只读模式检出差异但状态未变、也没写事件；
6. 连续三次 `?repair=true` 只产生 **1** 条事件（幂等）；
7. 跨租户访问该接口 404。

**没做到的部分**

- **只覆盖一类自动修复**。其余差异（checkpoint 缺线程、产物只在单侧存在、
  游标超过镜头数）只报告，需要人工判断。
- **在跑索引的能力边界**：任务以单键（`scid:job:<id>`）存储、没有索引，
  因此"有哪些任务在跑"靠一个**全局在跑集合**回答（复用租户配额那套"现算自愈"）。
  集合在任务进入终态时被剔除，所以它覆盖的是"从未变成终态"的任务 ——
  正是真实故障的形状；但"曾经终态、后来又被改回非终态"的任务**不会被扫到**。
- **未验证多副本并发对账**：修复动作是幂等的（写入前二次确认），但两个副本同时
  发现同一任务时的行为没有实测。
- **没有对账的历史留痕**：修复写了事件，但"扫描过哪些任务、结论是什么"没有落库，
  只能在日志里看。

### 验收标准

| 编号 | 验收项 | 判定方式 |
| --- | --- | --- |
| A1 | 给定脚本，`RunPipeline` 能产出至少 1 个可播放的 MP4 片段 | 检查产物存在且 `ffprobe` 可解析 |
| A2 | 数学镜头走 Manim、数据镜头走 D3（路由正确） | 检查事件的 `node`/`artifact.engine` |
| A3 | 人为注入「字号过小」的代码，审查必须判为不合格并给出可执行建议 | 单测 + 人工构造用例 |
| A4 | 不合格镜头能自动重试，且第 N 次仍不合格时转 `AWAITING_HUMAN` | 日志中出现熔断事件 |
| A5 | 沙盒能拦截 `import os` / `eval` / `__subclasses__` 等逃逸手法 | `tests/test_sandbox_policy.py` |
| A6 | 沙盒超时后进程被真正杀死，不留孤儿进程 | 渲染死循环代码并检查进程表 |
| A7 | 渲染失败时把编译错误回灌给编码智能体，重试后修复 | 构造语法错误用例 |
| A8 | 断点续跑：杀死进程后重启，流水线从最后完成的节点继续 | 手动验证 + checkpoint 查询 |

### 关键指标（阶段二结束时需产出基线）

- **一次通过率（first-pass rate）**：最重要的北极星指标，同时反映提示词质量、RAG 质量与路由正确性
- 平均尝试次数
- 单镜头渲染耗时分布
- token 成本 / 镜头

> 指标采集依赖阶段五的 metrics 管道，当前仅在图终止节点的 `token_summary` 事件里
> 输出 token 汇总，尚未做时间序列沉淀。

### 阶段二验收结果

验收环境：本机仅具备 `stock`（ffmpeg）引擎，`manim` / `d3` / `echarts` / `code_anim`
的本地工具链缺失（`engine_availability()` 实测 `missing`），因此**依赖 LLM 生成代码的引擎
无法在本机真实渲染**，相关验收项以「路由单测 + 熔断行为」替代验证。

| 编号 | 结果 | 证据 |
| --- | --- | --- |
| A1 | ✅ | 端到端任务 `job-799a0041b7ad3434` 产出可播放 MP4（AMBIENCE 镜头经 ffmpeg lavfi 生成），`ffprobe` 可解析 |
| A2 | ⚠️ 部分 | 路由正确性由 `tests/test_graph_routing.py` 覆盖（MATH→manim、DATA→d3、CODE→code_anim、AMBIENCE→stock），事件中 `artifact.engine` 与标签一致；但本机缺 manim/d3 工具链，真实渲染未跑通 |
| A3 | ✅ | `tests/test_critic_agent.py` 构造「字号过小 / 信息密度过高」用例，判定不合格且给出可执行建议 |
| A4 | ✅ | 真实运行中 MATH / DATA 镜头在第 3 次尝试后熔断，事件流出现 `AWAITING_HUMAN`，任务状态置 `PARTIAL` |
| A5 | ✅ | `tests/test_sandbox_policy.py`（`import os` / `eval` / `__subclasses__` 等逃逸手法全部拦截） |
| A6 | ✅ | `tests/test_sandbox_runner.py` 死循环用例超时后进程树被杀，无孤儿进程；Windows 走 Job Object，POSIX 走 `RLIMIT_DATA`（Linux 4.7+ 覆盖 brk 与私有匿名映射）+ `killpg`。**v0.4.2 修正**：此前 POSIX 侧用 `RLIMIT_AS`，它限制的是虚拟地址空间而非内存，会误杀需要大量地址空间的正常进程（实测 ffmpeg 抽一帧 RSS 56MB / 地址空间约 2GB），且失败形态是「退出码 0、产物为空」；同时补上 CPU 预算耗尽与墙钟超时相撞时的归一化 |
| A7 | ✅ | 渲染失败的错误信息回灌给编码智能体，`revise` 节点重写后重试 |
| A8 | ⚠️ 部分 | `build_checkpointer()` 在无 Postgres 时显式降级到 `MemorySaver` 并打印警告；断点续跑仅在 Postgres 可用时成立，本机未验证 |

**这次端到端跑测暴露的真实行为**（4 个镜头）：

| 镜头 | 标签 | 最终状态 | 说明 |
| --- | --- | --- | --- |
| #0 | AMBIENCE | `APPROVED` | 真实 MP4 产出 |
| #1 | MATH | `AWAITING_HUMAN` | 本地无 manim，连续失败后熔断 |
| #2 | DATA | `AWAITING_HUMAN` | 本地无 d3，第 3 次尝试后熔断 |
| #3 | AMBIENCE | `APPROVED` | 真实 MP4 产出 |

任务终态 `PARTIAL`，`progress=0.5`，
`stat={"total":4,"approved":2,"failed":0,"awaiting_human":2,"in_progress":0}`；
Go 事件存储累计 22 条事件。**「引擎缺失」被正确地当作失败而非崩溃**，熔断而非重试到死 ——
这正是阶段二要证明的行为。

### 阶段二开发中发现并已修复的问题

均已在当次提交内修复并补回归测试，记录在此以免重复踩坑：

| # | 问题 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | ffmpeg `drawtext` 报 `No option name near '/Windows/Fonts/msyh.ttc:…'` | Windows 路径的 `:` 与滤镜分隔符冲突，且该构建**没有 fontconfig** 无法回退 | `fontfile` 两级转义（`C\\:`）；`%` **不**转义；三级降级 + 字体探测缓存 |
| 2 | 渲染器切换 cwd 后相对路径失效 | 子进程/滤镜的 cwd 与主进程不同 | 传入子进程的路径一律 `.resolve()` |
| 3 | mock LLM 对着导演提示词返回了审查 JSON，静默解析成"空分镜表" | 客户端按提示词关键词嗅探任务类型 | 意图**显式传参** `llm.Task`，禁止文本嗅探 |
| 4 | 循环导入 `tools/__init__ → renderer → sandbox.manim → tools.media` | 媒体工具被放在 `tools/` 包，同时被上层与沙盒反向依赖 | `media.py` / `renderer.py` 提升到包根，`sandbox/__init__.py` 用 PEP 562 `__getattr__` 惰性转发 |
| 5 | `title_from` 按分隔符列表顺序取标题，而非文本位置 | 用了 `for sep in seps` 而非比较索引 | 改为取**最靠前**的分隔符位置 |
| 6 | 中文关键词召回恒为兜底分 | 空格分词对中文无效 | 改用 **CJK 字符二元组** 匹配 |
| 7 | 子进程 stderr 是 GBK，回灌给模型的编译错误是乱码 | Windows 中文环境默认代码页 | 沙盒强制注入 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` |
| 8 | 两个平台的内存限制产生不同的可观测结果 | POSIX 抛 `MemoryError`，Windows Job Object 由内核直接杀 | 统一归一化为 `killed_reason="memory"` |
| 9 | 不可重试的渲染失败只发 `FAILED`，**绕过了 `revise` 出口** | 节点直接调用了终止路径 | 改为发 `AWAITING_HUMAN` —— 有出口，而不是死路 |
| 10 | `MediaToolError` 逃逸出图，未被渲染层归一 | 异常类型未收敛 | 在渲染器边界统一转成 `RendererError` |
| 11 | HTML 契约违规生成的反馈是病句（「使用了被禁止的 缺少渲染契约…」） | 违规项只有"禁止什么"没有"该怎么改" | `PolicyViolation.advice` 支持直出人类可读建议 |
| 12 | `pbconv` 缺 `StatusFromPB` / `StatusToPB` | 枚举双向映射漏了一组 | 补齐并加对称测试 |
| 13 | proto3 未设置的 message 字段是**空消息**而非 `None` | 用 `is None` 判断永远为假 | 改用 `HasField` |
| 14 | 冒烟脚本按事件计数产物，导致 2 个镜头报出 4 个产物 | `render` 与 `critique` 事件共用同一个 artifact | 按 `video_path` 去重 |
| 15 | 测试断言 `common.ShotStatus.Name()` 的字符串 | protoc 版本差异会改变命名 | 改为比较枚举**数值** |

> 第 1、7、8 条是**真实 ffmpeg 与真实子进程才能暴露**的问题：
> 单元测试里用 mock 永远走不到这些分支。第 3、9、13 条属于
> **"看起来成功"的静默失败**，比抛异常危险得多 —— 这也是为什么每层落地都要立刻真跑一次。

---

## 阶段三 · Go 编排与媒体处理 ✅ 已完成

> 除「全片 TTS 配音」外全部落地。TTS 因本机无可用引擎且需先确定服务商，
> 单列为待办（见下方第 1 项）。

阶段一已注入主链路处理器与 ffmpeg 合成骨架，阶段三补齐：

1. **字幕与配音**：全片 TTS、字幕时间轴对齐、软字幕封装（`MuxFinal` 接口已就绪）
   - ✅ 字幕时间轴对齐 + 软字幕封装（mov_text）
   - ✅ **「按真实音频时长对齐」这一半已完成**（与选谁做 TTS 无关）。见下方
     「配音链路：已完成的那一半」。
   - ⏳ **配音合成本身仍未接入** —— 需先确定服务商（Edge TTS / Azure / OpenAI / 本地模型）。
     选型的关键差别不在音质，而在**能不能拿到时间戳**：能拿到的（Azure / Edge）
     可以把 cue 切得更细；只能拿到音频的（OpenAI）则按镜头时长对齐（本仓库已实现的那一半）。
     确定之后要写的只有「产生音频」这一步：把音频路径写进 `RenderArtifact.audio_path`，
     其余链路已就绪，且已被真实音频（合成的）端到端验证过。
2. **转场与统一规格**：跨镜头转场、统一调色（避免 Manim 与浏览器录制出的画面质感割裂）— ✅ 已完成
3. **局部重渲染**：只重渲某几秒而不是整镜（进一步降本）— ✅ 已完成
4. **产物归档**：中间产物与成片上传 MinIO，本地卷按策略清理 — ✅ 已完成
   （归档后端 none/local/s3 三选一；本地卷清理策略已实现并单测。
   **s3 路径已补上端到端验证**，见下方「s3 归档路径的验证方式」。
   默认后端仍是 local：缺省配置不依赖任何对象存储，这是刻意的）
5. **队列可观测**：暴露 Asynq 队列深度、失败任务数 — ✅ 已完成
   （`GET /api/v1/queue/stats`，含 size/retry/archived/latency 与汇总；
   已用真实 Redis 端到端验证）

### 阶段三验收结果

| 编号 | 结果 | 证据 |
| --- | --- | --- |
| B1 | ✅ | `TestRunnerBoundsRealFFmpegProcesses` 在真实 ffmpeg 下采样信号量占用，断言并发峰值**恰好等于**上限（只断言「不超过」会漏掉「根本没并发」的退化） |
| B2 | ✅ | `TestNormalizeAndConcatDifferentSpecs`：尺寸/帧率不一致的片段归一化后 concat，成片规格与时长正确；`TestConcatHandlesApostropheInPath` 覆盖含单引号路径 |
| B3 | ✅ | `TestRunnerCancelKillsFFmpegPromptly`：120 秒素材在取消后毫秒级返回，且槽位被归还 |
| B4 | ✅ | 见下方「B4 / B5 的验证方式」：入队侧 2 项 + 执行侧 4 项，全部基于**真实 gRPC server + 真实 Redis**，不用接口替身 |
| B5 | ✅ | 私有 `redis-server` + `SIGKILL` 真实故障注入 2 项：排队中的任务靠 AOF 恢复、执行中的任务靠租约过期 + recoverer 重投（后者为慢用例，`make test-failover` 显式运行） |

阶段三实际交付范围见 `Agent.md` 的迭代日志 v0.3.0。

### B4 / B5 的验证方式

这两项都属于「不真的把并发/故障造出来就测不到」的类别，因此都用**真实依赖**验证。

**B4 —— 同一 `(job, shot, attempt)` 重复投递只渲染一次**。
实际有**两道**闸门，防的不是同一件事，缺一不可：

| 闸门 | 位置 | 防的是什么 | 用例 |
| --- | --- | --- | --- |
| 入队侧 | `EnqueueRenderShot` 的 `asynq.TaskID` | 人工连点两下、前端重试 —— 任务根本不会重复入队 | `TestB4EnqueueRenderShotDeduplicatesSameAttempt`、`TestB4EnqueueRenderShotAllowsNewAttempt` |
| 执行侧 | `HandleRenderShot` 的 `AcquireIdempotencyKey` | Asynq **执行成功但确认失败**后的重复投递 —— 此时入队侧完全帮不上忙 | `TestB4ConcurrentDuplicateDeliveryRendersOnce` 等 4 项 |

执行侧用例不 mock 依赖：它起一个**真实的 gRPC server** 冒充 Python 大脑
（只负责数 `ReviseShot` 被调用了几次），配真实 Redis，
因此连 `ai.Client` 的错误分类（可重试 / 不可重试）一起覆盖。
四个用例之间是**互相兜住**的关系：只断言「重复投递被跳过」的话，
"永远跳过"这种退化实现也能通过 —— 必须同时断言
「不同 attempt 要各自渲染」「失败后必须释放占位」「UNIMPLEMENTED 要释放占位且不重试」。

**B5 —— 断开 Redis 后队列中的任务在恢复后继续执行**。
用测试自己拉起的私有 `redis-server`（独立端口、独立目录）做真实故障注入，
用 `SIGKILL` 模拟进程崩溃而**不是**优雅关闭：

| 用例 | 故障形态 | 靠什么恢复 |
| --- | --- | --- |
| `TestB5QueuedTasksSurviveRedisRestart` | 任务**还在排队**、尚未被取走时 Redis 崩溃 | AOF 持久化原样恢复（先断言任务真的还在，再断言全部被执行） |
| `TestB5InFlightTaskRecoveredAfterRedisRestart` | 任务**正在执行**时 Redis 崩溃 | Asynq 租约过期 → recoverer 重投 |

后一项受 Asynq 硬编码的时间参数所限（租约 30s、recoverer 轮询 60s、过期余量 30s），
需要 2~4 分钟，因此**默认跳过**，用 `make test-failover` 显式运行。
长耗时用例留在默认目标里，最后一定会被整体 skip 掉，等于没写。

> 两个坑记在这里，免得下次重复踩：
> **① 断线必须持续够久。** 第一版用例断开不到 1 秒就把 Redis 拉回来，
> 结果下一次心跳（15s 一次）把租约续上了，正在执行的任务根本没被打断、
> 正常跑完 —— 测试看着"恢复了"，但 recoverer 从头到尾没参与，等于什么都没验证。
> **② 崩溃与优雅关闭不等价。** 优雅关闭会让 Redis 主动落盘，
> 从而掩盖 `appendfsync everysec` 这个策略到底够不够用；必须 `SIGKILL`。
> 相应地，杀进程前要等过一个完整的 fsync 周期（1.5s），
> 否则偶发失败的现象看起来像"持久化没生效"，会把排查方向带偏。
>
> **本机怎么跑 B5**：用例需要一个 `redis-server` 可执行文件，默认从 `PATH` 查找；
> 若 Redis 是解压出来的、不在 `PATH` 上，用环境变量显式指定：
>
> ```bash
> SCID_TEST_REDIS_BIN=/path/to/redis-server make test-failover
> ```
>
> 找不到时用例会 **SKIP 而不是失败**——这与 B4 的跳过一样，构成「看起来全绿但其实没跑到」
> 的坑。所以判断 B5 到底跑没跑，要看 `go test -v` 里的 `--- SKIP` 或 SKIP 计数，
> 不能只看 `ok` 那一行。

### s3 归档路径的验证方式

此前「s3 路径未经端到端验证」的唯一原因是**没有对象存储实例**。补验证时把两半都真跑了一遍，
因为**两半各自通过不等于接起来通过**：

| 层 | 用例 | 验证什么 |
| --- | --- | --- |
| 归档器本身 | `archive/s3_e2e_test.go`（`TestS3*`） | 按需建桶、上传后用**独立客户端**读回并逐字节比对、Content-Type、敌意文件名的键清洗在真实存储里的落点、端点不可达时如实报错 |
| 接进流程 | `worker/archive_test.go`（`TestFinalizeArtifacts*`） | 归档成功才清理本地、**归档失败一个本地文件都不删**、未配置归档时清理照常生效、真实归档器 + 真实 `finalizeArtifacts` 的联合路径 |

跑法（端点缺失时自动 SKIP，CI 无对象存储不会变红）：

```bash
SCID_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
SCID_TEST_S3_ACCESS_KEY=... SCID_TEST_S3_SECRET_KEY=... make test-s3
```

用例只依赖 **S3 API**、不绑定具体产品，因此 RustFS / MinIO / SeaweedFS / moto 都能跑。

**验证结果（两轮）**：

1. 第一轮跑在 **moto**（一个 S3 API 实现）上——先把代码路径跑通，此时结论只到「对 S3 API 正确」。
2. 第二轮跑在**真实 RustFS 容器**上（`docker compose up -d rustfs`，`rustfs/rustfs:latest`），
   全部用例通过。至此「生产将用的对象存储」这一层才算真的补上。

```bash
docker compose up -d redis postgres rustfs
SCID_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
SCID_TEST_S3_ACCESS_KEY=scidirector SCID_TEST_S3_SECRET_KEY=scidirector-secret make test-s3
```

> **踩坑三处**（前两处是用例自身的错，第三处是配置缺陷，但都会让人误判）：
> **① `minio-go` 的 `ListObjects` 默认不递归**，按 `/` 返回**公共前缀**
> （拿到的是 `jobs/etc/` 这样的目录条目）。用它断言「对象键有没有落在前缀下」，
> 断言的是空气 —— 必须显式 `Recursive: true`。
> **② 模拟器（moto）不校验凭据**，所以「用错误密钥必须失败」这种断言会**在模拟器上不成立**。
> 想钉住「错误不能被吞掉」，应该用**端点不可达**来构造失败 —— 那是任何实现下都必须失败的情形。
> **③ 环境变量名曾经是断的**：`.env.example` 与 compose 的公共块写的是 `SCID_S3_*`，
> 而 Go 侧实际只读 `SCID_MINIO_*` —— **没有任何代码读 `SCID_S3_*`**。
> 照着模板配 S3 的人会设一堆完全不起作用的变量，而「端点是空串」与「压根没配置」
> 在归档路径上无法区分。现已统一为 `SCID_S3_*`，`SCID_MINIO_*` 保留为兼容回退
> （新名优先），并补了 `backend/internal/config/config_test.go` 钉住迁移语义 ——
> 该包此前**一个测试都没有**。

#### 对象存储选型：MinIO → RustFS

原来的计划是「用 MinIO 验证」，但 `dl.min.io` 现在直接返回 **410 Gone**，页面原文写明：
MinIO Server / Client / KES 的开源版本**已归档、不再维护**，不再提供安全更新，
且不再从该站分发。`minio/minio` 在 GitHub 上最后一个 release（`RELEASE.2025-10-15`）
**没有任何可下载产物**。一个不再有安全更新的组件不适合作为产线依赖。

**已改动**（经用户确认选型）：

- compose 的 `minio` 服务换成 **`rustfs/rustfs:latest`**（同为 S3 兼容、仍在活跃维护，
  端口布局一致：9000 = S3 API、9001 = 控制台）。
- **删掉 `minio-init` 容器**。它用 `minio/mc`（同样已归档）建桶并设匿名读；
  而建桶本来就由代码负责 —— `S3Archiver.ensureBucket` 在首次上传时按需创建，
  这条路径正是 `s3_e2e_test.go` 覆盖的内容。少一个容器、少一个已归档镜像依赖，
  也少一处「init 跑失败但没人看」的隐患。
- 卷名 `minio-data` → `rustfs-data`（旧的本地卷不再被挂载，需要时自行迁移）。

> 顺带修掉两个只有真起容器才会暴露的问题：RustFS 镜像**没有 bash**
> （有 sh/curl/wget/nc），所以健康检查不能用 `</dev/tcp/...` 那种 bash 写法 ——
> 那会让容器永远停在 `starting`；改用 RustFS 提供的 MinIO 同名健康端点
> `/minio/health/live`。另外 compose 默认把 Postgres 发布到宿主 5432，
> 而本机 5432 已被宿主自带的 Postgres 占用（`address already in use`），
> 用 `SCID_PG_PORT` 让开即可。

### 配音链路：已完成的那一半（与 TTS 服务商无关）

「全片 TTS」要分两半看：**产生音频**（依赖服务商）与**消费音频**（不依赖）。
后者已完成并验证，因此选型定下来之后只剩「产生音频」一步要写。

| 环节 | 实现 | 语义 |
| --- | --- | --- |
| 探测配音时长 | `Runner.ProbeAudio` | 只读 `format.duration`；**不复用 `Probe`**，后者是产物校验器、要求存在视频流 |
| 拼整片配音轨 | `Runner.BuildNarrationTrack` | 逐镜头对齐到各自画面时长（短则补静音、长则截断）后 concat |
| 字幕按真实时长排布 | `PlanCuesWithNarration` | cue 只覆盖配音那一段，**末尾留白不再被字幕占满** |
| 接入成片 | 合成路径把 `narration.m4a` 传给 `MuxFinal` | 原调用契约不变（此前传空串） |

三条必须说清的语义：

1. **画面比旁白长** → 字幕在旁白结束处收尾。旧行为会按窗口铺满，把最后一句
   **拉伸着挂到画面结束**：声音早停了、观众早读完了，字幕还在。
2. **旁白比画面长** → 字幕夹到窗口末尾（不能侵占下一个镜头），
   同时这是一个信号：**该镜头的画面需要加长**。
3. **没有配音的镜头** → 回退成铺满窗口（即旧行为），且整轨里补**等长静音**。
   跳过它会让整轨比画面短，其后每个镜头的声音整体前移 ——
   「某段没声音」是小瑕疵，「全片音画错位」是废片。

验证（都**不依赖 TTS 服务商**，用合成的正弦音当配音）：

- `media` 包：4 项纯函数用例（末尾留白 / 越界夹取 / 部分镜头有配音 / 极短配音合并保内容）
  + 4 项真实 ffmpeg 用例（补静音 / 截断超长 / 全静音干净 / 输入校验），
  其中用 `volumedetect` 证明确实有音频被拼了进去，而不是整轨静音。
- `worker` 包：1 项**端到端**用例 —— 真片段 + 真配音 → 真 `HandleComposeJob` →
  断言成片有音轨、时长等于两段画面之和、镜头 0 的旁白只有 1.2s 时字幕确实在 1.2s 收尾、
  而**没有配音的镜头 1 仍铺满窗口**（证明新逻辑没有误伤回退路径）。

> **两个坑**：
> **① `Runner.Probe` 会拒绝纯音频文件**（`未找到有效的视频流`）。
> 那条校验对镜头片段是对的、不该放宽，所以配音时长另走一条探测路径 ——
> 第一版用例就是因为复用了 `Probe` 而失败的。
> **② 没配音的镜头必须补静音而不是跳过**，理由见上表第 3 条。

### 语音合成：四家服务商的适配层

按用户要求支持四家：**Edge TTS / 豆包（火山引擎）/ OpenAI / Fish Audio**。
适配层在 `ai/scidirector_ai/tts/`，统一接口 + 按需导入（用 Edge 的人不必装豆包/OpenAI 的 SDK）。

| 服务商 | 鉴权 | 时间戳 | 本机验证状态 |
| --- | --- | --- | --- |
| **edge** | 无需密钥 | **句级**（实测 `SentenceBoundary` 带 offset/duration） | ✅ **真实验证**：三句话拿到 3 条时间戳，音频 7.15s，sidecar 落盘并回读 |
| **doubao** | appid + access token | 未确认（无依据，故不臆造） | ⚠️ **未验证**：无凭据，且官方文档页是 JS 壳、抓不到正文 |
| **openai** | API key | **无**（`audio.speech` 不返回时间戳） | ⚠️ **未验证**：端点在本机不可达 |
| **fish** | Bearer key | 另有 `/v1/tts/stream/with-timestamp`（SSE），但可获取的规范**未定义事件结构**，故暂未实现 | ⚠️ **未验证**：端点在本机不可达 |

**Edge TTS 是本机唯一能端到端验证的一家**，也是缺省推荐：免费、无需密钥、给句级时间戳。
代价是**非官方接口**，无 SLA、属 ToS 灰区 —— 产线要稳定性应换 Azure 官方（同一引擎、带密钥）。

时间戳通过**音频旁边的 sidecar JSON**（`<音频>.marks.json`，带版本号）交给 Go：
与 `payload_json` 当初的取舍一致 —— 结构还在演进，走 JSON 不必每次重生成两侧代码；
而且它是**可选增强**，没有 sidecar 时 Go 回退到「按镜头真实音频时长对齐」（已实现并验证）。

> **「未验证」是字面意思**：豆包/OpenAI/Fish 三家只覆盖到「请求形状与错误分类」这一层
> （用 mock 传输测的），**没有对真实服务调用过**。豆包适配器里唯一可能与现实不符的是
> `build_payload` 的字段名（鉴权头 `Bearer;<token>`、成功码 3000、base64 音频这些是稳定的）；
> 端点、cluster、成功码均已做成可配置，真实形状不符时不必改代码。
> **首次接入这三家时请先核对官方文档，并补一次真实调用验证。**

### 已接进流水线，并用真实 Edge TTS 端到端验证

渲染节点现在会对每个镜头合成配音（失败**只降级不抛出**：画面才是主体，
没有旁白的镜头仍是可用产物；但一定留 warning，因为「成片没声音」是可见的质量差异）。
缺省 `SCID_TTS_PROVIDER` 为空 ⇒ 不合成、不产生任何额外文件，与接入前逐字节一致。

**实测一条真实任务**（`job-d71b2f4265d6b760`，Edge TTS，中文）：

| 验证点 | 结果 |
| --- | --- |
| 镜头产物 | `audio_path` 指向真实 Edge TTS 音频（3.768s / 4.344s） |
| 整片配音轨 | `narration.m4a` 由两段镜头配音拼成（120KB） |
| 成片真的有声音 | 成片音轨 **-22.5 dB**（配音轨 -23.1 dB）—— 不是静音 |
| 字幕跟着真实配音走 | 第二条 cue 结束于 **7.944s** = 3.6 + 4.344（该镜头配音时长），**而不是窗口末尾 8.5s** |
| 任务终态 | `PARTIAL`（两个无产物的镜头被放行 → 如实标记，未伪装成 COMPLETED） |

最后一行是这整套改动的意义：**声音停了字幕就停**，末尾留白不再被字幕占满。

> **一个只有端到端才会暴露的坑**：ai 服务跑在 `ai/`、Go worker 跑在 `backend/`，
> 而 Python 侧最初把 `audio_path` 报成了**相对路径**（`.data/sandbox/...`）。
> 到了 worker 那边就是「文件不存在」，而 worker **只降级不报错** ——
> 成片照出、只是没有声音，极易被误判成「TTS 没配好」。
> 修在写入方（`write_audio` 统一 resolve 并返回绝对路径），四个适配器一起受保护，
> 并补了两条回归用例（音频路径与 sidecar 路径都必须是绝对路径）。
> 本项目 §9 早就记过同源的坑（「传给子进程的路径必须 `.resolve()`」），这是它在**跨进程**场景下的变体。

### 句级时间戳对齐（Go 侧已消费）

sidecar 写出来之后，Go 侧现在会读它并按**真实句子起止**排字幕：

| 环节 | 位置 | 语义 |
| --- | --- | --- |
| 读 sidecar | `media.ReadNarrationMarks` | 尽力而为：没有 → `(nil,nil)` 静默回退；**有但用不了 → 返回 error**，调用方记 warn（那说明两侧契约对不上） |
| 按句锚定 | `media.PlanCuesWithMarks` | 句数与时间戳条数一致时，每句落在它**真实被念出来**的那段时间里；对不上就退回比例分配 |
| 接线 | `worker.HandleComposeJob` | 逐镜头读 marks，命中时记 `使用句级时间戳对齐字幕` |

**为什么值得做**：按镜头时长比例分配时，句与句之间的**停顿会被均摊掉** ——
语音停了一秒，字幕却仍在匀速推进，越往后越偏。句级时间戳把这个误差从
「整镜累积」压到「句内可忽略」。用例 `TestPlanCuesWithMarksKeepsPauseBetweenSentences`
就钉住了这一点：两句之间停 1.5 秒时，第二条字幕必须在 **3.5s** 出现，
而不是比例分配算出来的约 2.75s。

刻意**不做** `mergeToAtMost`：句级时间戳来自真实语音，合并等于丢掉刚拿到的精度。
代价是极短的句子会一闪而过 —— 那本来就是语音的真实形态，不该由字幕粉饰。

> **跨语言契约必须用真实样本测**：`narration_marks_test.go` 里的 fixture 是
> **Python 侧真实产出**的 sidecar 内容（未经手工修改）。自己编一份「看起来对」的 JSON
> 只能证明我们和自己的想象一致 —— 而契约坏掉时的表现是**静默降级**
> （字幕退回按时长对齐、成片照样出），不会有任何报错。

**真实任务实测**（`job-b754208870947990`，Edge TTS）：两个镜头都命中句级时间戳
（`使用句级时间戳对齐字幕 shot=0 marks=1` / `shot=3 marks=1`），字幕轴与 sidecar 逐项对上：

| | 字幕 cue | sidecar |
| --- | --- | --- |
| 镜头 0 | `00:00:00,100 → 00:00:03,600` | start=0.100, end=3.7625 |
| 镜头 3 | `00:00:03,700 → 00:00:07,938` | 3.6+0.100, 3.6+4.3375=**7.9375** |

注意两条 cue 的**起点都是 0.100**（而不是 0.000）—— 那就是句级时间戳里的 start_sec。
成片音轨 -22.5 dB（确有声音）。

> **另一个只有真跑才会暴露的问题：TTS 的瞬时失败必须重试。**
> 实测本机访问 Edge TTS 会出现「第一次成功、紧接着连接被 reset」：
>
> ```
> [0] OK   2.32s
> [1] FAIL Connection reset by peer (retryable)
> [2] FAIL Connection reset by peer (retryable)
> ```
>
> 而流水线原先**只试一次**，于是第一个镜头直接失去配音 —— 更糟的是
> 「失去配音」只降级不报错，成片出来是「莫名没声音」，排查方向完全跑偏。
> 现加 `synthesize_with_retry`（只重试**可重试**错误、指数退避带**抖动**、次数有限），
> 四个适配器共用。修复后同一条流水线上两个镜头都拿到了配音。
> **一般化的教训：网络调用 + 只降级不报错 = 静默失去功能。**
> 降级本身是对的（画面是主体），但降级之前必须先重试几次。

**已无遗留**：这条链路（合成 → 重试 → 逐镜头对齐 → 整轨 → 字幕按真实句子起止）完整且被验证。

### 已完成部分：并发收敛 + 转场 + 统一规格

**并发模型**（两道防线是乘法关系，不是重复）：

| 防线 | 作用域 | 防的是什么 |
| --- | --- | --- |
| `media.Pool`（`errgroup.SetLimit`） | 单任务内 | goroutine 爆炸 —— 内存 O(limit) 而非 O(分镜数) |
| `Runner.sem` | **整个 worker 进程**（单例） | 真正的 OOM —— ffmpeg 子进程总数上限 |

同时加固了外部进程生命周期：`WaitDelay` 兜住「孙进程占用管道导致 `Wait` 永久阻塞」、
自定义 `Cancel` 杀**整棵进程树**（POSIX `killpg` / Windows `taskkill /T`）、
单条命令硬超时（`SCID_FFMPEG_CMD_TIMEOUT`，防止卡死进程永久占住槽位）。

**转场**：`PlanTransitions` 是**纯函数**，因此 offset 数学可被单测锁死。
关键点是 offset 必须基于**累积游标**而不是前一片段时长 ——
两种写法在第 1 个连接点结果相同、从第 2 个开始分道扬镳，
而 ffmpeg 对错误的 offset 多数情况下**不报错**，只产出转场位置漂移的成片。
音频用同样的时长做 `acrossfade`，否则每过一个转场音轨就比画面多 T 秒。
转场无法 `-c copy`（必须重编码），因此默认走判定 → 硬切/转场两条路径，
并把「为什么没启用转场」的原因写进日志与事件。

**统一规格**：不只是统一分辨率/帧率，还显式**转换并打标**色彩范围与色彩空间。
「Manim 与浏览器录制质感割裂」的首要原因是色彩范围不匹配
（矢量输出有限范围 vs 逐帧截图全范围），只统一分辨率修不好这个问题。

### 验收标准

| 编号 | 验收项 | 判定方式 |
| --- | --- | --- |
| B1 | 4 个分镜并发归一化，总耗时显著低于串行 | 对比日志时间戳 |
| B2 | 不同分辨率/帧率的片段能正确 concat，无花屏、无音画不同步 | 人工播放 + ffprobe 校验 |
| B3 | 任务取消时所有 ffmpeg 进程被杀死，无僵尸进程 | 取消任务后检查进程表 |
| B4 | 同一 `(job, shot, attempt)` 重复投递只渲染一次 | 检查幂等键日志 |
| B5 | 断开 Redis 后，队列中的任务在恢复后继续执行 | 手动故障注入 |

---

## 阶段四 · 反馈闭环与前端 ✅ 已完成

1. **React 审核台**：分镜表、视频预览、事件时间线、实时进度条 — ✅
2. **打回重做**：可视化填写意见，走 `reject` 接口或 WebSocket `feedback` — ✅
3. **断线重连**：用 `last_event_id` 自动补齐，不丢事件、进度不回跳 — ✅
   （重连以**快照**为基线 + seq 去重 + 空洞检测触发 `resync`）
4. **人工放行**：熔断镜头的 approve 出口 — ✅
5. **成分镜编辑**：直接修改分镜的 `narration` / `visual_brief` 后重做 — ✅
   （`PATCH /api/v1/jobs/:id/shots/:id`）

### 验收标准

| 编号 | 验收项 | 结果 |
| --- | --- | --- |
| C1 | 提交脚本后，前端实时看到分镜逐个出现并推进状态 | ✅ **真实浏览器驱动真实全栈**（`scripts/smoke-web.py`，`make smoke-web`）：分镜表从空到出现、各镜头状态发生推进、进度单调不回退。产物含截图，供**人工目视**确认观感那部分 |
| C2 | 刷新页面不丢上下文（快照 + 历史重放生效） | ✅ 快照为**权威基线**（整体替换而非与本地增量合并），含历史事件；`applySnapshot` 有单测 |
| C3 | 断网 10 秒后恢复，进度自动补齐且不回跳 | ✅ **真实切断实时通道 10 秒后恢复**：连接指示器 `实时 → 重连中… → 实时`，恢复后进度不回跳，且页面**收敛到服务端真值**（统计一致）。见下方「C1/C3 的验证方式」 |
| C4 | 打回一个镜头后**只有它**重新渲染，其余镜头进度不变 | ✅ **真实服务验证**：打回 #0 后仅 #0 的 attempt 1→2，其余三个纹丝不动 |
| C5 | 熔断镜头可人工放行，任务继续合成 | ✅ **真实服务验证**：放行最后一个熔断镜头 → `compose_enqueued:true` → worker 合成 → `COMPLETED`，成片含 h264(tv)/aac/mov_text 三路流 |

### 联调中发现并修复的问题

| # | 问题 | 后果 | 修复 |
| --- | --- | --- | --- |
| 1 | `HandleApproveShot` 只改状态、**没有入队合成任务** | 最后一个熔断镜头放行后任务**永远停在 COMPOSING**，前端看着 100% 却等不到成片，且无任何错误可报 | 「全部通过」时主动 `EnqueueComposeJob` 并补一条事件 |
| 2 | 前端没有拆响应信封 | 成功响应是 `{"ok":true,"data":{...}}`，直接 `as T` 会让字段全变 `undefined` —— 页面**不报错**，只是静静地什么都不显示 | `request()` 统一拆信封；新增 Go 侧「响应形状契约」测试钉死两层包装 |
| 3 | `applyEvents(state, [])` 把 job 清成 `null` | 一次心跳、一次空补发就足以清空整个页面 | 空批次路径透传 `state.job` 而非传入的 `nil` |
| 4 | 人工放行了**无产物**的镜头后，成片静默缺镜头却标记 `COMPLETED` | 用户拿到一部少了几段的片子，界面显示一切正常 —— 比直接失败更危险 | 有缺失镜头时如实标为 `PARTIAL` 并发说明事件 |
| 5 | `CheckOrigin` / CORS 把 `*` 当成普通字面来源比对 | 配 `SCID_CORS_ALLOWED_ORIGINS=*`（最自然的「全放开」写法）时**每一次** WebSocket 升级都 403，实时通道整个是死的；而 REST 轮询仍在更新页面，看起来只是「有点卡」 | 两处都显式把 `*` 识别为放开；`internal/ws` 此前**零测试**，补了 4 项来源校验用例，CORS 补了 4 项 |
| 6 | `onNeedJobRefresh` 只在**断线时**触发，实时事件不触发 | 实时推送不含分镜明细，而快照是在「刚提交、还没拆解」那一刻生成的 ⇒ 分镜表**永远空着**，连接指示器却一路显示「实时」 | 事件到达时按 300ms 节流回源。讽刺的是第 5 条（WS 全 403）会让 `onclose` 反复回源，从而**掩盖**这一条 |
| 7 | 回源只合并 `job`，丢掉同一信封里的 `stat` / `progress` | `deriveStat` 优先用服务端 `stat`，而 `stat` 只在快照里到达过（那时 `total=0`）⇒ 界面长期显示「表格 4 行、统计写着共 0 个分镜、进度 0%」，正是 `stream.ts` 注释里警告过的那种自相矛盾界面 | 新增 `applyEventsWithDetail`，三样一起合并 |
| 8 | 文档/compose 里的 S3 端点写成 `http://host:9000`，而 `minio-go` **不接受带 scheme 的 Endpoint** | **worker 直接启动失败**（`Endpoint url cannot have fully qualified paths` ⇒「归档配置非法」）—— 也就是**照着文档配置就无法启动服务** | `splitEndpoint` 归一化两种写法并按 scheme 推断 TLS；补 2 项用例钉住 |

阶段四主要产物：`web/src/{stream,useJobStream,api,display,types}.ts`、
`web/src/components/{ShotRow,EventTimeline}.tsx`、`backend/internal/httpapi/hitl_test.go`。

### C1/C3 的验证方式

C1/C3 此前一直标着「浏览器人工目视验证未做」——而它们恰恰是最容易在单测里漏掉的：
C2/C3 的全部逻辑都在前端归约（`stream.ts`）里，出问题的表现是「偶尔少一条事件」
「进度条退一格」，靠点页面几乎无法稳定复现。补验证时**第一次用真实浏览器驱动真实全栈**，
立刻暴露出上表的第 5、6、7 条 —— 三条都是「页面看起来在工作」的静默故障。

`scripts/smoke-web.py`（`make smoke-web`）负责**可自动化的那一半**：

| 断言 | 做法 |
| --- | --- |
| 分镜出现 + 状态推进 | 轮询页面，断言分镜条数从 0 变为 N、至少一个镜头经历过两种以上状态 |
| 进度单调 | 全程采样，断言进度序列不回退 |
| 断网后**补齐** | 真切断实时通道 10 秒再恢复，断言页面收敛到服务端真值（统计逐项一致） |
| 断网后**不回跳** | 断言恢复后的进度不低于断线前 |
| 布局自洽（C1 结束时 + C3 断网中） | 页面无横向溢出、关键元素不越界、分镜行有可见高度且互不重叠、长文本未被裁切、进度条宽度与统计百分比一致、统计的分镜数与表格行数一致 |

> **两个必须写下来的坑**：
> **① `context.set_offline(True)` 不会断开已经建立的 WebSocket。**
> 实测断网期间连接指示器仍是「实时」，演练等于没做。脚本因此支持
> `--api-restart-cmd`，用「停掉 api 10 秒再拉起」真正切断实时通道 ——
> 这同时覆盖了代码里明确设计过的「服务端重启」场景。
> 未提供该参数时脚本**如实报告**「本次演练并未真正断线」，而不是假装通过。
> **② 截图只证明观感，断言只证明通路，两者不可互相替代。**
> 脚本产出截图供人工目视；自动化断言通过 ≠ 画面好看、布局正确。
>
> **③ 布局检查器也必须带负向对照。** 上面那组布局断言如果选择器写错，
> 就会「什么都没检查到却报通过」—— 一个永远返回「没问题」的检查器
> 比没有检查更糟，因为它会让人以为布局已经验证过了（与「探测谎报可用」同类）。
> 因此脚本在 C1 结束后**故意注入一个 2px 高的分镜行**，确认检查器确实会报，
> 再移除它并确认恢复「无问题」。另外检查器自带一致性校验：
> 统计文字说「共 N 个分镜」时，表格行数必须等于 N —— 这条既是真缺陷的检查，
> 也顺带证明选择器确实选到了元素。

#### 引擎就绪探测曾「谎报可用」（已修复）

联调时装上 `playwright`（只有 Python 包、没装浏览器二进制）之后，
`/healthz` 的 `engine:d3=ok`、`engine:echarts=ok`、`engine:code_anim=ok` 全部变成可用，
但实际渲染仍然失败。

根因在**两个地方**，且健康检查走的是后者：

| 位置 | 原判定依据 | 问题 |
| --- | --- | --- |
| `config.toolchain_report()` | `__import__("playwright")` 成功即算可用 | **健康检查读的是这个**，于是 `engine_availability` 认为三个 HTML 引擎都就绪 |
| `renderer.HtmlRenderer.available()` | 同上，只 import 不检查二进制 | 渲染前的自检也会放行 |

Python 包与浏览器二进制是**两步**安装（`pip install playwright` +
`playwright install chromium`），只做完第一步就会谎报可用。
这与阶段二「`Health.engines` 让编排层提前知道哪些标签不可渲染」的设计意图直接冲突：
一个会说谎的探测比没有探测更糟 —— 它把「环境没准备好」伪装成「内容不达标」。

**已修复**：新增唯一一份探测 `config.browser_ready()`（真正解析
`pw.chromium.executable_path` 并确认其存在，带缓存避免健康检查反复起子进程），
`toolchain_report()` 与 `HtmlRenderer.available()` **共用**它 ——
两边各写一份迟早会各说各话。修复后本机 `/healthz` 如实报告
`d3/echarts/code_anim = False, stock = True`（本就只装了 ffmpeg）。

> **一处需要更正的说法。** 上面这版文字最初把「DATA 镜头烧满 3 次 attempt」
> 也算在这个探测头上（`#2 DATA attempt=3 → AWAITING_HUMAN`）。**这是错的。**
> 复核后：那 3 次来自**编码智能体的契约修复循环** —— `check_html_contract`
> 是在 `agents/coder.py` 生成代码时调用（第 301 行）的，而不是在渲染时；
> mock LLM 产不出带 `window.__seek` 的 HTML，于是按设计重写 3 次后熔断（阶段二 A7 的行为）。
> 也就是说：**探测说谎确实存在且已修，但「它导致 3 次 attempt」是我把两个现象
> 归因到一起了。** 修复后重跑同一个任务，`#2 DATA` 仍然是 `attempt=3` ——
> 因为那条路径与引擎可用性无关。把它记在这里，是因为**错误的归因比没有归因更危险**：
> 它会让人以为「修好探测就不会浪费 attempt」。


---

## 阶段五 · 生产加固 ⏳ 待办

- **可观测性**：✅ OpenTelemetry 链路追踪（Go span ↔ gRPC ↔ Python span 串联）、Prometheus 指标、Grafana 看板
- **成本核算**：✅ 按任务统计 token 与渲染时长；**配额已随多租户一起做**（按租户限制在跑任务数）
- **多租户**：✅ 任务归属与隔离（含按租户配额，见下）
- **状态对账**：✅ 定期比对 + 自动修一类（见下）
- **沙盒加固**：🟡 网络隔离（`unshare -r -n`）与**只读根文件系统**（`unshare -r -m`）已做；**seccomp 白名单未做**（本机有 `libseccomp.so.2`，可行）

### 可观测性：做到哪一步、证据是什么

验收标准是「一次生成请求能在 Grafana 上看到完整的 span 树与耗时分解」。
这句话的主语是 **Grafana**，所以最终的判定必须在**浏览器**里做，而不是查 Tempo 的 HTTP API ——
两者的差别是真实存在的：现代 Grafana 里 Tempo 的 TraceQL 搜索由**前端插件**在浏览器里
直接打数据源代理执行，不走 `/api/ds/query`（那里只认 `traceId` 等少数后端查询类型）。
我一开始正是拿 `/api/ds/query` 去试，得到 `unsupported query type: 'traceql'`，差点
得出「配置坏了」的错误结论。

**实现的骨架**

| 环节 | 做法 | 为什么这么做 |
| --- | --- | --- |
| Go 服务端 span | `httpapi.TraceMiddleware` **自己**承担，不叠 otelgin | 项目已有 `tr-` 前缀的 trace_id 语义（X-Request-ID、日志字段）。再加一层自动埋点就会出现**两个都叫 trace_id 的东西**，于是「拿日志里的 ID 查链路」必然失效 —— 而这类不一致不会报错，只会让人以为链路没采到 |
| 跨队列传播 | 入队时把完整 `traceparent` 存进载荷，**收口在 queue client** | worker 与 api 是两个进程、中间隔着 Redis，Asynq 不传递任何上下文。入队点有六处，逐个去记得填一定会漏，而漏掉的表现是「链路后半段没了」 |
| 跨语言传播 | `otelgrpc` 写 metadata，Python 用官方 `GrpcInstrumentorServer` 解 | 都用 W3C `traceparent`，不需要任何自定义协议 |
| Python 图节点 | `traced_node` 装饰器，span 名 = 事件流里的 `node` 字段 | 两套视图（事件流与链路）能直接对上，不必维护映射表 |
| 日志与链路对齐 | 日志的 `trace_id` 回退到当前 OTel span 的 trace ID | 保证是**同一个值**，否则「从日志跳到链路」不成立 |
| 观测栈 | `--profile observability` 起 collector → Tempo + Prometheus → Grafana，数据源与看板**全部 provisioning** | 看板是验收对象，必须能被一条 `docker compose` 复现；手工点出来的配置在别人机器上复现不出来 |

**证据一：一次真实请求的完整链路**（`job-f10e3761b20a8165`，本机全真实依赖）

同一棵树上有 25 个 span，覆盖三个服务：

    scid-api        /api/v1/generate                          5.12 ms
    scid-worker     consume generate                          16.6 s
    scid-worker     scidirector.v1.AiDirectorService/RunPipeline  16.5 s
    scidirector-ai  /scidirector.v1.AiDirectorService/RunPipeline 16.5 s
    scidirector-ai  plan / code / render / critique / advance / revise …

worker 的消费 span 上带 `scidirector.trace_continued=true`。这个标记是刻意加的：
链路断掉时，两侧各自都有**完整可看**的 trace，只是不在一棵树上 —— 没有报错、没有失败，
只有这个布尔值能让「断链」一眼可见。本轮就是靠它定位到自己写错的地方（见下）。

**证据二：浏览器里的 Grafana**（`scripts/verify-observability.py`）

用真实 Chromium 驱动 Grafana，断言的是 **DOM 文本**而不是像素（模型读不了图；
截图另存 `.data/obs-shots/` 供人复核）：

- 看板页渲染出标题，且链路表格真的有 Trace ID 行（只断言标题的话，一个全是空面板的看板也能通过）；
- 链路视图里同时出现 **三个服务名**、跨服务 span 名（`/api/v1/generate`、`consume generate`）
  与**耗时数值**。

**本轮踩到的坑（同一类：不报错，只是查不到）**

1. **带 span 的 ctx 装回请求的时机**。必须放在 `c.Next()` **之前** —— 我第一版放在之后，
   handler 里拿到的仍是没有 span 的上下文，于是入队 traceparent 为空、worker 自成一根。
   这个错误格外隐蔽：两侧都有完整链路，只是不在一棵树上。
2. **`/metrics` 不能建 span**。Prometheus 每 5 秒抓一次，它会把 Grafana 表格的 limit 占满，
   让真正要看的跨服务 span **挤不进来**，看起来就像断链 —— 我因此误判过一次。
3. Explore 链接格式必须**照抄 Grafana 自己生成的**：少了 query 内部的 `datasource`
   会静默退化成「没有选中数据源」的空白编辑器（不报错，只是什么都不显示）。
4. Tempo 的 `Spans Limit` 默认只有 3，会把跨服务的那一段截掉。
5. Tempo 的 search 返回**去掉前导零**的 32 位 trace ID，字符串比对会失败，要补零。
6. 容器读挂载配置报 `permission denied` 是**文件权限**（工具写出的文件可能是 0600）。
7. 观测栈的 `depends_on` 不该指向需要**构建**的服务，否则会逼 compose 去构建 api 镜像。
8. 本机 9090 已被 Clash 占用 —— 端口一律要可覆盖。

**没验证的部分**

- **未在 compose 内跑端到端的应用容器**：本机的 Go/Python 进程直接跑在宿主上
  （api 容器需要 `golang:1.23-alpine` 基础镜像，本网络下不可达）。因此
  Prometheus 的 `api:8080` 目标在本机是 **down**（预期），验证走的是 `host.docker.internal:8080`
  这个并列的 job。两个 job 都写在提交的抓取配置里，不是本地临时改动。
- **Grafana 的嵌套瀑布图（trace view）未做自动化断言**：验证到的是「跨服务 span 表 + 耗时」。
  从表格点进单条 trace 的瀑布图需要额外的 UI 交互（Grafana 11 的 Explore 用 Monaco 编辑器 +
  懒加载表格），本轮没有把它做稳；截图存档供人工点开复核。
- **指标侧只验到了「被抓到且有真实数据」**（`go_goroutines`、`process_resident_memory_bytes`、
  `up=1`，以及 Grafana 经 Prometheus 数据源查询成功）。**业务指标未做**：
  目前没有把「任务数 / 一次通过率 / 渲染时长分布」导出为 Prometheus 指标，
  这些信息在链路与成本接口里可得，但 Grafana 上还看不到趋势图。
- **采样率、Tempo 保留期、Grafana 匿名登录都是开发档配置**：生产要改成有限采样、
  接对象存储、打开认证（追踪数据里有任务脚本与内部拓扑）。

### 成本核算：已完成的部分与口径

验收标准是「能查询任意历史任务的 token 成本与渲染成本」。现已落地：
`GET /api/v1/jobs/:jobID/cost`，且**任务详情里也带** `cost`（免得前端为一个数字多发一次请求）。

口径的取舍（这几条比实现本身更重要）：

| 决策 | 原因 |
| --- | --- |
| 报**用量**不报金额 | 换算成钱要单价表，而单价随服务商/模型/时段变。写死在代码里的单价是一个「看起来精确但已过时」的数字；要金额就在上层按可配置单价换算 |
| `llm.*` 由 Python 上报并持久化 | 只有 Python 持有 LLM 客户端，Go 推不出来 |
| `render_sec` / `tts_chars` 由 Go **读取时现算** | 它们能从任务状态推导（产物自带 `render_cost_sec`；有 `audio_path` 的镜头就是合成过配音的那几个）。存起来就要同步，而「两处口径不一致」在成本数字上没有报错、没有告警，只是一个数字悄悄偏了 |
| 只统计**真的产出配音**的镜头 | 否则 TTS 整体失败时成本看起来照样正常，正好掩盖了真正的问题 |
| 中文按**字符**计而非字节 | 一个汉字 3 字节，用字节数会把 TTS 成本算成三倍 |

为把「谁能写进存储」变成编译期约束，用了**两个类型**：`domain.LLMUsage`（上报、持久化）
与 `domain.Cost`（面向调用方的快照）。合成一个「部分字段有值」的类型，
就等于给「某处顺手把推导值写回存储」留了门。

**验证方式与证据**

- 单测（`domain/cost_test.go` 9 项）：含**反向控制** —— 没配到音的镜头不计字数、
  无产物时推导项为 0、重复上报是覆盖而非累加（断点续跑会重放 final 事件）。
- 跨语言契约（`pbconv/cost_json_test.go` 4 项 + `ai/tests/test_final_event_cost.py` 4 项）：
  两侧各自用**照抄对方格式**的字面量断言键名。Go 侧验证解析（坏 JSON 必须让事件照常迁移
  并留下 `raw_parse_error`，而不是静默变成 0），Python 侧验证产出（键名逐字钉住、
  值必须是 JSON 数字而非字符串）。
  **补 Python 侧这组用例是因为契约测试原本是不对称的**：先只有 Go 侧钉着，而它用的是硬编码
  字面量 —— 也就是说改 `builder.py` 的键名会让两侧单测**全绿**，线上成本却恒为 0。
  已做变异验证：把 `llm_total_tokens` 改名后该用例确实变红。
- 落库路径（`worker/cost_test.go` 4 项，真 Redis）：**专门覆盖「任务级事件无 shot_id」**
  这一条 —— 成本记账若写在提前返回之后就会被吞掉。对实现做过**变异验证**：
  把记账块短路后，用例确实变红。
- **真实 gRPC 通道**（`worker/cost_transport_test.go` 2 项）：起真实 server 扮演 Python，
  经 `RunPipeline` 流式发出真实形状的 payload，再由 Go 真实客户端接收落库。
  为什么必须有这一层：直接构造 `pb.PipelineEvent` 的用例把传输整段跳过了，
  而成本归零最常见的两种真实原因（字段没被赋值、消息被接收上限截断）都发生在这里。
- HTTP（`httpapi/cost_test.go` 4 项）：含「详情里也要有 cost」与「不存在的任务 404」。
- **真实任务端到端**（本机，Redis/RustFS/ffmpeg/Edge TTS 全真实）：

  | 任务 | 结果 |
  | --- | --- |
  | `job-db02acefed80f53d` | `render_sec=2.549`、`tts_chars=40`、`tts_shots=2`、`shots=4`、`approved=2` |
  | 独立重算 | 从分镜原文重算得 **2.549 / 40 / 2 / 4 / 2** —— 逐项一致 |

  其中两个 `AWAITING_HUMAN` 的镜头（未渲染、未配音）贡献 **0**，反向控制在实际数据上成立。
  `tts_chars=40` 对应两份**真实存在**的配音文件（22608 / 26064 字节，各带 `marks.json` sidecar），
  `render_sec` 来自 Python 用 `time.monotonic()` 实测的渲染墙钟时间。

- `job-730ed329c08c3524` 是**改动前的 Python 服务**产出的任务（final 事件里没有 `cost` 键）。
  它的成本接口仍正常返回推导项，`llm_usage` 为 `null` 而**不是**编出来的零值记录 ——
  这是一次意外的真实反向控制，说明「上游没报」和「报了 0」在数据上可区分。

**没验证的部分（不要让上面的绿字掩盖它）**

- **真实 token 数走完全链路未经实测**：本机 `SCID_LLM_PROVIDER=mock`，而 mock 模式
  **不产生用量**（`_mock_response` 直接返回文本，不经过 usage 累加），因此真实任务的
  `llm.*` 恒为 0。已用两条独立证据补上这个缺口：① `llm.py` 的 usage 累加路径与
  `builder.py` 的 `cost` 组装经单测验证（`Usage(100,250,3)` → `350/3`）；
  ② 真实 gRPC 通道用例用非零数字（350/3、33/2）走通传输+落库。
  **但「接上真 LLM 后数字是否正确」仍需一次带 API key 的实测。**
- **配额与限流未做**。验收标准只要求「可查询」，故本轮到此为止；它与多租户耦合
  （限额要按归属算），建议与阶段五第 4 项一起做。

### 沙盒网络隔离：做到哪一步、证明是什么

验收标准是「沙盒在无网络条件下**仍能完成渲染**（证明没有隐式外联）」。
这句话有两个方向，缺一个都算没验：**外联确实被挡住**，以及**渲染确实还能成功**。
只验后者，等于在一个本来就断网的机器上跑绿 —— 什么也没证明。

实现：`SandboxRunner` 把命令包进 `unshare -r -n -- <原命令>`（`sandbox/netns.py`）。
`-n` 建新网络命名空间；`-r` 同时建用户命名空间把当前 uid 映射成里面的 root
（非 root 用户本来无权建网络命名空间，本机 `unshare -n` 就是不可用的，只有
`unshare -rn` 可以 —— 所以可用性**必须实跑一条最小命令探测**，不能看到 Linux 就假定可用）。

**机制层面的实测证据**（`ai/tests/test_netns.py`，10 项）：

| 验证项 | 结果 |
| --- | --- |
| 隔离下 `connect("8.8.8.8", 53)` | **BLOCKED** |
| **不隔离**时同一段代码、同一台机器 | **REACHABLE**（反向对照，本次运行实际走到） |
| 隔离下 DNS 解析 `example.com` | **BLOCKED** |
| 隔离下真实跑 ffmpeg 渲染 | **出片**（产物非空） |
| 隔离下 loopback 状态 | DOWN（见下） |

反向对照这一条是必须的：没有它，「外联被挡住」在一个本来就上不了网的机器上
会**永远为真** —— 那时用例是绿的，却只是证明了这台机器没网。
本次运行它确实连出去了，所以「被挡住」是隔离的功劳，不是环境的巧合。

**真实任务端到端**（`job-0479b151dae53242`，隔离开启）：`/healthz` 报
`sandbox:network=netns`，任务照常渲染成功（`render_sec=2.782`、2 个镜头通过），
且 Edge TTS 仍能合成（`tts_chars=40`）—— 后者恰恰说明**隔离的边界是对的**：
沙盒里的渲染代码没有网络，而服务进程自身的对外调用（LLM / TTS）不受影响。

**两个实测出来的事实**（都已写进代码注释与 `docs/DESIGN.md`）：

- **新命名空间里 loopback 是 DOWN 的，但不影响任何现有引擎。**
  Playwright 用 `--remote-debugging-pipe`（管道而非 TCP）与 Chromium 通信，
  隔离前后截图**逐字节相同**。因此刻意**不**去折腾 loopback ——
  多一步就多一处会坏的地方。`test_loopback_state_inside_namespace_is_documented`
  把这个事实钉住：将来若某个引擎真的需要 loopback，它会给出一个**指向原因**的失败。
- **`unshare` 默认 `exec` 成目标命令（同一 PID）**，所以内存探针（读 `/proc/<pid>` 的
  `VmHWM`）与杀进程树仍指向真进程。**绝不能加 `--fork`** —— 那会让峰值内存变成
  读 unshare 自己（小到离谱且没有任何报错）。

### 只读文件系统：容器级 `--read-only` 的命名空间等价物

需求里写的是「容器级 `--network=none --read-only`」。网络那半已由 `unshare -r -n` 覆盖；
只读这半用**非特权挂载命名空间**实现，效果等价：

    unshare -r -n -m -- python exec_guard.py --workdir <wd> -- <原命令>

`exec_guard` 做三件事：把**每一个真实文件系统**重挂为只读、挂一个有界私有 tmpfs 到 `/tmp`、
把工作目录绑定回可写；然后 **exec 掉自己（PID 不变）**。

| 事实 | 说明 |
| --- | --- |
| `mount -o remount,ro,bind /` **只作用于根那一个挂载** | 本机 `/vol1`/`/vol2` 是独立 btrfs、`/boot/efi` 是 vfat。只重挂 `/` 之后往 `/vol1/...` 写文件**照样成功** —— 我第一版就这么写，测试当场把文件写进了宿主机的项目目录。现遍历 `/proc/self/mountinfo` 逐个重挂 |
| 只读根会让 `/tmp` 不可写 | ffmpeg / Playwright 都要写临时目录，表现为"渲染莫名失败"。故挂**有界**私有 tmpfs（`size=256m`；不设上限的 tmpfs 能吃掉整机内存） |
| 工作目录若在 `/tmp` 下，替换 `/tmp` 会**遮蔽**它 | 写进去的东西命名空间外看不到，表现为"渲染成功但产物凭空消失"。检测到就**不**替换并如实记 gap |
| "配置启用了" ≠ "实际生效" | 网络隔离由能力+配置决定；只读是**逐次**才知道（某个挂载点重挂失败即失效）。因此 `read_only_enforced`/`read_only_gaps` 来自子进程写回的报告，读不到一律按未生效处理，且不生效时**打警告日志** |

**证据**：17 项测试，含**反向对照**（关闭只读时写父目录必须**成功** —— 否则"被挡住"
在一个本来就没有写权限的机器上会永远为真）与「只读下 ffmpeg 真实出片」；
**变异验证**：把重挂改成"只计数不真挂"（即谎报已加固）后用例立刻变红。
真实任务在只读开启下照常出片，且日志里「沙盒只读未生效」出现 **0 次**。

**为什么默认 `off`**：只读根会让 `$HOME` 下的缓存（matplotlib 字体缓存、LaTeX 缓存）
不可写，而 manim/LaTeX 依赖它们。开启前必须逐个引擎验证 ——
不能替使用者默认打开一个会让 manim 静默失败的开关。

**没做到的部分（这一项不能算全部完成）**

- **HTML 引擎（d3 / echarts / code_anim）不在隔离范围内。**
  `HtmlRenderer._capture` 是在 AI 服务进程里直接 `sync_playwright()` 起 Chromium，
  **不经过 `SandboxRunner`**，因此本层的网络命名空间覆盖不到它 ——
  也就是说 LLM 生成的 HTML/JS 目前仍在有网络的浏览器里执行。
  要补上需要把截图步骤移进沙盒，而**直接移会立刻撞上另一个问题**：
  runner 的 `RLIMIT_AS` 兜底会让 Chromium 瞬间 SIGTRAP 崩溃（已逐项二分确认：
  `RLIMIT_DATA` 2GB/8GB 正常，`RLIMIT_AS` 8GB/32GB 都崩）—— 所以这一步要连带调整
  资源限制策略，属于独立的一次改动，**本轮没有做**。
- **seccomp 白名单未做。** 本机有 `libseccomp.so.2`（可经 ctypes 使用），因此这条路可行；
  未做的原因是需要先确定"白名单还是黑名单"以及逐个引擎验证系统调用面 ——
  一个过窄的白名单会让渲染**静默失败**，比不加更危险。
- **真正的容器层仍未做。** 命名空间路径覆盖了「无网络」与「只读文件系统」两件事，
  但没有覆盖 cgroup 资源限制与镜像级的最小化；生产若跑容器，容器层仍应作为**外层**
  防御（纵深防御，不是二选一）。
- **`require` 模式下的 fail closed 只经单测覆盖**（不可用场景靠 monkeypatch 构造），
  没有在真实缺少 `unshare` 的机器上跑过。

### 验收标准

- ✅ 一次生成请求能在 Grafana 上看到完整的 span 树与耗时分解
      （已验：跨三个服务、25 个 span；浏览器里断言过跨服务 span 名与耗时数值。
       未验：从表格点进单条 trace 的嵌套瀑布图未做自动化断言）
- ✅ 能查询任意历史任务的 token 成本与渲染成本（`GET /api/v1/jobs/:id/cost`）
- ✅ 多租户：任务归属与隔离（`docs/ROADMAP.md` 阶段五「多租户」一节；
      **注意身份来源是请求头而非认证**，物理隔离未做）
- ✅ 定期比对 Go 的 Redis 状态与 LangGraph checkpoint，并修复双写不一致
      （已验：周期扫描真实检出并修复一个"卡住"的任务、只读模式只报不改、
       修复幂等且写事件；**只自动修一类**，详见上文）
- 🟡 沙盒在无网络条件下仍能完成渲染（证明没有隐式外联）
      （已验：外联与 DNS 被挡住、**同一台机器不隔离时能连出去**这个反向对照成立、
       隔离下 ffmpeg 真实出片；只读根已实现并有反向对照与变异验证。
       未做：**seccomp 白名单**；HTML 引擎的 Chromium 不经 runner，**未被隔离**）

---

## 进度速查

| 阶段 | 状态 | 完成度 |
| --- | --- | --- |
| 一 · 环境与骨架 | ✅ 已完成 | 验收命令全部通过 |
| 二 · 多智能体核心 | ✅ 已完成 | 368 → 377 个 Python 单测通过；5 个 RPC 全部实现并经 Go 侧 gRPC 打通 |
| 三 · 编排与媒体 | ✅ 已完成 | FFmpeg 并发收敛为全局有界 Worker Pool、转场与统一调色、字幕与软字幕封装、局部重渲染、产物归档、队列可观测；Go 测试 media 包 68 项 / archive 包 27 项 / queue 包 6 项。B4/B5 已补自动化验证（B4 共 6 项、B5 共 2 项；其中「执行中断线」是 `make test-failover` 显式运行的慢用例）。**全片 TTS 配音待办**（无可用引擎） |
| 四 · 反馈闭环与前端 | ✅ 已完成 | React 审核台、打回/放行/成分镜编辑、断线重连与快照重放；C4/C5 已用真实服务端到端验证，C1/C3 的浏览器人工目视验证待做 |
| 五 · 生产加固 | ⏳ 进行中 | 沙盒加固 🟡（网络隔离 + 只读根；**seccomp 未做**）； 状态对账 ✅（周期扫描 + 按需接口；**只自动修「卡住」一类**）； 多租户 ✅（归属 + 隔离 + 按租户配额；**身份来源仍是请求头，不是认证**）； 可观测性 ✅（一次生成请求 = 跨 scid-api/scid-worker/scidirector-ai 的 25 个 span，浏览器里验证过 Grafana 能看到跨服务 span 与耗时；**业务指标未导出**、嵌套瀑布图未自动化断言）；成本核算 ✅（`GET /jobs/:id/cost` + 详情内 `cost`；真实任务端到端复验，推导项与分镜原文逐项一致）；沙盒网络隔离 🟡（`unshare -r -n` + 反向对照验证「外联确实被挡住」；**HTML 引擎的 Chromium 未被覆盖**、容器层与 seccomp 未做）。**未做**：真实 token 链路实测（本机 mock LLM 不产生用量）、配额与限流、可观测性、多租户、状态对账 |
