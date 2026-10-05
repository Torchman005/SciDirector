# HTML-first 场景规格

HTML 引擎（d3/echarts/code_anim/motion）默认让模型生成内部 `SceneSpec v1`，固定编译器产生 HTML/SVG。
引擎名称及 Go/Python proto 没有改变：跨语言继续传 `code` 和 `language=html+js`。
GenerateShot、ReviseShot 和流水线均复用 Coder 入口。

模型负责对象、内容、布局和时间轴；系统负责 DOM、字体、图表组件和 `window.__seek(t)`。
编译后的 HTML 含 `scid-scene-v1` 不可执行 JSON。重做回读规格及元素 id，不回灌固定运行时代码。
旧代码和片段渲染、抽帧证据、重试上限、人工审核、并行截图继续使用现有链路。

## 配置与兼容

`SCID_CODER_SCENE_MODE=structured` 是新镜头默认值，已透传 Docker Compose。
设为 `code` 可使用原 HTML 提示词；单镜头 `meta.generation_mode=code` 是特殊镜头出口。
历史自由 HTML 的修订自动保留代码路径，避免悄悄丢失现有复杂画面；结构化产物的修订按当前配置走规格路径。
MATH 默认继续 Manim；需要 LaTeX、复杂数学、3D、复杂曲线/角色/交互时选择原引擎或显式 code。
不自动用标题卡替代不能表达的场景。模型明确返回旧 HTML 包络时经 HTML 契约和浏览器门禁兼容，并记录日志。

## SceneSpec v1

对象：`version:1 / background / elements / explanation`。每个元素包含稳定唯一 id、kind、box、内容与 keyframes。
box 的 x/y/width/height 为画布比例；font_size 是实际成片像素。

| kind | 内容 | 固定能力 |
| --- | --- | --- |
| text | text、align | 自动换行文字 |
| card | text、align | 带边框底板的文字卡片 |
| code | text | 等宽纯文字、按 Unicode 字符逐步呈现；本版不含语法着色 |
| bars | data=[{label,value}]、unit | 非负横向柱图、共享比例、标签和单位、生长 |
| rect / circle | color | 基础几何形状 |
| line | color、arrow | 线段及可选箭头 |

颜色只能 text/primary/muted 或 #RRGGBB。风格和字体来自现有 StyleGuide；正文不插入 HTML。
background 支持既有 solid/gradient/grid/vignette/noise/scanlines 预设；StyleGuide 的明确选择优先，auto 时由场景选择。
keyframes 使用归一化 time，必须从 0 严格递增；每帧完整状态为 opacity/dx/dy/rotation/reveal。
未写字段恢复默认值，不继承上一帧。easing=linear/smooth/step 描述到达该帧的方式。
reveal 只控制 code 打字、bars 生长。最后一帧后定格，阅读停留不强制制造无关变化。
旋转仅允许几何图形；复杂图案可使用多个几何元素，但没有任意 JS/SVG path 逃逸字段。

最多 48 元素、每元素 24 关键帧、柱图最多 8 行。未知字段、不支持组件、非法颜色、非有限数值、
重复 id、无单位/负数据、可见对象越界、旋转包络、字号不足、文字区域过小和末帧空白会被拒绝。
6% 边距为布局建议；透明入场和首帧淡入允许建立场景。关键帧可在 1 结束，编译器自动预留末尾 0.5s（短镜头 10%）阅读停留。
实际文字宽高与重叠仍要浏览器测量：换行会保留完整内容，溢出由现有预检发现，不缩字号或省略正文来假装通过。
CSS 无自主动画；每次 seek 都重设完整状态，支持倒序、重复、并行页面从中间时刻开始和局部重渲。
草稿视口缩小通过舞台缩放实现，保持 authored layout。

## 生成与审核预算

一次场景生成 + 最多一次生成内修复，解析/结构/布局/浏览器缺陷共享此预算。
Scene 调用将 LLM 的内部 JSON 解析重试设为一次，避免解析修复和布局修复相乘；网络传输重试仍沿用客户端配置。
场景独立输出预算 `SCID_CODER_SCENE_MAX_TOKENS` 默认 8192（Compose 同步透传），审核等调用仍走通用预算。
生成及规格回灌省略默认字段；关键帧省略值仍恢复默认，不继承上一帧。模型报告 `finish_reason=length`
时明确标记为输出截断并回灌精简要求，不把未完成 JSON 自动补齐或从中提取内部 elements 列表。
单次客户端解析预算与一次场景生成中的两次调用分开计数，最终门禁错误报告实际场景生成次数。
失败把实际错误和上一份规格回灌，第二次仍失败则 policy_ok=false，流水线跳过渲染并按现有规则重试或转人工。
浏览器不可用明确记录原因，继续完整渲染/审核路径；不伪造预检成功。
自由代码分支保持现有 RAG，规格分支使用组件指南和 JSON Schema，不混入 HTML 代码范例。

## 验证与实际效果

测试包含输入负例、脚本终止标签注入、规格回读、修订定位、共享修复预算、历史代码兼容、Manim/mock、
执行真实 JS 的重复/倒序/独立页面中段播放，以及 Chromium 文字、卡片、代码、柱图及超长文字负例。
出片回归修复另验证 **235 passed、6 skipped（浏览器单独运行）、2 deselected**；
真实场景测试 **41 passed**，包括首次全透明入场及 time=1 完成的场景通过生产 HtmlRenderer 并行截图、
ffmpeg 编码、ffprobe 校验，产出 3s、1920×1080 MP4；串行镜头的代码与错误也有隔离回归。
本轮相关 Python 回归 **233 passed、5 skipped（浏览器用例单独运行）、2 deselected（真实全流水线）**。
另在获准的本地环境运行场景及客户端集成 **57 passed**，包含 5 项真实 Chromium 用例和本地假模型 HTTP 服务测试。
Go config/worker/pbconv、Python compileall、git diff --check 通过。
全量回归尚未通过：Windows 缺少 `sh` 导致 load-env 用例失败，普通沙盒的本地 HTTP 连接也不可用（客户端用例经获准执行已通过）。
全栈容器和真实模型生产任务的首稿通过率尚未测量。
本次改动减少模型生成的可执行代码量，但不保证科学内容正确或所有镜头首稿通过，VLM 审核仍保留。

运行 Python 用例：`python -m pytest tests/test_scene.py tests/test_coder.py tests/test_generation_quality.py`。
真实浏览器用例需设置 `SCID_CHROME` 为本机 Chromium 可执行文件路径。
