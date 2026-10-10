# Live2D 讲解员

创建任务时展开「Live2D 讲解员」，上传模型 ZIP，然后正常生成视频。角色出现在成片右下角，嘴部随最终 TTS 旁白的音量开合，静音时闭嘴。BGM 不参与口型计算。已有无讲解员任务的行为不变。

## 启用运行环境

1. 从 [Live2D Cubism SDK for Web](https://www.live2d.com/sdk/download/web/) 获取官方 `live2dcubismcore.min.js`，按官方许可使用。Core 和模型资源不随本仓库分发。
2. 本地运行 `npm ci --prefix ai/live2d-runtime --ignore-scripts`；Docker AI 镜像已安装锁定的 Pixi 6.5.10 和 pixi-live2d-display 0.4.0。
3. 设置 `SCID_LIVE2D_CORE_PATH` 为 Core 的绝对路径，并启用实际 TTS 提供者。Docker 可将文件放到 `.data/work/live2d/live2dcubismcore.min.js`，设置容器路径 `/data/work/live2d/live2dcubismcore.min.js`。Go worker 与 AI 必须能读取同一个媒体共享目录。
4. 重启 AI / API / worker；`Health` 能力应含 `presenter:live2d=ok`。这表示依赖文件和 TTS 配置就绪，具体模型兼容性在实际渲染时校验。浏览器需要支持 WebGL，FFmpeg 需要 libvpx-vp9、libx264。

可选配置：`SCID_LIVE2D_RUNTIME_DIR` 指向安装好的 node_modules；`SCID_LIVE2D_TIMEOUT_SEC` 默认 1800 秒。渲染最多 30 分钟、60fps，角色图层最大 1024px。渲染速度取决于 CPU/WebGL 环境，超时会明确报错。

## 模型包

- 一个 Cubism 3/4 `.model3.json`，其引用的 `.moc3` 及 1～16 张 PNG/JPEG 纹理；保持相对目录结构。纹理最大 8192px。
- ZIP 最大 100 MB，展开最大 256 MB，最多 256 个条目。拒绝路径穿越、链接、大小写重复、Windows 设备名与可执行脚本。
- 仅提取核心模型、纹理、参数分组和布局；模型动作、音频、物理配置及表达式文件不运行。使用程序化眨眼、呼吸和轻微头部运动，确保任意时间跳转可复现。
- 默认读取模型 `Groups → LipSync → Ids`，缺失时使用 `ParamMouthOpenY`。特殊模型可填写自己的嘴部开合参数并调整强度（0.2～3）。参数不存在会明确失败。

## 画面与口型边界

主图完整缩至原尺寸的 76%，为右侧角色栏和底部字幕留白，不裁切科学图形；字幕在角色之后叠加。字号也会随主图缩小，建议用 1080p 输出并适当增大场景字号。角色图层单独生成，不会改变数学/图表坐标关系。

口型采用 50Hz 音量包络、噪声门限、归一化及开合平滑，并对齐包含镜头转场的最终纯旁白轨。它是**音量驱动开合**，不是逐音素识别，不会自动生成不同元音的唇形。依据：[Cubism 音量口型说明](https://docs.live2d.com/en/cubism-sdk-manual/lipsync/)。

导入只校验文件结构、纹理和 moc3 文件头，真实 SDK 对模型版本/参数的校验在渲染时完成。默认适配器针对 Cubism 3/4，未承诺 Cubism 5 新特性。模型可用性、视觉质量及使用权由实际模型决定。

## 验证

测试覆盖 ZIP 安全边界、租户隔离、客户端路径丢弃、RPC 截止时间和错误映射、语音/静音包络、透明层合成及成片时长。真实验证使用官方 Hiyori 示例与实际 TTS 音频，成功生成透明 VP9 并由 Go/FFmpeg 合成；每次渲染前自动验证顺序/倒序跳转截图一致。

部署自检：生成一段包含停顿的短视频，确认讲话时开嘴、停顿闭嘴、BGM 单独播放不触发嘴部、字幕不被遮挡。样例资源仅保留于本地忽略目录，不提交到 Git。
