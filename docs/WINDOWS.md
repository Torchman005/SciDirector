# Windows 原生启动说明

> 本文是 `scripts\dev.bat` 的中文说明。脚本本身是**纯 ASCII**，原因见下。

---

## 一、为什么 `dev.bat` 里一个中文都没有

批处理的解析依赖**控制台代码页**。中文写进 `.bat` 会以多种方式出错，
而这几种本项目**全都踩过**：

| 现象 | 原因 |
| --- | --- |
| `'环境自检（工具链' is not recognized as an internal or external command` | cmd 把 UTF-8 字节按 GBK 解码，产生游离字符并破坏语法；**报错指向的字符与真正的原因毫无关系** |
| `Maximum setlocal recursion level reached` | 同上，解析错位后控制流被破坏 |
| 行首字符被吃掉（`setlocal` → `etlocal`） | `.bat` 是 LF 换行。cmd 对 LF-only 批处理有缺陷 |

**关键教训：`chcp 65001` 放在开头并不可靠。** 它不保证在 cmd 解析后续行之前生效。
最讽刺的一次是：为了说明「`chcp` 必须排在所有中文之前」，
我把这段说明**放在了 `chcp` 之前** —— 于是解释这个坑的注释本身成了故障源。
而且它只在**全新控制台**（GBK 码页）下暴露，从已切到 UTF-8 的终端里调用时一切正常，
排查方向被误导了很久。

**所以：把文件保持纯 ASCII，从根上消除这一整类问题。**
它在任何控制台、任何区域设置、任何代码页下解析行为完全一致。

仍然**必须**保留的是 `.gitattributes` 里的 `*.bat eol=crlf` —— 那条对应上表第三行。

中文说明放在本文件（UTF-8，在 `.md` 里是安全的）。

---

## 二、用法

```cmd
:: 打开一个 cmd 窗口（开始菜单搜 cmd / Win+R 输入 cmd）
cd /d D:\itJinYu_toolkit\SciDirector

scripts\dev.bat check           :: 环境自检
scripts\dev.bat build           :: 编译 Go 的 api / worker
scripts\dev.bat start           :: 启动全部（每个服务一个窗口）
scripts\dev.bat stop            :: 停止全部
scripts\dev.bat status          :: 查看端口状态
scripts\dev.bat run <服务>       :: 前台单独运行一个服务
```

**推荐从已打开的 cmd 窗口运行，而不是双击。** 双击时脚本跑完窗口会立刻关闭，
输出一闪而过 —— 那是批处理的固有行为。现在脚本会检测双击并自动暂停
（判定依据：双击时 cmd 命令行里含脚本名，而终端里手输命令时不含），
所以双击也不会再看不到输出，但开发时反复执行命令还是开一个 cmd 更顺手。

---

## 三、不用 Docker 也能跑

`dev.bat` 已把这些设成默认值，**整个系统唯一的硬依赖是 Redis**：

| 组件 | 不装会怎样 |
| --- | --- |
| Postgres | `SCID_POSTGRES_DSN` 置空 → LangGraph checkpointer **显式降级**为内存实现（只告警不崩），代价是进程重启后无法断点续跑 |
| RustFS / S3 | `SCID_ARCHIVE_BACKEND=none` → 归档是 no-op；想本地留档改成 `local` |
| 观测栈 | `SCID_OTEL_ENDPOINT` 置空 → 链路追踪 no-op（依赖也是惰性导入的） |

本机的 Redis 用仓库外的原生 `redis-server.exe`，路径通过 `SCID_REDIS_BIN` 覆盖：

```cmd
set SCID_REDIS_BIN=D:\path\to\redis-server.exe
```

---

## 四、`.env` 会被自动加载

这一点 Windows 侧**长期是缺的**：根目录 `.env` 只在 `docker compose` 插值时生效 ——
Go 根本不读 `.env` 文件，Python 的 `env_file` 又是相对当前目录解析的（找的是 `ai\.env`）。
于是「照着 `.env.example` 配好密钥、再跑本地进程」会**静默进入 mock 模式**：
内容全是占位，且没有任何报错。

现在 `dev.bat` 会调用 `scripts\load-env.ps1` 加载它，语义与 POSIX 侧的
`scripts/load-env.sh` 完全一致，解析逻辑**只有这一处实现**（多处各写一套必然分叉）：

- 显式环境变量优先于文件
- 跳过空行与整行注释
- 去掉值两侧的成对引号
- **行内注释仅在 `#` 前有空白时截断**，引号内的 `#` 保留

最后一条最容易漏。`.env.example` 里就有 `SCID_SANDBOX_MANIM_QUALITY=l   # l=480p`，
不处理的话值会变成整行，pydantic 直接报 `literal_error`、服务起不来。

---

## 五、配模型服务商

`.env` 里至少要配好「服务商 + 密钥」。**模型名建议留空**，让代码用出厂默认值：

| 服务商 | 默认文本模型 | 默认视觉模型 |
| --- | --- | --- |
| `openai` | `gpt-4o` | `gpt-4o` |
| `deepseek` | `deepseek-chat` | **无**（DeepSeek 没有视觉模型） |
| `bailian`（阿里云百炼） | `qwen-plus` | `qwen-vl-max` |
| `mock` | `mock` | `mock` |

> ⚠️ **别自己编模型名。** 本项目实际遇到过把 `SCID_LLM_MODEL` 填成
> `deepseek-v4.1-flash`、`SCID_VLM_MODEL` 填成 `qwen-v1-max` 的情况 ——
> 这两个名字在服务商那边都不存在，**每次调用都会 404**。
> 留空即可，除非你有明确理由指定别的模型。

推荐组合（DeepSeek 写代码便宜但无视觉，审查另配一家有视觉的）：

```ini
SCID_LLM_PROVIDER=deepseek
SCID_DEEPSEEK_API_KEY=sk-...
SCID_VLM_PROVIDER=bailian
SCID_DASHSCOPE_API_KEY=sk-...
```

> ⚠️ 若视觉服务商没配好，系统**不会**退回 mock 审查 —— 那会返回伪造的「审查通过」。
> 正确行为是每个镜头降级转人工，并在 `/healthz` 报 `llm:vision=none`。

改完 `.env` **必须重启 AI 服务**（环境变量是进程启动时读的）：

```cmd
scripts\dev.bat stop
scripts\dev.bat start
```

验证：

```cmd
curl.exe http://127.0.0.1:8000/healthz
```

`capabilities` 里应出现 `llm:text=deepseek/deepseek-chat`，且**不再有** `mock-llm`。

---

## 六、渲染引擎与模型是两件事

**换真模型只解决「代码生成对不对」，解决不了「本机有没有渲染引擎」。**

| 引擎 | 需要 | 本机现状 |
| --- | --- | --- |
| stock（AMBIENCE 氛围镜头） | ffmpeg | ✅ 可用 |
| manim（MATH 数学镜头） | `pip install manim` + MiKTeX/TeX Live | ❌ 缺 |
| d3 / echarts（DATA 数据镜头） | `pip install playwright && playwright install chromium` | ❌ 缺 |
| code_anim（CODE 代码镜头） | 同上 | ❌ 缺 |

缺引擎的镜头会熔断为 `AWAITING_HUMAN`，任务终态是 `PARTIAL` ——
**这是设计行为，不是故障**：引擎不可用属于镜头级失败，不该拖垮其余镜头。

实际可用的引擎从 `/healthz` 的 `engines` 字段读：

```cmd
curl.exe http://127.0.0.1:8000/healthz
```

想一次装齐，用 Docker（镜像里 manim + LaTeX + Playwright 都装好了）：

```cmd
docker compose up -d --build
```

---

## 七、工作目录用绝对路径

Python 的 `sandbox_work_dir` 默认是**相对路径** `./.data/sandbox`，
于是它跟着**启动时的 cwd** 走 —— `cd ai` 之后启动就落到 `ai\.data\sandbox`，
在仓库根启动就落到 `.data\sandbox`。产物因此分裂成两棵树，
清理和排查时极易找错地方。

`dev.bat` 会把相对路径按仓库根**展开成绝对路径**（而不是无条件覆盖 ——
`.env` 或命令行里显式配的值仍然生效）。涉及：

```
SCID_MEDIA_WORK_DIR      默认 <repo>\.data\work
SCID_SANDBOX_WORK_DIR    默认 <repo>\.data\sandbox
SCID_ARCHIVE_LOCAL_DIR
```

---

## 八、常见问题

**Q: `make dev-ai` 报 `make (e=2): 系统找不到指定的文件`**

Makefile 的配方用了 `sh -c` 与 `. ./scripts/load-env.sh`，而 Windows 原生环境
**没有 `sh`**，于是 make 把配方喂给了 `cmd.exe`，cmd 一个都不认识
（`. 不是内部或外部命令`、`'{' 不是内部或外部命令`、`此时不应有 -x`）。

两条路：

1. **用 `scripts\dev.bat`**（推荐，不依赖 make / sh）
2. 把 Git 自带的 sh 加进 PATH：`set "PATH=D:\Git\usr\bin;%PATH%"`
   —— 实测这能让 `make doctor` 跑通，但 `make dev-ai` 仍会因配方的
   引号问题失败（`echo: -c: line 1: unexpected EOF`）。所以推荐第 1 条。

**Q: 服务起来了但内容是 `（mock）...` 的占位**

说明没读到模型配置。用 `scripts\dev.bat check` 看「resolved configuration」段，
它会直接打出 LLM 服务商、Postgres DSN、两个工作目录 —— 一眼看出哪一项没生效。

**Q: 任务跑完是 `PARTIAL` 而不是 `SUCCESS`**

看 `/healthz` 的 `engines`。缺引擎的镜头会熔断转人工，这是预期行为。
在审核台上可以对熔断镜头「放行」或「打回重做」。

**Q: 成片比预期短、少了几个镜头**

人工放行了**没有产物**的镜头（引擎缺失时它们本来就没渲染出视频）。
此时任务会**如实标为 `PARTIAL`** 并给出说明事件，不会假装成功 ——
一部静默少几段的片子比明确失败更危险。
