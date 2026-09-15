# 编码智能体 —— 用户消息模板

请为下面这个分镜编写渲染代码。

## 分镜

- 序号：第 {{index}} 个
- 场景标签：`{{tag}}`（渲染引擎：`{{engine}}`）
- 目标时长：{{duration_sec}} 秒
- 画外音：{{narration}}
- 视觉意图：{{visual_brief}}
- 检索关键词：{{keywords}}

## 风格约束

{{style_guide}}

## 参考范例（来自已验证的科普动画库；**借鉴写法，不要照抄内容**）

{{examples}}

{{feedback_block}}

## 现在开始

**只输出 JSON**（格式见系统提示）。不要输出解释、前言或 markdown 代码块之外的内容。

<!--
模板变量（由 CoderAgent 注入）：
  {{index}} {{tag}} {{engine}} {{duration_sec}} {{narration}} {{visual_brief}}
  {{keywords}} {{style_guide}} {{examples}} {{feedback_block}}
-->
