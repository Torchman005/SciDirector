# 导演已规划但分镜表为 0

`job-e117c32bdabfc15a` 的只读诊断发现：任务接口仍为 PLANNING、Shots 为空；AI 检查点包含 45 个分镜，并已有产物。事件流记录过 `Unavailable: Too many pings`，之后出现编码、渲染、审核和“共 45 镜头”的推进事件，却没有成功送达的完整 plan。这是检查点与消费者状态不同步，刷新页面不能补回分镜。

导演完成后，LangGraph 可能先提交检查点，再向 gRPC 消费者交付事件。连接此时中断，旧实现续跑会跳过导演，也就不再发送分镜表；Go 对未知分镜的事件又直接忽略，导致 AI 继续生成、网页始终显示 0。原 30 秒客户端保活还短于 Python gRPC 服务端缺省接受的最小 ping 间隔。

## 恢复契约

- Go 客户端与 Python 服务端使用 5 分钟保活，仅在有调用时启用；不关闭服务端 ping 约束。
- `resume=true` 且存在规划结果时，PipelineRunner 在图继续执行前补发任务级 plan。已完成检查点也先补发，再发 final，不能重新生成镜头。尚未规划的检查点继续原图，不提前虚构分镜表。
- plan 的 shots/outline 保持原格式。新增可选 recovery_events，以原始 plan 为基线，按原顺序携带实际 render/critique/revise 事件及每镜最新 code；不从“有文件”推断审核通过。若老检查点无原 plan，则仅以现有 shots 元数据作为基线。
- 嵌套事件由 Python `MessageToDict(event_to_pb(...))` 生成，snake_case、数字枚举，int64 按 protobuf JSON 编码为字符串。Go 必须用 protojson 解码，不能直接 encoding/json 到生成的 PipelineEvent。恢复审核的时间取原事件时间，不写成恢复时刻。
- Go 在一次任务更新中创建缺失镜头并回放其真实历史；已有镜头保持进度，不回放旧审核或旧产物。plan 重复投递不清空源码、产物、修复账本或已修订旁白。fy 的 MATH/DATA 等标签仍统一使用 motion。
- 旁白以原始规划开始，只在带有新视频的 render/narration_patch 事件上更新；拒绝错误历史、旧视频/旧配音组合，继续保留现有审核和产物约束。
- 其他任务的事件、不支持的历史节点、引用未知镜头或无效旁白修订均拒绝，不能发布部分恢复的分镜表。正常流遇到未知分镜也明确报错并停止消费，交由队列原有重试恢复，不继续静默丢失进度。

## 范围和上线限制

这次恢复只补齐原来缺失的镜头；它不是已存在镜头的通用事件对账。原始历史可能包含多次真实审核，恢复保留这些记录，但只传每镜最新源码以控制大小。仍受双方 gRPC 消息上限约束（Go 缺省 8 MiB，Python 32 MiB），不保证任意长度任务均可单条恢复；没有增加模型调用、提高预算或修改前端计数算法。

当前任务使用旧 AI 进程内的 memory 检查点。修改磁盘上的代码不会替换运行中的图；重启 AI 会丢失该进度。现有 GetCheckpointSnapshot RPC 仅提供 ID、attempt 和产物标记，不提供完整规划/历史，不能据此伪造完整分镜。此次没有重启、重跑或修改该任务，也不声称它已在线恢复。部署需要协调更新 worker 与 Python，并先处理在途内存状态；持久化检查点应作为后续运维配置工作。

验证记录见 [QUALITY_VALIDATION.md](QUALITY_VALIDATION.md)。跨语言合成夹具为 `proto/testdata/pipeline_plan_recovery.json`，不含真实任务文案、模型输出或媒体。
