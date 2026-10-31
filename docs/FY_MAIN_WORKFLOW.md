# 网页任务使用 fy 画风库

此前 `make-chapter.py --engine fy-canvas` 只接入独立 CLI，网页仍提交 anime/paper/paper_stage。安装库文件本身不会改变网页任务。这次把实际主链路也接上了。

## 使用

1. 更新 AI、Go API/worker 和前端，确认空闲后协调重启。Windows 使用仓库根目录的 `scripts\dev.bat stop`、`scripts\dev.bat start`（start 会重编 Go）。刷新网页。
2. 动画演出默认是「fy 画风库动画」，默认编号 X17。可搜索编号、名称、视觉特征和适用内容，查看完整原文与来源。
3. 等待「原文已加载」再生成。配色/背景由画风原文决定，原预设不会参与请求；后期调色仍会改变成片颜色，需保留原画风时选择「不调色」。
4. 任务头、分镜引擎和任务概览展示所选编号；概览提供原文 SHA-256，修复继续使用已保存快照。

旧任务保持原模式，不会因为默认值变化自动重渲染。当前运行的旧 API 返回 404 时，前端明确提示重启，不把失败当成已应用画风。原模式仍可选择。

## 服务契约

- `GET /api/v1/styles/fy` 返回 `entries`、`prompt_count`、`library_version`、`repository`、`license_note`。条目含 id/name/category/look/fit/keywords（部分可选）；由 Python 固定读取器重建索引并验证原文件。
- `GET /api/v1/styles/fy/:id` 返回完整来源：id/name/content/path/sha256/library_version/library_commit/repository/attribution 与依赖提示等。依赖提示不是完整审计，也不触发自动安装。
- 上述 REST 调用 `GetFyStyles(FyStylesRequest{style_id})`。空编号返回 `catalog_json`；明确编号返回 `source_json`，不同时返回两者。不接收客户端路径。
- 新建请求的 `style_guide` 只需 `{"animation_style":"fy","fy_style_id":"X17"}`。`fy_style_source` 为内部字段，REST 拒绝客户端提供。Go 在模型调用/入队前解析来源，保存到 Job.StyleGuide；不可用返回明确 503，不创建替代任务。缺少编号、类型错误、非 16:9 或分镜图片返回 400。
- 主流水线与单镜 RPC 通过原 `style_guide_json` 传完整快照。Python 校验编号、全文与 SHA-256、来源和版本；fy 校验错误映射 INVALID_ARGUMENT，不能沿用历史“损坏风格回默认”的行为。非 fy 的历史兼容保留。
- 导演统一派生 `engine=motion`，meta 保留 generation_mode=fy-canvas、画风编号与哈希。专用 coder 只生成 Canvas 场景，编译时注入固定上游运行时；旧 SceneSpec 和范例不参与。源码中的 `scid-fy-code` 保存模型场景供修复，`scid-fy-source` 保存来源摘要。
- 主渲染继续走 HtmlRenderer/SandboxRunner，不调用独立章节的浏览器直出路径。主预检增加约 33 个均布时刻及倒序像素比较，和原安全/预检共用最多一次生成内修复。初始化或绘制错误会在 seek 时明确失败，关闭预检也不会以缺主题的默认纸面配色静默出片。

## 安装与部署

外部库须包含 `风格提示词/`。本仓库默认位置为 `.data/integrations/code-animation-341-styles`，相对路径按仓库根解析，不随 `cd ai` 漂移。也可通过进程环境 `FY_STYLE_LIBRARY_ROOT` 指定根目录；安装为 Python wheel 时应显式配置绝对路径。Go 不需要直接挂载或读取原库。

Compose 的 AI 服务将该目录只读挂载为 `/opt/fy-styles`，容器内固定使用该路径，不传宿主 Windows 路径。库不随 Git 或 wheel 分发；另一台机器必须另行安装，并保留上游许可/来源。

fy 上游组件固定提交 `d9a545afa393b2ffab4f36e87371691daf8e502c`。本机画风库提交 `01576924a2b316d1f11ae33f75d3c005e8781451`，341 份提示词；X17 原文 SHA-256 为 `f651c31fd400e9a2fcb9b6c074636c43b77a86ef40f362c82bbbeba03fca850b`。实际任务以提交时的快照为准。

## 能力边界

支持 16:9 的实际渲染尺寸，主链路不绘制章节字幕/图签/进度条；Go 合成器继续负责字幕、音乐和讲解员。讲解员局部矩形参与文字实测避让。fy 当前仅使用 Canvas 2D，自带分镜图片编排、WebGL、外部脚本、字体、音视频不自动迁入；前端保留原模式用于图片编排。

选定原文会指导构图、材质、排字与运动，不能保证模型完全复现示例或自动消除连线语义/组件相撞。Canvas 预检实测文字，任意图形遮挡与科学表达仍依赖原审片及有界修复。主链路词锚按单镜时长比例估算，没有改成章节 CLI 的逐字 TTS 先行，也没有调整语音服务商、音色或自然度。

整库采用上游自定义非商业许可，L/V/X 系列保留 MIT。原文与引用依赖的具体范围以库内 LICENSE/NOTICE/第三方许可为准。
