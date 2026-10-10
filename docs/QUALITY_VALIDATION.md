# 质量升级验证（2026-10-10）

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
