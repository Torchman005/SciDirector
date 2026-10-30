# 章节模式与成品参考库

章节模式以整章旁白为时间轴，让同一对象跨句连续变化。编剧、旁白、场景代码生成、浏览器预检修复及整章渲染已在 `scripts/make-chapter.py` 串联；它仍是独立的 Python CLI 工作流，尚未接入 Go/REST 任务入口与前端审核台。

## 流程

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

## 使用成品参考

本阶段将已有代码作为**可检索设计参考**：编剧看到场景标题和动作标签，导演按当前场景主题检索最多两个代码片段，借鉴对象组织、动作和连续演出。修复调用使用同一章的参考库，以当前问题为优先。没有相关结果时不注入无关片段，也不增加检索模型调用。

支持选择一个含 `index.html` 的项目，或多个网页项目的父目录。仅读取页面标题和 `app.js`、`scenes.js`、`scenes_a.js`、`scenes_b.js`；通过行首 `scene(...)` 及邻近 `SCENE n - 标题` 注释识别场景，截取构建与更新窗口。这不是完整 JavaScript 解析，内联脚本、其他文件名或注册方式需要另外适配；片段也可能缺少上下文。

在仓库根目录执行（或先安装 `ai` 包）：

```powershell
$env:PYTHONPATH = "$PWD/ai;$PWD/.pylibs"
$env:PYTHONIOENCODING = "utf-8"

# 离线建立参考包，无模型调用；输出须为原目录以外的新文件。
python scripts/import-chapter-reference.py --source "E:\videos\科普视频\agent\3" --output ".data/chapter-references/agent-3.json"

# 将参考包用于新章节。此命令会调用已配置的文本/代码模型及 TTS。
python scripts/make-chapter.py --content-file "内容.md" --output ".data/chapter-demo" --target-sec 90 --reference ".data/chapter-references/agent-3.json"

# 也可直接读取原成品目录；输出目录须位于原成品目录之外。
python scripts/make-chapter.py --content "解释 MoE 专家路由" --output ".data/chapter-moe" --reference "E:\videos\科普视频\agent\3" --no-tts --no-video
```

`--no-tts --no-video` 仍调用文本/代码模型并执行浏览器预检，只省略配音和视频编码。省略 `--reference` 则不启用参考。单独导出的 JSON 包可脱离原目录使用；本机已验证的包为 `.data/chapter-references/agent-3-v1.json`，含四个项目、37 个场景，不纳入 Git。

输出目录增加：

- `reference-library.json`：本章使用的参考快照，包含相对源文件、源码 SHA-256、片段行号和标签。
- `reference-selections.json`：编剧及各场景最近一次选取的参考 id；失败退出也尝试保存。它记录选取，不证明模型采纳了设计或成片质量达标。

每章用请求上下文隔离参考，不改变全局默认库。导入上限为 16 个项目、64 个场景、单源文件 256 KiB、所选 JS 合计 2 MiB；JSON 包上限 1 MiB。每个场景最多保留 4,200 字符，单次序列化参考上下文上限 12,000 字符，超过预算明确报错。

## 适配边界

原成品源码不直接拼入输出，也不作为已验证的内容事实。提示词要求按当前主题重写，并处理 API 差异：旧 `scene(svg => ...)` 或数字编号改为当前首句 id，`W` 改为 `Wn`，旧颜色全局变量改用 `C`；旧 `txt` 基线坐标与当前垂直居中约定不同。原辅助函数、旁白 id、数据和时长不能假定可用，必须绑定当前运行时、脚本及 `V`。

不读取或引入原音视频、语音脚本、播放器、Live2D 模型、Cubism/Pixi 或其他第三方运行库。外部注释、字符串和角色标记仅作数据，不能覆盖任务指令。原有检查与修复预算保持不变。

当前来源集中于注意力、FFN 与位置编码，跨领域的匹配能力有限。后续可从人工认可的场景提炼参数化模板和确定性布局约束；本阶段没有实现该模板引擎，也没有通过真实模型生成对比证明视觉质量提升。

## 原型与预检

`chapter/examples/attention.{py,js}` 是当前运行时的原生示例：11 句、约 61 秒、两个场景（“它”指代、点积与缩放），数值为示意（d = 64）。

```powershell
python scripts/render-attention-chapter.py                # 真实 Fish 合成并渲染
python scripts/render-attention-chapter.py --estimate     # 无 TTS 的无声预览
python scripts/render-attention-chapter.py --reuse-audio  # 复用已合成旁白，只改画面
```

`preflight_chapter` 在多个时刻 seek，收集运行异常、关键词缺失与布局问题，并倒序重放检查状态确定性。它不能替代对机制解释、运动观感和配音自然度的实际审片。Live2D 尚未叠加；字幕由页面绘制，未来接入 Go 流水线时须避免重复烧录字幕。本阶段没有改动 TTS 音色或自然度参数。
