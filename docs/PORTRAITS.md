# 多状态立绘讲解员

创建任务页 →「画面讲解员」→「多状态立绘」→ 导入 ZIP。素材由服务端保存，不需要手动复制到项目代码里。
本次没有调用生图，也没有附带替代角色画稿；请使用你已有或之后绘制的同一角色立绘。

ZIP 中放入一个 `portrait.json` 和四张 PNG。所有 PNG 使用相同画布尺寸（不超过 4096×4096），
建议透明背景、角色脚底和头部在相同位置。文件引用相对 JSON 所在目录。示例：

```json
{
  "version": 1,
  "states": {
    "neutral": {"image": "neutral.png", "mouth_open": "neutral.open.png"},
    "explain": {"image": "explain.png", "mouth_open": "explain.open.png"},
    "think": {"image": "think.png", "mouth_open": "think.open.png"},
    "surprised": {"image": "surprised.png", "mouth_open": "surprised.open.png"}
  },
  "cues": [
    {"time": 0, "state": "neutral"},
    {"time": 0.12, "state": "explain"},
    {"time": 0.45, "state": "think"},
    {"time": 0.65, "state": "surprised"},
    {"time": 0.8, "state": "explain"}
  ]
}
```

- `time` 是整部成片的比例（0～1），严格递增，最多 64 项。上例 0.45 表示在成片 45% 处切换到思考。
- 可省略 `cues`：有语音时讲解，停顿时待机；不会凭音量猜测思考或惊讶。
- `mouth_open` 须四种状态全部提供或全部省略。提供时根据实际 TTS 音量切换开合，属于双帧口型，不能表达完整音素。
- 张嘴图除嘴部外应与对应原图对齐。省略口型图时仍可切换表情，但界面和合成记录明确报告无口型。
- 需要启用 TTS、可用 Chromium 与 FFmpeg；立绘不需要 Cubism SDK。Live2D 的安装要求保持不变。
- 成片将科学主图完整缩放，为右下角人物留出区域；字幕在最上层。

素材按租户保存在服务端媒体目录的 `assets/live2d/<租户哈希>/<素材ID>.portrait/`（实际前缀依部署媒体目录）。
只通过 API 返回的 `asset_id` 引用；客户端路径不能覆盖服务端资源。上传限制 100 MB，解压限制 256 MB，包内仅接受 PNG/JSON。

接口：`POST /api/v1/assets/portraits`（multipart 的 `file`），返回 `asset_id/filename/kind/states/lip_sync`；
预览：`GET /api/v1/assets/portraits/:id/:state`，state 为四个英文状态之一，读取时验证租户归属。
