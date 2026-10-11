# 质量升级验证（2026-10-10）

## 2026-10-11 字幕与缺片修复

- Fish 实际返回 30 条词/字时间戳，FFN 为 4.24–4.80 秒、下一句 5.92 秒；Go 字幕按原稿字符对齐，合成保留全部旁白。相关 38 项 Python 及 Go 字幕测试通过。
- Go `go test ./internal/... -count=1` 全部通过，包括禁止缺片放行/合成、满幅右下透明叠加及音频时长检查。
- 科学图标/网格/信号路径、旧布局重建、重做配音、Fish 和 gRPC：29 passed / 1 skipped。离线流程演示通过真实 Chromium 遮挡/裁切/倒序重复 seek 检查，产物 `.data/process-demo/process-demo_motion.mp4`。
- 前端构建、18 项状态测试和审核台浏览器验收通过。旧镜头需重做才能应用新安全区；真实六镜头任务恢复仍在继续，尚未据此宣称全部恢复或批量通过率。

## 2026-10-11 程序升级复验

- Python 全量：822 passed / 30 skipped，281 秒；包含新材质、共用时间轴、重叠拦截、立绘 alpha 真实编码及倒序一致性。Go `go test ./internal/... -count=1` 全部通过。
- 新场景相关 57 项、材质/背景 11 项通过；生成了 1600×900 / 10 秒 FlashAttention 演示，真实浏览器预检没有遮挡/裁切问题。
- 立绘/Live2D 9 项通过，带/不带口型图片分别验证；Go 立绘安全包校验、租户隔离、HTTP 导入预览验证通过。
- 前端 tsc、18 项状态测试、生产构建通过；Chromium 导入/失败恢复/四状态预览/390px/键盘/视频检查通过。构建仍有既存的大包提示。
- DESIGN.md lint 为 0 errors / 0 warnings。Premium 静态审计仍报告此前的 18 个 JSX 解析误报（见下文）；实际 DOM 和交互检查通过。
- `.data/live-attention-review` 实际文本/视觉审核一轮修订：0.6925 → 0.8825，通过；改动 compute/name_O/output_label/tile_O，两项 major 有当前证据并关闭。初审仍有不准确的目测字号推断，不能以单例宣称批量通过率；历史任务 Redis 记录不可用，未计算生产通过率。
- 据此调整审核证据后，`.data/live-attention-grounded` 再测：初审明确定位“必须反复写回”与旁白“避免写回”的科学矛盾，并使用真实 32px 字号；0.6475 → 0.8985，一轮修订通过，logic/pacing 两项 major 均依据当前图像关闭，只保留非阻断的 advisory。修订仅涉及 name_O/output_O/output_label。相关 88 项回归通过；未降低门槛或移除科学检查。
- 本轮没有调用生图；立绘测试使用几何 PNG 夹具，仅验证程序，不作为角色画稿交付。

## 已执行

- 最终全量 `python -m pytest ai/tests -q`：**807 passed / 30 skipped**，221 秒；包含真实 Chromium、FFmpeg 和 gRPC。修正 Windows 中使用 shell 内建 echo 的探针、明确 Linux 专属用例条件，以及 PNG/JPEG 断言格式；保留不可隔离时必须拒绝执行的负向测试。
- 最终 `go test ./internal/... -count=1`：所有有测试的包通过，包含真实 FFmpeg 媒体测试；新增模型故障入队保护后 HTTP 包再次通过。
- 审核/修订回归：87 passed / 6 skipped；覆盖空补丁拒绝、元素保留、当前版本规格、定向证据、修复任务账本与熔断。
- 二次元场景：67 passed（含真实 Chromium）；固定反射定律样例渲染为 8 秒视频，目视检查角色、文字及几何。
- Live2D：官方 Hiyori 模型真实透明 VP9 渲染，实际 TTS 驱动；顺序/倒序 seek 截图一致。Go 真实 FFmpeg alpha 叠加和时长检查通过。
- `go test ./internal/assets ./internal/domain ./internal/httpapi ./internal/worker ./internal/ai`：通过。HTTP 补充测试以独立本地 Redis 执行，12 条任务路由的跨租户访问均与不存在任务返回一致，Range / 409 版本保护 / 文件清理 404 通过。
- Python presenter / service-health / gRPC 组合：31 passed / 1 skipped（修正 Windows gRPC 时钟容差后相关两例单独复跑通过）；另有 pbconv 契约测试通过。
- `npm --prefix web run lint`、`npm --prefix web test`：通过，18 项前端状态测试。生产构建通过；原有包体大于 500 kB 提示仍在，不影响构建。
- `python scripts/verify-quality-ui.py`：通过。真实 Chromium 验证必填、Select 键盘和 Escape、上传忙碌锁、替换失败保留模型、提交失败保留表单、创建成功/深链接、当前媒体/问题关闭状态、视频拖动、390px 窄屏、弹窗 Escape、媒体失败入口、减少动态效果，以及无浏览器异常。API/WS 使用独立夹具，真实后端行为由 Go 集成测试覆盖。
- `npx -p @google/design.md designmd lint DESIGN.md`：0 errors / 0 warnings。

## 真实模型修订闭环

`python scripts/verify-review-live.py --live --output .data/live-review-final` 执行成功（退出码 0）。
文本为 DeepSeek `deepseek-chat`，视觉为百炼 `qwen3-vl-plus`，实际渲染 1280×720 / 8 秒。
参考镜头故意把反射定律写成「入射角 ≠ 反射角」。初审判负 0.6175，定位到 `angles`；
结构化修订只改变该元素，其他图形和角色保持。复审 0.9175、passed=true，
原 blocking 任务 `r1-01` 标记 resolved，引用当前第 5 帧和配对 AFTER 图像 7 中的正确等号。
完整本地记录：`.data/live-review-final/result.json`，前后视频位于同目录的 before/after。

本轮实测也发现并修复了旧视觉模型 404、历史 BEFORE 图污染当前评分、遗漏逐项验收结果三类问题。
修复依靠模型配置、图片版本标签和有界澄清，不降低科学准确性阈值。

## 静态 UI 审计结果

执行技能 `audit_project.py . --mode strict`。它报告 18 个 JSX 解析误报：将 Ant Design `Form.Item` 识别为原生 form、在 `Form<SubmitForm>` 泛型处截断标签而漏读现有 noValidate，以及将 Ant Design `Select` 当成原生 select。逐项核对源码与浏览器 DOM：真正的 form 使用 noValidate；Select 弹层属于 antd，已执行键盘与窄屏检查。没有为消除误报给 Form.Item 塞入无效属性或虚报原生控件归属。原始审计结果保留于 `.data/ui-verification/premium-audit.json`。

`UX-CONTRACT.md` 记录组件所有者和业务来源。搜索应用源码未发现原生 alert/confirm/prompt 或空 hash 链接。实际发现的 reduced-motion 加载图标残留已通过 ConfigProvider 的 motion token 修复。

## 结论边界

上述证明修订链路和媒体功能可运行，不能据此宣称任意脚本的审核通过率已提高某个百分比。尚无固定真实模型基准集的前后统计。模型供应商输出、复杂镜头表现和导入模型兼容性仍需按实际素材验收。

Windows 无 Linux namespace/seccomp 时，相关 Linux 隔离测试不可覆盖；SDK Core 和模型属于本地验证资产，未提交。Live2D 是音量驱动开合，不是逐音素嘴形生成。浏览器验证范围不等同于全设备或读屏认证。
