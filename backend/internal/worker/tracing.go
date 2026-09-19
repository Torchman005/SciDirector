package worker

// worker 侧的链路接续。
//
// worker 与 api 是两个进程，中间隔着 Redis（甚至可能隔着机器）。Asynq 不传递任何
// 上下文，所以「HTTP 请求 → 入队 → worker 执行 → gRPC 调 Python」天然会断成两棵树。
// 断掉的那一段恰恰是最需要看的一段：任务在队列里排了多久、worker 里慢在哪一步。
//
// 接续靠两件事：
//  1. 入队时把 W3C `traceparent` 存进载荷（见 queue.carryTrace，收口在 client 上）；
//  2. 出队时用它恢复远端父上下文，再起一个 consumer span。

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/obs"
)

// startConsumerSpan 接续入队方的链路，并起一个表示「本进程正在消费这个任务」的 span。
//
// traceparent 为空时（例如定时补偿任务、或上游本来就没有链路）**新建一条根链路** ——
// 这是正确行为而不是缺陷：这类任务本来就不属于任何请求。
//
// 顺带把 trace_id 写回日志上下文，让 worker 侧的所有日志与这条链路的 span 对得上。
// 这一步不能省：worker 日志此前**完全没有 trace_id**（它不经过 HTTP 中间件），
// 于是「从日志跳到链路」这条路在 worker 侧根本走不通。
func startConsumerSpan(ctx context.Context, traceparent, taskType, jobID string) (context.Context, trace.Span) {
	ctx = obs.ExtractTraceparent(ctx, traceparent)
	ctx, span := obs.Tracer("worker").Start(ctx, "consume "+taskType, trace.WithSpanKind(trace.SpanKindConsumer))
	span.SetAttributes(
		attribute.String("messaging.system", "asynq"),
		attribute.String("messaging.destination.name", taskType),
		attribute.String("scidirector.job_id", jobID),
		// 标出这一段是否真的接上了上游。**没有这个标记，断链会表现为
		// 「两条看起来都正常的 trace」，而不是一眼可见的问题**。
		attribute.Bool("scidirector.trace_continued", traceparent != ""),
	)
	if tid := obs.TraceIDFromContext(ctx); tid != "" {
		ctx = logging.WithTrace(ctx, tid)
	}
	return ctx, span
}
