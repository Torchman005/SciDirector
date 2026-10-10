---
version: alpha
colors:
  primary: '#4F8CFF'
  background: '#0B1020'
  panel: '#0F1730'
  quiet: '#0D1428'
  border: '#1E2740'
  text: '#E6ECFF'
  muted: '#8A94A6'
typography:
  body:
    fontFamily: "system-ui, 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif"
  code:
    fontFamily: "ui-monospace, Consolas, monospace"
rounded:
  control: 8px
spacing:
  page: 24px
  gap: 16px
---

## Overview
SciDirector 是中文科学视频制作与审核工具。界面沿用深蓝审核台，重点是可读的制作设置、
可追溯的修复证据和可靠的素材导入。影片的二次元美术不扩散为后台的装饰。

## Colors
运行时真源为 `web/src/theme.ts` 的 BRAND/SURFACE，经 Ant Design ConfigProvider 应用。
全局 CSS 仅处理布局、媒体及原生控件。新增状态使用 antd 的语义反馈，不只依赖颜色。

## Typography
正文使用系统中文无衬线；源码与路径使用等宽。所有操作与错误用简体中文。

## Layout
1200px 最大内容宽度、24px 页面留白、16px 栅格。窄屏单列；页面自然滚动。
表单从脚本、动画、素材到后期效果；审核先展示结果、再展示问题和证据。

## Elevation & Depth
Ant Design darkAlgorithm 负责层级与浮层，不在单个表单另建主题。

## Shapes
控件与媒体圆角 8px。动画人物是影片里的原创图形组件，不能作为科学数据的替代。

## Components
Select/Listbox、Form、Upload、Modal、Table 归 Ant Design；Toast 归 AntApp.useApp。
Scrollbar 归 styles.css 的全局基线。API 与错误解析归 api.ts，异步状态保留表单输入。
上传失败展示可恢复的错误；上传期间禁止提交依赖未完成素材的任务。
任务创建成功进入任务页并更新 URL，所有素材只通过服务端签发的 ID 引用。

## Do's and Don'ts
保持键盘可达、可见焦点、中文标签、表单错误定位与 reduced-motion。
禁止原生 alert/confirm/prompt、远程脚本随素材执行、隐藏实际失败、用颜色代替文字状态。
