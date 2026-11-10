# 自动画风与多服务商配图

## 范围

脚本提交页默认按脚本用词匹配已安装画风库的 fit、keywords、name、look，按区分度与字段权重排序；这是确定性的本地检索，不是 LLM 对语气/内容的深度理解，不调用模型、不额外计费。匹配结果展示依据，允许关闭自动匹配或直接手动选择。脚本变化、手动覆盖和取消会使旧结果失效；自动状态未匹配或原文未就绪不能提交。无匹配不冒充默认推荐。提交依旧仅送 ID，Go 重新解析完整画风快照并落库。

配图为显式的一次一张请求，服务商选择、模型、尺寸与确认费用后才调用。生成后先预览，再勾选使用，不随脚本变化或视频提交隐式生图。失败、超时、限流均不自动重试，不切换其他付费服务商；超时不代表上游未计费。当前没有生图价格估算、自动提示词规划、编辑/参考图、异步任务、URL 图片下载或付费请求幂等重放。

## 配置

配置均在 Python 服务端，见 `.env.example`：

```dotenv
SCID_IMAGE_PROVIDERS_JSON='[{"id":"relay","label":"我的中转站","base_url":"https://your-relay.example/v1","model":"EXACT_MODEL_ID","response_format":"b64_json","sizes":["1024x1024"]},{"id":"official","label":"另一个服务","base_url":"https://your-provider.example/v1","model":"EXACT_MODEL_ID","sizes":["1024x1024"]}]'
SCID_IMAGE_API_KEYS='{"relay":"NEW_KEY","official":"NEW_KEY"}'
SCID_IMAGE_TIMEOUT_SEC=180
SCID_IMAGE_MAX_PARALLEL=2
```

每个服务独立地址、模型、密钥和尺寸列表；当前协议为 `openai_images`，向 `base_url + /images/generations` 发起 JSON 请求：model/prompt/n=1/size。response_format 可设 b64_json，或省略以兼容不接受该参数但返回 Base64 的模型。必须返回 `data[0].b64_json`；返回 URL 或异步任务时明确报协议不兼容，不访问不受信任链接。并非任意厂商的原生协议均支持。

Vankit 用户提供的根站为 `https://sub.vankit.top`，本次未获取到可核对的接口文档，`/v1` 兼容路径及“gpt image2.5”的准确模型别名、尺寸、response_format 是否受支持仍需文档或单独联调，不能视作已验证。示例配置不代表其真实能力。聊天中出现过的密钥应撤销换新，不能放入代码、日志或提交，也不能复用文本模型的密钥作为隐式后备。

密钥不送前端、任务、checkpoint 或素材元数据；服务列表仅返回 ID、标签、协议、模型、尺寸和是否已配置。错误不回传上游响应正文、认证头或完整请求。HTTP 环境代理不自动继承。运行服务仍需协调更新 AI/API/worker 与前端，新 RPC 不兼容旧运行进程；本轮未重启或调用真实生图。

容器前端为生图接口单独等待 320 秒，超过 Go 的 310 秒调用期限和 Python 最长 300 秒配置；禁用代理上游重试。普通 API 的 60 秒代理等待不变。自备反向代理也需保留该等待窗口，不得对生图 POST 自动重放。

## 图片进入视频

生成结果在 Go 提供的共享临时目录保存。Python 完整解码 PNG/JPEG/WebP、保留透明度、剥离附加元数据并转 PNG；Go 复用导入图片的大小、尺寸、完整像素校验后签发租户素材 ID。沿用每张 2 MB、4096 最长边、1200 万像素、每账号 48 张、每任务最多 6 张/8 MB 的限制。保存服务商、模型、可选画风 ID/hash，不保存密钥或原生服务响应。

已有 SceneSpec 图片保持兼容。fy 主链路新增 `asset_ids` 与 `imageAsset(id,x,y,w,h,{fit:'contain'|'cover'})`；只允许已选素材，在 document 对象回调内绘制。编译器按 ID 解析服务端路径并将实际使用图片内嵌，不执行模型提供的路径/URL。每镜内嵌图片 Base64 总量限 5 MB，为既有 gRPC 消息保留空间，超过则明确失败，不悄悄截断或降低画质。

资源解码完成才置 ready；画面纯绝对时间 seek，可重复和倒序。图片沿用对象关键帧、层级、关系线避障与碰撞检查；完整图片矩形作为保守占用区，不做任意透明形状识别。必要文字与关系线仍由引擎绘制，不能假定生成图片中的文字、数据或科学图示正确，仍走原审片门槛。

图片数据进入对象版本哈希；精确修订复核实际素材数据并保留嵌入图，不能顺带换图或改画笔。旧章节模式不开放该图片契约，旧输出不自动迁移。本功能不改变既有任务记录、审核状态、13 个已通过视频或 TTS。

## 接口

- `POST /api/v1/styles/fy/recommend`：`{raw_script}`，20～30000 字，返回 style_id/reason/method/library_version/alternatives。
- `GET /api/v1/image-providers`：公开的非敏感服务列表。
- `POST /api/v1/assets/visuals/generate`：provider_id/prompt/size/可选 fy_style_id/role/description/subject/pose/confirm_cost=true，返回与导入图片相同的素材元数据。
- protobuf 增加 RecommendFyStyle/GetImageProviders/GenerateIllustration；只传请求和共享路径，不经 gRPC 传图片二进制。

离线验证使用模拟生图传输、隔离的真实 HTTP/gRPC、临时素材库和真实 Chromium，不调用付费接口、不连接或清理用户 Redis。

2026-11-10 离线复核：Python 相关回归 210 项、前端 60 项通过，前端生产构建通过；Go 素材/AI/配置包和隔离的生图 HTTP/gRPC 测试通过。`scripts/verify-visual-preparation.py` 验证手动覆盖、费用取消/确认、生成期间禁提交、生成图片解码、显式选用和手机端无横向溢出，证据保存在 `.data/previews/visual-preparation/`。Docker/Nginx 未在本机启动验证，不等于 Vankit 实际联调或服务已上线。
