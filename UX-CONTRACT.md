# SciDirector 审核台交互契约

界面设计见 DESIGN.md；本文件仅记录既有业务规则的界面后果。

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Select/Listbox | Ant Design Select | web/src/App.tsx、components/ShotDetail.tsx | 中文业务选项；弹层跟随触发器宽度 | 浏览器键盘、窄屏选择 |
| Form | Ant Design Form / Modal | web/src/App.tsx、components/useShotActions.ts | 主表单提交、镜头模态编辑 | 必填、忙碌禁用、失败保留输入 |
| Scrollbar | web/src/styles.css | 全局 CSS 基线 | forced-colors 使用系统颜色 | computed style 与可达底部 |
| Toast | Ant Design App.useApp | web/src/api.ts | 局部上传和媒体失败使用 Alert | 提交/失败反馈 |
| CRUD | API + useShotActions | docs/API.md、backend/internal/httpapi/handlers.go | 创建任务、查看、修改、人工审核；不新增删除任务能力 | 服务端错误恢复、刷新、路由隔离 |

## 业务来源

- 权限：Agent.md、domain/tenant.go，媒体接口先验证任务归属；前端不能用本地路径绕过服务端。
- 状态与生命周期：Agent.md §6、docs/API.md；人工通过/打回由后端决定合法迁移。UI 不能凭分数或意见重复推导通过。
- 模型素材：docs/LIVE2D.md、assets/live2d.go；导入失败保留已选模型；导入期间不能提交依赖它的任务。
- 科学验收：docs/REVIEW.md；先观看当前画面再检查修复清单。当前媒体与历史反馈分别标版本。
- 计费：不增加订阅/付款 UI；现有成本统计读取服务端数据。
- 删除与法律文案：本次没有新增资源删除业务；不使用讲解员仅清除本次表单选择。模型与 SDK 使用范围见官方许可，部署说明链接官方来源。

## 运行时与布局

深蓝单一主题，简体中文，沿用 web/src/theme.ts。主页面自然滚动；镜头表格可横向滚动，展开预览适应内容宽度，证据帧响应式排列。媒体使用原生播放器和带中文名称的图片链接，不自动播放。上传/提交失败可在原表单重试，重复提交有忙碌锁。校验不弹原生对话框。

## 验证记录

本次具体命令、浏览器状态与覆盖边界记录在 docs/QUALITY_VALIDATION.md。
