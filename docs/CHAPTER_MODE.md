# 章节模式与 fy 画风

## fy-code-animation / 341 画风库试验

`--engine fy-canvas` 增加独立的 Canvas 章节后端；默认仍为 `svg`。
fy-code-animation 是创作规范与组件库，并非独立渲染器。本次固定采用
[`Akillll-DuoHe/fy-code-animation`](https://github.com/Akillll-DuoHe/fy-code-animation)
提交 `d9a545afa393b2ffab4f36e87371691daf8e502c` 的 `motion-core.mjs`、
`canvas-contract.mjs` 和 `style_library.py`，SHA-256 随源码保留。

外部画风库为 [`wqb578/code-animation-341-styles`](https://github.com/wqb578/code-animation-341-styles)，
本机检查的提交为 `01576924a2b316d1f11ae33f75d3c005e8781451`，341 份提示词完整，
上游 doctor 的 `ready` 为 true。只拉取文字与许可，没有下载参考视频或 JPG。
索引版本是全部提示词的内容版本，不等同于 Git 提交。

在仓库根目录运行；未安装 Python 包时先设置 `PYTHONPATH`。

```powershell
$env:PYTHONPATH = "$PWD/ai;$PWD/.pylibs"
$env:PYTHONIOENCODING = "utf-8"

# 默认位置为 .data/integrations/code-animation-341-styles；也可显式指定根目录。
$env:FY_STYLE_LIBRARY_ROOT = "$PWD/.data/integrations/code-animation-341-styles"
python scripts/fy-styles.py search --query "蓝图" --limit 5
python scripts/fy-styles.py get --query X17

# 不调用模型或 TTS：渲染手工 X17 蓝图候选样片，输出目录必须是新目录。
python scripts/try-fy-canvas.py --fy-style X17 --output .data/fy-blueprint-new --chrome "C:/Program Files/Google/Chrome/Application/chrome.exe"

# 真正的智能体生成入口：会调用已配置的文本/代码模型；这里仅省去配音。
python scripts/make-chapter.py --engine fy-canvas --fy-style X17 --content "解释 MoE 专家路由" --output .data/fy-moe-new --no-tts --chrome "C:/Program Files/Google/Chrome/Application/chrome.exe"
```

画风选择要求明确编号，避免同名歧义。`--fy-style-root` 或环境变量指定根目录，目录中须有
`风格提示词/`。每次重建索引并校验全文哈希；完整原文传入规划、逐场景生成及修复，超过
24,000 字符则明确报错，不截断后冒称全文。输出保留 `style-source.json`、`style-index.json`、
`style-licenses/` 与 `engine.json` 的来源摘要。依赖审计仅提供字面路径提示，
`dependency_audit_complete=false`；没有缺失提示不代表字体、动态引用或效果依赖均已满足。
省略编号时使用项目纸面配色，不声称使用外部画风。

Canvas API 与旧 SVG API 不同：`scene('首句id', ({ctx,t,...}) => { ... })` 每次重画，
不返回更新函数。第一场景可在注册前调用一次 `theme({...})`，完整设置
`paper/card/ink/mute/line/q/k/v/gold/blue`；所选画风要求设置主题，开始绘制后配色冻结。
字幕、系列标签与进度条同步采用主题色。`M` 来自上游 motion-core；`T/TE/K/KE`、`V`、
文字测量适配器来自 SciDirector。场景切点是下一场首句，无自动交叉淡化。

浏览器预检在全章 33 个均布时刻与逐句阅读时刻测量实际 Canvas 文字，检查字号、边界、
文字重叠与运行异常；倒序重复 seek 比较真实像素。静态及浏览器问题最多修复三轮，仍有问题
则停止编码。这是采样检查，不能保证所有中间帧都正确，也不覆盖任意图形相撞、关系线语义、
美感或内容事实。当前没有接入自动 VLM 审片、Live2D、Go/REST/前端及 fy 完整创作流程，
暂不接受 `--reuse-audio`。TTS 沿用原实现，本阶段没有改善音色或自然度。

本机样片 `.data/fy-canvas-x17/chapter.mp4` 是 15.42 秒、1920×1080、24 fps 的手工无声试验，
时间轴为估算。它演示 X17 的蓝底白线、黄色重点、制图网格、尺寸线、编号标注与图签；
原文指定的字体以本机字体替代。它只是候选，不代表用户选定风格，也不证明智能体已能自动
生成同等质量。后续应使用同一口播稿做真实生成对比。

许可随来源保留：fy 组件采用原作者免费使用/分享条款；341 库总体为自定义非商业许可，
其中 L/V/X 系列另适用原 MIT 条款。外部库不随本项目或 wheel 分发，分享输出时保留其
来源说明与 `style-licenses/`。详细范围以各原文为准。

章节模式以整章旁白为时间轴，让同一对象跨句连续变化。编剧、旁白、场景代码生成、浏览器预检修复及整章渲染已在 `scripts/make-chapter.py` 串联；它仍是独立的 Python CLI 工作流，尚未接入 Go/REST 任务入口与前端审核台。

## SVG 后端流程

1. **编剧**（`chapter/writer.py`）：原始内容生成 `ChapterScript`，包含场景、逐句旁白、画面意图和示意数值。脚本审读与长度校验按原有预算修订。
2. **旁白先行**（`chapter/narration.py`）：整章旁白一次送 Fish `/v1/tts/with-timestamp`，用序列比对把逐字时间戳对到每句，生成 `timing.json` 的句子 `start/end` 与逐字 `times`；兼容“2017”读作“二零一七”的规范化。片头留白 1.0 秒，片尾停留 1.8 秒。无 TTS 时采用估算时间轴。
3. **场景代码**（`chapter/writer.py`）：逐场景生成 SVG 构建函数及 `return t => ...` 更新函数，引用当前旁白时间与 `V` 中的章节数值。编译后以章节时间调用 `__seek(t)`。
4. **检查与修复**（`chapter/workflow.py`）：静态/关键词检查和浏览器预检回传场景定位，在既有预算内修复。致命问题耗尽预算后报错，其他残留问题返回警告；生成完成不等于画面已获人工认可。
5. **整章渲染**（`chapter/compile.py`）：复用 `html_capture` 逐帧截图，单次交付编码，按时间轴混入整章旁白。

受信任运行时 `chapter/chapter_runtime.js` 提供：

- 时间锚：`T(id)` 句首、`TE(id)` 句末、`K(id, '关键词')` 说到该词、`KE` 说完该词；不存在的词立即报错。
- 缓动与 SVG：`P / Wn / Ez`、`S A O TR TRS txt drawPath DP`。
- 组件：`chip rowXs panel heading arcD/arcPt flowDots vecRow matrix bars pop`。
- 框架：场景交叉淡化、系列标签、进度条和关键词着色字幕。

## 原型与预检

`chapter/examples/attention.{py,js}` 是当前运行时的原生示例：11 句、约 61 秒、两个场景（“它”指代、点积与缩放），数值为示意（d = 64）。

```powershell
python scripts/render-attention-chapter.py                # 真实 Fish 合成并渲染
python scripts/render-attention-chapter.py --estimate     # 无 TTS 的无声预览
python scripts/render-attention-chapter.py --reuse-audio  # 复用已合成旁白，只改画面
```

`preflight_chapter` 在多个时刻 seek，收集运行异常、关键词缺失与布局问题，并倒序重放检查状态确定性。它不能替代对机制解释、运动观感和配音自然度的实际审片。Live2D 尚未叠加；字幕由页面绘制，未来接入 Go 流水线时须避免重复烧录字幕。本阶段没有改动 TTS 音色或自然度参数。

## 旧成品参考入口已移除

不再读取或注入旧成品源码，也不再提供参考包导入、检索与选取记录。旧 `--reference` 参数已删除，传入时由 CLI 明确报错。fy 的画风原文仍按上文通过 `--fy-style` 选择；用户原成品与既有输出留存。
