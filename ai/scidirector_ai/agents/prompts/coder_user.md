# 编码智能体 —— 用户消息模板

请为下面这个分镜编写渲染代码。

## 分镜

- 序号：第 {{index}} 个
- 场景标签：`{{tag}}`（渲染引擎：`{{engine}}`）
- 目标时长：{{duration_sec}} 秒
- 画外音：{{narration}}
- 视觉意图：{{visual_brief}}
- 画面节拍（只描述先后顺序，不是绝对时间）：{{beats}}
- 检索关键词：{{keywords}}

## 风格约束

{{style_guide}}

## 参考范例（来自已验证的科普动画库；**借鉴写法，不要照抄内容**）

{{examples}}

{{feedback_block}}

## 现在开始

**只输出 JSON**（格式见系统提示）。不要输出解释、前言或 markdown 代码块之外的内容。

<!--
模板变量（由 CoderAgent 注入）：index / tag / engine / duration_sec / narration /
visual_brief / beats / keywords / style_guide / examples / feedback_block

**这份清单刻意不写成占位符形式。** 渲染器替换的是双花括号包起来的名字，
而清单若也那样写，就会被**真的替换一遍** —— 于是 feedback_block（含完整的
上一版代码）在每一条提示词里出现两次，白烧一倍上下文与 token。
这张模板此前正是如此，而且没有任何报错，只表现为"重做又慢又贵"。

新加占位符时请同时更新这份清单（用纯名字），并跑
`tests/test_coder.py::TestUserPromptTemplate::test_all_placeholders_resolve` ——
它会拦住任何残留的、无法解析的占位符（本注释的第一版就是因为写了示例语法而变红的）。
-->
