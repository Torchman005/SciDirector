# Live2D 讲解员

创建任务时展开「Live2D 讲解员」，上传模型 ZIP，然后正常生成视频。角色出现在成片右下角，嘴部随最终 TTS 旁白的音量开合，静音时闭嘴。BGM 不参与口型计算。已有无讲解员任务的行为不变。

## 启用运行环境

1. 从 [Live2D Cubism SDK for Web](https://www.live2d.com/sdk/download/web/) 获取官方 Core，按官方许可使用。若已有解压后的 SDK，可在 PowerShell 执行：`./scripts/install-live2d-core.ps1 -SdkRoot 'D:\itJinYu_toolkit\CubismSdkForWeb-5-r.5\CubismSdkForWeb-5-r.5'`。脚本只复制 `Samples/TypeScript/Demo/public/Core/live2dcubismcore.min.js` 与其许可文件到 Git 忽略的 `.data/live2d-core/`，并核对 SHA-256。Core 和模型资源不随仓库分发。
2. 本地运行 `npm ci --prefix ai/live2d-runtime --ignore-scripts`；Docker AI 镜像已安装锁定的 Pixi 6.5.10 和 pixi-live2d-display 0.4.0。
3. 本地 `.env` 的 `SCID_LIVE2D_CORE_PATH` 设置为 `.data/live2d-core/live2dcubismcore.min.js` 的**宿主绝对路径**，并启用实际 TTS 提供者。Docker Compose 自动将该目录只读挂载到 AI 容器 `/opt/live2d-core/`，容器变量固定为 `/opt/live2d-core/live2dcubismcore.min.js`；Windows 宿主路径不会传入 Linux 容器。Go worker 与 AI 仍通过原有 `media-work` 卷共享媒体。
4. 重启 AI / API / worker；`Health` 能力应含 `presenter:live2d=ok`。这表示依赖文件和 TTS 配置就绪，具体模型兼容性在实际渲染时校验。浏览器需要支持 WebGL，FFmpeg 需要 libvpx-vp9、libx264。

可选配置：`SCID_LIVE2D_RUNTIME_DIR` 指向安装好的 node_modules；`SCID_LIVE2D_TIMEOUT_SEC` 默认 1800 秒。渲染最多 30 分钟、60fps，角色图层最大 1024px。渲染速度取决于 CPU/WebGL 环境，超时会明确报错。

## 模型包

- 一个 Cubism 3/4 `.model3.json`，其引用的 `.moc3` 及 1～16 张 PNG/JPEG 纹理；保持相对目录结构。纹理最大 8192px。
- ZIP 最大 100 MB，展开最大 256 MB，最多 256 个条目。拒绝路径穿越、链接、大小写重复、Windows 设备名与可执行脚本。
- 打包工具重复写入相同的目录记录不会影响导入；重复文件或仅大小写不同的路径仍会被拒绝，错误会指出冲突的 ZIP 内路径。遇到此错误时，从原始模型目录重新压缩，不要把多个模型包合并到一个 ZIP。
- 仅提取核心模型、纹理、参数分组和布局；模型动作、音频、物理配置及表达式文件不运行。使用程序化眨眼、呼吸和轻微头部运动，确保任意时间跳转可复现。
- 默认读取模型 `Groups → LipSync → Ids`，缺失时使用 `ParamMouthOpenY`。特殊模型可填写自己的嘴部开合参数并调整强度（0.2～3）。参数不存在会明确失败。

## 画面与口型边界

主图完整缩至原尺寸的 76%，为右侧角色栏和底部字幕留白，不裁切科学图形；字幕在角色之后叠加。字号也会随主图缩小，建议用 1080p 输出并适当增大场景字号。角色图层单独生成，不会改变数学/图表坐标关系。

口型采用 50Hz 音量包络、噪声门限、归一化及开合平滑，并对齐包含镜头转场的最终纯旁白轨。它是**音量驱动开合**，不是逐音素识别，不会自动生成不同元音的唇形。依据：[Cubism 音量口型说明](https://docs.live2d.com/en/cubism-sdk-manual/lipsync/)。

导入只校验文件结构、纹理和 moc3 文件头，真实 SDK 对模型版本/参数的校验在渲染时完成。SDK 5 Core 与锁定显示库对绘制顺序的接口不同；运行时只对**不含离屏绘制对象**的传统模型做兼容映射，遇到 Cubism 5 离屏绘制特性会明确拒绝，避免输出缺层的画面。其他 Cubism 5 新特性尚未承诺。模型可用性、视觉质量及使用权由实际模型决定。

## 验证

测试覆盖 ZIP 安全边界、租户隔离、客户端路径丢弃、RPC 截止时间和错误映射、语音/静音包络、透明层合成及成片时长。真实验证使用官方 Hiyori 示例与实际 TTS 音频，成功生成透明 VP9 并由 Go/FFmpeg 合成；每次渲染前自动验证顺序/倒序跳转截图一致。

2026-10-11：使用用户本地的 Cubism SDK for Web 5-r.5 Core（SHA-256 `8741F739779B5D5210872BD3D7D99F0F1E56E6C87409E7D26D6BB4B80AA1EF47`）与 Hiyori 模型渲染 2 秒 400×600 / 12fps VP9 alpha 层，倒序 seek 校验通过；再用已有真实 TTS 旁白生成 3 秒同规格透明层，口型包络最大值 0.981，尾部音频结束后闭嘴。`.env` 的本地 Core 路径、AI 健康能力 `presenter:live2d=ok`、Compose YAML 和只读挂载声明均已验证；当前环境没有 Docker CLI，因此未运行实际容器。Core 5 与传统模型的兼容映射、旧版绘制顺序字段及离屏模型拒绝均有浏览器回归测试。

部署自检：生成一段包含停顿的短视频，确认讲话时开嘴、停顿闭嘴、BGM 单独播放不触发嘴部、字幕不被遮挡。样例资源仅保留于本地忽略目录，不提交到 Git。

## 上传接口返回 404

Windows 本地启动器过去只在 `backend/bin/scid-api.exe` 不存在时编译，可能运行没有 Live2D 路由的旧二进制。2026-10-11 的实测旧 exe 构建于 10 月 4 日，`POST /api/v1/assets/live2d` 直连 API 和经过 Vite 都返回 404；当时 Gin 的未匹配路由日志还把 `path` 记为空。现已改为启动已停止的 Go 服务前重新编译，并在进程仍运行时提示重启。更新后无模型的 POST 返回预期的 400，完整 Hiyori ZIP 通过网页代理与真实浏览器导入返回 200；未匹配路由日志显示实际 URL。

若更新代码后进程仍在运行，执行 `scripts\dev.bat stop` 再 `scripts\dev.bat start`，或单独重启 Go API/worker。Core 文件可用与否不会让上传路由消失；404 先检查当前 API 二进制及前端代理目标。
