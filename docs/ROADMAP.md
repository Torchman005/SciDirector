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
| A6 | ✅ | `tests/test_sandbox_runner.py` 死循环用例超时后进程树被杀，无孤儿进程；Windows 走 Job Object，POSIX 走 `RLIMIT_AS`+`killpg` |
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

## 阶段三 · Go 编排与媒体处理 🚧 进行中

阶段一已注入主链路处理器与 ffmpeg 合成骨架，阶段三补齐：

1. **字幕与配音**：全片 TTS、字幕时间轴对齐、软字幕封装（`MuxFinal` 接口已就绪）— ⏳ 待办
2. **转场与统一规格**：跨镜头转场、统一调色（避免 Manim 与浏览器录制出的画面质感割裂）— ✅ 已完成
3. **局部重渲染**：只重渲某几秒而不是整镜（进一步降本）— ⏳ 待办
4. **产物归档**：中间产物与成片上传 MinIO，本地卷按策略清理 — ⏳ 待办
5. **队列可观测**：暴露 Asynq 队列深度、失败任务数 — ⏳ 待办

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

## 阶段四 · 反馈闭环与前端 ⏳ 待办

1. **React 审核台**：分镜表、视频预览、事件时间线、实时进度条
2. **打回重做**：可视化填写意见，走 `reject` 接口或 WebSocket `feedback`
3. **断线重连**：用 `last_event_id` 自动补齐，不丢事件、进度不回跳
4. **人工放行**：熔断镜头的 approve 出口
5. **成分镜编辑**：直接修改分镜的 `narration` / `visual_brief` 后重做

### 验收标准

| 编号 | 验收项 | 判定方式 |
| --- | --- | --- |
| C1 | 提交脚本后，前端实时看到分镜逐个出现并推进状态 | 人工观察 |
| C2 | 刷新页面不丢上下文（快照 + 历史重放生效） | 刷新后事件时间线完整 |
| C3 | 断网 10 秒后恢复，进度自动补齐且不回跳 | 断网测试 |
| C4 | 打回一个镜头后**只有它**重新渲染，其余镜头进度不变 | 观察事件流与日志 |
| C5 | 熔断镜头可人工放行，任务继续合成 | 端到端演示 |

---

## 阶段五 · 生产加固 ⏳ 待办

- **可观测性**：OpenTelemetry 链路追踪（Go span ↔ gRPC ↔ Python span 串联）、Prometheus 指标、Grafana 看板
- **成本核算**：按任务统计 token 与渲染时长，接入配额与限流
- **多租户**：任务归属与隔离
- **状态对账**：定期比对 Go 的 Redis 状态与 LangGraph 的 checkpoint，发现并修复双写不一致
- **沙盒加固**：容器级 `--network=none --read-only`、seccomp 白名单

### 验收标准

- 一次生成请求能在 Grafana 上看到完整的 span 树与耗时分解
- 能查询任意历史任务的 token 成本与渲染成本
- 沙盒容器在无网络条件下仍能完成渲染（证明没有隐式外联）

---

## 进度速查

| 阶段 | 状态 | 完成度 |
| --- | --- | --- |
| 一 · 环境与骨架 | ✅ 已完成 | 验收命令全部通过 |
| 二 · 多智能体核心 | ✅ 已完成 | 368 个 Python 单测通过；5 个 RPC 全部实现并经 Go 侧 gRPC 打通（A1/A3~A7 通过，A2/A8 受本机工具链限制部分验证） |
| 三 · 编排与媒体 | ⏳ 待办 | 主链路与合成骨架已注入（Go 侧） |
| 四 · 反馈闭环与前端 | ⏳ 待办 | WebSocket 服务端与事件流已就绪（Go 侧） |
| 五 · 生产加固 | ⏳ 待办 | — |
