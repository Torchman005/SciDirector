# 章节模式（原型）

参考：`E:\videos\科普视频\agent\3\attention_video` 的手写成片。它与逐镜头流水线的差别不在参数，而在生产方式：
整章一条旁白、画面以旁白时刻为锚点、对象在句与句之间连续变化。本模式实现其中的第 1–3 步；
第 4 步（由模型生成导演代码）尚未实现。

## 流程

1. **旁白先行**（`chapter/narration.py`）：整章旁白一次送 Fish `/v1/tts/with-timestamp`，
   用序列比对把逐字时间戳对到每句（兼容服务端把“2017”读成“二零一七”的规范化），
   生成 `timing.json`：每句 `start/end` 与每个字的 `times`。片头留白 1.0 秒，片尾停留 1.8 秒。
2. **章节级场景**（`chapter/compile.py`）：一章一个可 seek 的 HTML，`__seek(t)` 用章节时间；
   截帧复用 `html_capture`，单次交付编码（与 Go `deliveryVideoArgs` 一致），旁白按留白对齐混入。
3. **受信任运行时**（`chapter/chapter_runtime.js`）：
   - 时间锚：`T(id)` 句首、`TE(id)` 句末、`K(id,'牛奶')` 说到该词、`KE` 说完该词；找不到的词立刻报错。
   - 缓动：`P / Wn / Ez`；SVG：`S A O TR TRS txt drawPath DP`。
   - 组件：`chip rowXs panel heading arcD/arcPt flowDots vecRow matrix bars pop`。
   - 框架：场景交叉淡化（含轻微模糊）、系列标签、进度条、关键词着色字幕。

## 预检

`preflight_chapter` 在 40+ 个时刻 seek：收集脚本异常与 `K()` 找不到的词，
并倒序重放比对可见场景，确认画面只由 t 决定。

## 原型与运行

`chapter/examples/attention.{py,js}`：11 句、约 61 秒、两个场景（“它”指代、点积与缩放）。

    python scripts/render-attention-chapter.py              # 真实 Fish 合成 → .data/chapter-attention/chapter.mp4
    python scripts/render-attention-chapter.py --estimate   # 不调用 TTS 的无声预览
    python scripts/render-attention-chapter.py --reuse-audio  # 复用已合成旁白，只改画面

## 已知限制

- 未接入任务流水线、审核与修复台账；Live2D 讲解员未叠加（字幕已为右下角让位）。
- 字幕由页面绘制；接入流水线时需关闭 Go 侧字幕烧录，避免重复。
- 数值为示意（d = 64），面板已标注。
