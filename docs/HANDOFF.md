# SciDirector 交接说明（换机 / 新会话续作）

> 本文件是**会话引导**，不是状态真源。
> 项目状态以 `git log`、`Agent.md §10 迭代日志`、`docs/ROADMAP.md` 的验收表为准 ——
> 它们随每次提交更新；本文件的数字会过时。
>
> 用途：换一台电脑、或开一个全新的 AI 会话时，把下面代码块里的内容整段贴进去。

---

## 可直接粘贴的提示词

```text
我在继续一个已有的项目 SciDirector（科学视频导演）。请不要从零开始设计，
先按下面的顺序建立上下文，再动手。

## 第 1 步：取得代码

git clone git@github.com:Torchman005/SciDirector.git
（远端也可能已更新，先 git fetch 再看。分支是 main。）

## 第 2 步：按顺序读这四份文档（它们是这个项目真正的记忆）

1. Agent.md                  —— 协作契约。重点看 §4 进度表、§5 智能体契约、
                                §6 状态机、§9 跨语言实现约定（30+ 条踩坑记录）、
                                §10 迭代日志（v0.1.0 ~ 最新）
2. docs/ROADMAP.md           —— 五个阶段的交付物与**逐项验收结果**，
                                含「发现并修复的问题」表
3. docs/DESIGN.md            —— 为什么这么做、代价与取舍
4. docs/API.md               —— REST / WebSocket / gRPC 契约

读完后你应该能回答：这个系统由哪几层组成、契约在哪、"哪些事被验证过、
哪些没有"。

## 第 3 步：搭环境并跑通验证基线

这是一个 Go + Python 混合项目。按 README 或下面的说明装依赖，
然后**必须**先跑通基线再改代码：

  Go：      gofmt -l . && go vet ./... && go test ./internal/...
  Python：  cd ai && python -m pytest tests -q
  前端：    cd web && npx tsc --noEmit && npx vitest run && npx vite build

注意：部分集成测试需要**本机 Redis** 与 **ffmpeg**，缺失时会 SKIP 而不是失败。
如果看到大量 SKIP，先确认这两个依赖，否则你以为跑过了其实没跑到。

## 第 4 步：然后开始阶段五（生产加固）

见 docs/ROADMAP.md「阶段五」。范围：
  - 可观测性：OpenTelemetry 链路追踪（Go span ↔ gRPC ↔ Python span 串联）、
    Prometheus 指标、Grafana 看板
  - 成本核算：按任务统计 token 与渲染时长，配额与限流
  - 多租户：任务归属与隔离
  - 状态对账：定期比对 Go 的 Redis 状态与 LangGraph checkpoint
  - 沙盒加固：容器级 --network=none --read-only、seccomp 白名单

在做阶段五之前，有一批**明确的遗留待办**，请先问我希望优先做哪个：
  1. 全片 TTS 配音 —— 之前一直没做，因为当地没有可用 TTS 引擎，
     且需要先确定服务商（Edge TTS / Azure / OpenAI / 本地模型）。
     MuxFinal 的 audioPath 参数与 acrossfade 音频链**已经就绪**，
     接入时无需改动调用契约。接入后字幕时间轴应从「按文本长度比例估算」
     切换为「按真实音频时长对齐」。
  2. 阶段三验收 B4（同一 (job,shot,attempt) 重复投递只渲染一次）
     与 B5（断开 Redis 后恢复继续执行）尚无自动化验证。
  3. s3 归档路径**未经端到端验证**（当时没有 MinIO 实例）。
     默认后端是 local，未验证的代码不会在缺省配置下执行。
  4. 阶段四验收 C1/C3 的**浏览器人工目视验证**未做；
     「断网 10 秒后恢复」也没有真实演练过（逻辑有单测）。

## 工作方式（请严格照做）

- **到里程碑就提交**，提交信息用中文，说清「改了什么」和**「为什么」**，
  尤其是踩到的坑。推送前先 git fetch，若远端已前进就 **rebase**（不要 force push）。
- **文档是活文档**：任何架构/契约变更必须在**同一次提交**里更新
  Agent.md 与 docs/ 下的对应文档。
- **每层落地就用真实依赖跑一遍**。这个项目里最严重的缺陷全部是端到端
  跑测才暴露的（详见 Agent.md §10 各版本条目），单测全绿不代表没问题。
- **如实区分「已验证」与「没验证」**。ROADMAP 的验收表里明确标了哪些项
  只是部分验证。请不要把没跑过的东西说成跑过了。
- **不要扩大范围**。我会明确说做什么；没说的先问，不要顺手重构。

## 必须提前知道的坑（会在新机器上立刻遇到）

1. **本项目的 `pwsh` 实际就是 Windows PowerShell 5.1**，不是 PowerShell 7。
2. **含中文的 .ps1 必须带 UTF-8 BOM**，否则 5.1 会按 GBK 解码，
   中文误码后吞掉换行，整份文件无法解析，**且报错行号指向错误位置**。
   仓库里的 scripts/*.ps1 已加 BOM，改它们时不要丢掉。
3. **绝不要用 PowerShell 的 Get-Content / Set-Content 改 UTF-8 源码**，
   它按系统代码页读写，会静默破坏中文注释（本项目踩过）。
   要读写文件用 .NET 显式指定编码，或用工具直接编辑。
4. **控制台会把 UTF-8 显示成 GBK 乱码，但文件其实是好的**。
   看到乱码先别急着"修"——用 node / Go 读一遍确认字节，再决定。
   （本项目因此差点误改一个本来正确的文件。）
5. `go test -race` 在 Windows 上会报 0xc0000139（缺 race 运行时 DLL），
   这是环境限制不是代码问题。Makefile 里 RACE 变量可覆盖。
6. `.gitignore` 里按名字忽略的目录规则**必须带前导斜杠锚定**。
   曾经一条未锚定的 `media/` 把整个 backend/internal/media/ Go 包吞掉，
   6 个文件从未入库，clone 下来直接构建失败。

## 项目一句话

Go 负责编排、状态、并发与媒体处理；Python 负责语义、生成与 VLM 审查；
两者通过 proto/scidirector/v1 的 gRPC 契约通信。
核心主张是**不生成像素，而生成可验证的渲染计划**。
```

---

## 一、环境清单（新机器需要装的）

本项目在 Windows 上开发。当前机器的实际版本，供对照：

| 组件 | 版本 | 备注 |
| --- | --- | --- |
| Go | 1.26.5 | `go.mod` 声明 1.23 |
| Python | 3.13.5（Anaconda） | `requires-python >=3.11` |
| Node / npm | v26.8.2 / 11.19.1 | 前端用 Vite 5 + React 18 |
| ffmpeg | 8.1.1 full build | 媒体层与集成测试需要 |
| protoc | 随 Anaconda 安装 | 仅改 proto 时需要 |
| PowerShell | **5.1** | 不是 7，见下方坑 |

**不在仓库里、必须在每台机器上重建的东西**：

| 路径 | 是什么 | 怎么来 |
| --- | --- | --- |
| `.gocache/` | 仓库内的 GOPATH/GOMODCACHE/GOCACHE | 首次 `go build` 自动生成 |
| `.pylibs/` | 仓库内的 Python 依赖目录 | `pip install --target .pylibs -r ai/requirements.txt` |
| `web/node_modules/` | 前端依赖 | `cd web && npm install` |
| `backend/bin/` | 构建产物 | `go build -o bin/scid-api.exe ./cmd/api` |
| `.data/` | 运行时产物 | 自动生成 |
| Redis | **本机安装，不在仓库里** | 见下 |

> `.pylibs/` 只是本机为隔离依赖而用的目录，**不是唯一方案**。
> 用 venv 或 conda 环境同样可以 —— 但若保留 `.pylibs`，
> 它必须放在 `PYTHONPATH` 的**末尾**（放前面会遮蔽版本更完整的同名包，
> 表现为 pydantic 导入时莫名的 `cannot import name`）。

## 二、环境变量（每次开新终端都要设）

```powershell
# 把 Go 的缓存固定在仓库内，避免污染全局环境
$env:GOPATH      = "$PWD\.gocache\gopath"
$env:GOMODCACHE  = "$PWD\.gocache\gopath\pkg\mod"
$env:GOCACHE     = "$PWD\.gocache\gobuild"

# 临时目录也放仓库内（沙盒环境常限制仓库外写入）
$env:TEMP = "$PWD\.tmp"; $env:TMP = "$PWD\.tmp"

# Python
$env:PYTHONPATH       = "$PWD\ai;$PWD\.pylibs"   # .pylibs 必须在末尾
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'                       # 否则子进程 stderr 走 GBK，回灌给模型的报错是乱码
```

也可以直接用仓库自带的脚本：`& .\scripts\dev-env.ps1`（它已含上述设置）。

**Redis**（本机安装路径因机器而异，仓库里没有）：

```powershell
# 仓库提供了一个仅用于本地手工测试的配置（关闭持久化、只监听 127.0.0.1）
& '<你的 redis-server 路径>' '.tmp\redis-manual.conf'
```

`queue` 与 `httpapi` 两个包的集成测试需要它；不可达时会 SKIP。

## 三、验证基线（改任何代码之前先跑一遍）

```powershell
# Go
cd backend
gofmt -l .                      # 必须无输出
go vet ./...                    # 必须 0
go test ./internal/... -count=1 # 全绿；media 包约 55s（含真实 ffmpeg 集成）

# Python
cd ai
python -m pytest tests -q       # 约 2 分钟

# 前端
cd web
npx tsc --noEmit
npx vitest run
npx vite build
```

**看到大量 SKIP 要警惕**：集成测试在缺 Redis / ffmpeg 时会跳过而非失败。
跳过意味着你以为跑过了，其实没跑到。

## 四、当前进度

| 阶段 | 状态 |
| --- | --- |
| 一 · 环境与骨架 | ✅ |
| 二 · Python 多智能体核心 | ✅ |
| 三 · Go 编排与媒体处理 | ✅（**全片 TTS 配音除外**） |
| 四 · 反馈闭环与前端 | ✅ |
| 五 · 生产加固 | ⏳ 下一步 |

各阶段的具体验收项与「哪些只是部分验证」见 `docs/ROADMAP.md`。
逐版本的变更与踩坑见 `Agent.md §10`。

## 五、本地如何把整套系统跑起来

```powershell
# 1) Redis（见上）
# 2) Python 大脑
cd ai; python -m scidirector_ai.main          # :8000 HTTP + :50051 gRPC

# 3) Go 网关与 worker（各自一个终端）
.\backend\bin\scid-api.exe                    # :8080
.\backend\bin\scid-worker.exe

# 4) 前端审核台
cd web; npm run dev                           # :5173，已配好 /api 与 /ws 代理
```

联调用的关键环境变量：`SCID_REDIS_ADDR`、`SCID_AI_GRPC_ADDR`、`SCID_HTTP_ADDR`、
`SCID_MEDIA_WORK_DIR`、`SCID_LLM_PROVIDER=mock`（无 API key 时用 mock）、
`SCID_RENDER_WIDTH/HEIGHT/FPS`（本机配置低以便快速跑通）。

## 六、一句话提醒

这个项目的价值有一半在**文档与"哪些没验证"的诚实记录**里。
续作时请保持这个习惯：改动 → 真跑一遍 → 同提交更新文档 → 提交推送。
