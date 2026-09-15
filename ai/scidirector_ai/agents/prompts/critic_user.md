# 审查智能体 —— 用户消息模板

请审查下面这个镜头的画面。

## 镜头信息

- 序号：第 {{index}} 个
- 场景标签：`{{tag}}`（渲染引擎：`{{engine}}`）
- 目标时长：{{duration_sec}} 秒
- 实际渲染时长：{{actual_duration}} 秒
- 分辨率：{{width}} × {{height}}
- 这是第 {{attempt}} 次尝试

## 本该讲的内容（画外音）

{{narration}}

## 本该呈现的画面（视觉意图）

{{visual_brief}}

## 风格约束

{{style_guide}}

{{previous_feedback}}

## 抽帧

下面附上 {{frame_count}} 张抽帧图片，按**时间先后顺序**排列
（第一张是首帧，最后一张是末帧，中间为等间隔采样）。

## 现在开始

先在心里过一遍这三步（**不要写出来**）：

1. **首帧与末帧相比，画面发生了明显变化吗？**
   几乎没有变化 → 动画没生效，这是致命问题。
2. **画面里出现的文字，在当前尺寸下能读清吗？**
   看不清 → 可读性失分，且必须给出"字号从多少提到多少"的具体建议。
3. **画面内容与上面的画外音说的是同一件事吗？**
   对不上 → 逻辑一致性失分，这是最严重的一类问题。

然后**只输出一个 JSON 对象**，格式见系统提示词第五节。
不要输出任何解释性文字，不要用 markdown 代码块包裹。

<!--
模板变量（由 CriticAgent 注入）：
  {{index}} {{tag}} {{engine}} {{duration_sec}} {{actual_duration}}
  {{width}} {{height}} {{attempt}} {{narration}} {{visual_brief}}
  {{style_guide}} {{previous_feedback}} {{frame_count}}
-->
