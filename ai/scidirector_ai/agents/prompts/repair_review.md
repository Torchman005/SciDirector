## 本轮逐项复审与整体检查

先按以下清单逐项核对原验收条件，再对当前镜头做整体检查。原问题编号和验收条件固定。
前 {{current_count}} 张是本轮完整镜头抽帧；后续图像按下面的图像编号清单提供。
旧证据只用于比较，不得当成本轮缺陷。每组 before/after 对齐到相同秒数。
局部像素变化或编码器自述不能代表修复成功。静帧不足以证明的连续运动标为 unverified。

{{repair_context}}

在完整审核 JSON 中增加 repair_results，每项只包含：
task_id、status（open/partial/resolved/unverified）、evidence（本轮可见结果与原验收条件的对应关系）、
image_indices（支持该结论的本轮图像编号，1 起始，不得只引用旧图）。
resolved 必须有真实可核对的本轮画面证据；缺证据或未满足原条件时不得关闭。
repair_tasks 仅列本轮新发现的具体问题，避免把旧问题重新编号。未选中的旧问题由程序保留。
整体检查仍评估内容、可读性和新缺陷；只输出一份完整 JSON，不额外发起审查。
