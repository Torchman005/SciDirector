// Package obs 提供可观测性基础设施：链路追踪（OpenTelemetry）、指标（Prometheus）
// 与它们的生命周期管理。
//
// ## 为什么单独成包
//
// 追踪与指标的初始化是**进程级一次性**的事情，且必须发生在任何业务代码之前
// （否则最早那批 span 会落到 no-op provider 上、被静默丢弃）。把它集中在一处，
// api 与 worker 两个入口就不会各写一份、也不会各差一个选项。
//
// ## 三条设计原则
//
//  1. **未配置时必须是无害的 no-op**，而不是报错或悄悄收集。
//     本地开发不跑 collector 是常态；此时 `Init` 应当成功返回，业务照常运行。
//     一条「配了才生效、没配就什么都不做」的路径，比「没配就崩」实用得多。
//  2. **trace_id 只有一个来源。** 项目原有的日志字段 `trace_id` 与 OTel 的 trace ID
//     必须**是同一个值**，否则「用日志里的 id 去查 trace」这个最基本的动作就会失败。
//     见 StartSpan 的说明。
//  3. **采样率必须可配且默认不过度采集。** 默认 1.0 在本地没问题，生产会先把
//     collector 打爆。这里默认仍取 1.0（本地联调要看全），但把开关交出去。
package obs

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	promexporter "go.opentelemetry.io/otel/exporters/prometheus"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
)

// Config 描述可观测性的启动参数。
type Config struct {
	// ServiceName 会写进每个 span 的 service.name 属性，
	// 是 Grafana/Tempo 里区分 api 与 worker 的唯一依据。
	ServiceName string
	// OTLPEndpoint 是 OTLP/gRPC 端点（如 127.0.0.1:4317）。为空表示**不导出**追踪。
	OTLPEndpoint string
	// Insecure 表示用明文 gRPC 连接 collector。本地/内网应当为 true。
	Insecure bool
	// SampleRatio 是采样比例（0~1]。1 表示全采。
	SampleRatio float64
	// MetricsEnabled 决定是否建立 Prometheus 注册表。
	MetricsEnabled bool
	Env            string
}

// Provider 持有已建立的 provider，负责在退出时优雅关闭。
type Provider struct {
	TracerProvider trace.TracerProvider
	Registry       *prometheus.Registry
	tp             *sdktrace.TracerProvider
	mp             *sdkmetric.MeterProvider

	// tracing 表示追踪是否真的建立了（而不是 no-op）。
	// 调用方据此在日志里如实说明"追踪未启用"，而不是让人以为采到了空数据。
	tracing bool
}

// TracingEnabled 报告追踪是否真的建立。
func (p *Provider) TracingEnabled() bool { return p != nil && p.tracing }

// Init 建立追踪与指标。
//
// 任何一步失败都**不会**让进程起不来：可观测性是辅助能力，
// 它坏掉时正确的行为是「业务照常、日志里说明白」，而不是让整个服务拒绝启动。
// 唯一的例外是配置本身非法（例如采样率越界），那属于启动期就该发现的问题。
func Init(ctx context.Context, cfg Config) (*Provider, error) {
	if cfg.ServiceName == "" {
		cfg.ServiceName = "scidirector"
	}
	if cfg.SampleRatio <= 0 {
		cfg.SampleRatio = 1.0
	}
	if cfg.SampleRatio > 1 {
		return nil, fmt.Errorf("obs: 采样率必须在 (0,1] 之间，实际 %v", cfg.SampleRatio)
	}

	p := &Provider{}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(cfg.ServiceName),
			attribute.String("deployment.environment", cfg.Env),
		),
	)
	if err != nil {
		// resource 合并失败不该阻断启动：退回默认 resource，服务名会缺一点信息。
		res = resource.Default()
	}

	// --- 追踪 ---------------------------------------------------------
	// 无论是否配了 endpoint 都要建 TracerProvider：否则 tracer 拿到的是全局 no-op，
	// span 连 trace ID 都没有，日志里的 trace_id 就会退化成空串。
	opts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(res),
		// 采样器与 BatchProcessor 分开设置：本地把 ratio 调成 1 就能看全，
		// 而不必改动导出目标。
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(cfg.SampleRatio))),
	}

	if cfg.OTLPEndpoint != "" {
		expOpts := []otlptracegrpc.Option{
			otlptracegrpc.WithEndpoint(cfg.OTLPEndpoint),
			// 导出超时不能太长：collector 挂掉时，业务不该被导出阻塞。
			otlptracegrpc.WithTimeout(5 * time.Second),
		}
		if cfg.Insecure {
			expOpts = append(expOpts, otlptracegrpc.WithInsecure())
		}
		exp, err := otlptracegrpc.New(ctx, expOpts...)
		if err != nil {
			return nil, fmt.Errorf("obs: 建立 OTLP 导出器失败: %w", err)
		}
		opts = append(opts, sdktrace.WithBatcher(exp,
			// 与默认值相比把批间隔调小：本项目的任务时长在秒级，
			// 默认 5s 会让「跑完一个任务马上去查 trace」查不到东西。
			sdktrace.WithBatchTimeout(2*time.Second),
		))
		p.tracing = true
	}

	p.tp = sdktrace.NewTracerProvider(opts...)
	otel.SetTracerProvider(p.tp)
	p.TracerProvider = p.tp

	// W3C TraceContext 是跨语言传播的**唯一**约定：Go 把 traceparent 放进 gRPC
	// metadata，Python 侧按同一规范解析，两侧才可能拼成一棵树。
	// Baggage 一并启用，便于后续把 job_id 之类随链路带过去。
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))

	// --- 指标 ---------------------------------------------------------
	if cfg.MetricsEnabled {
		reg := prometheus.NewRegistry()
		// 进程与 Go 运行时指标：排查「内存涨了是不是 goroutine 泄漏」时必需。
		reg.MustRegister(collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
		reg.MustRegister(collectors.NewGoCollector())

		exp, err := promexporter.New(promexporter.WithRegisterer(reg))
		if err != nil {
			return nil, fmt.Errorf("obs: 建立 Prometheus 导出器失败: %w", err)
		}
		p.mp = sdkmetric.NewMeterProvider(
			sdkmetric.WithResource(res),
			sdkmetric.WithReader(exp),
		)
		otel.SetMeterProvider(p.mp)
		p.Registry = reg
	}

	return p, nil
}

// Shutdown 冲刷并关闭 provider。
//
// **必须调用**：BatchProcessor 会把最近的 span 留在内存里，
// 不关就直接退出会丢掉最后几秒 —— 而恰恰是崩溃现场最想看的那几秒。
func (p *Provider) Shutdown(ctx context.Context) error {
	if p == nil {
		return nil
	}
	var errs []error
	if p.tp != nil {
		if err := p.tp.Shutdown(ctx); err != nil {
			errs = append(errs, err)
		}
	}
	if p.mp != nil {
		if err := p.mp.Shutdown(ctx); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// Tracer 返回本包约定命名的 tracer。
func Tracer(name string) trace.Tracer {
	if name == "" {
		name = "scidirector"
	}
	return otel.Tracer(name)
}

// ---------------------------------------------------------------------------
// 与既有日志字段的对接
// ---------------------------------------------------------------------------

// TraceIDFromContext 返回当前 span 的 trace ID（hex 形式）；没有则返回空串。
//
// **这是本项目里 trace_id 的唯一来源。** 不要另外生成一个随机 ID：
// 那样日志里的 trace_id 与 Tempo 里的 trace ID 会变成两个不同的东西，
// 「拿日志里的 ID 去查链路」这个最常用的排查动作会直接失效 ——
// 而这种不一致不会有任何报错，只会让人以为「链路没采到」。
func TraceIDFromContext(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	sc := trace.SpanContextFromContext(ctx)
	if !sc.HasTraceID() {
		return ""
	}
	return sc.TraceID().String()
}

// SpanIDFromContext 返回当前 span 的 span ID（hex 形式）；没有则返回空串。
func SpanIDFromContext(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	sc := trace.SpanContextFromContext(ctx)
	if !sc.HasSpanID() {
		return ""
	}
	return sc.SpanID().String()
}

// ParseTraceparent 解析 W3C traceparent 头，返回其中的 trace ID。
//
// 单独提供它是因为有一处特殊需求：任务入队时要把链路上下文**存进队列载荷**，
// 而出队时（新进程、新请求）需要把 trace ID 还原成一个普通字符串塞进日志。
// 用完整的 propagator 需要一对 carrier 接口，这里只要一个 ID，直接解析更直白。
//
// 格式：`00-<32位trace_id>-<16位span_id>-<flags>`，非法输入返回空串。
func ParseTraceparent(v string) string {
	parts := strings.Split(strings.TrimSpace(v), "-")
	if len(parts) != 4 {
		return ""
	}
	if parts[0] != "00" {
		return "" // 版本 00 之外的格式不做猜测
	}
	id := parts[1]
	if len(id) != 32 || strings.Trim(id, "0123456789abcdef") != "" {
		return ""
	}
	if id == strings.Repeat("0", 32) {
		return "" // 全零是非法 trace ID
	}
	return id
}

// ---------------------------------------------------------------------------
// 跨进程传播：把链路上下文塞进队列载荷
// ---------------------------------------------------------------------------

// mapCarrier 是 propagation.TextMapCarrier 的最小实现。
//
// 标准库没有现成的可用类型（`propagation.MapCarrier` 是 map[string]string，
// 但它带 mutex 且语义是「可写载体」）；这里只需要在 map 上读写一个键。
type mapCarrier map[string]string

func (c mapCarrier) Get(key string) string { return c[key] }
func (c mapCarrier) Set(key, value string) { c[key] = value }
func (c mapCarrier) Keys() []string {
	out := make([]string, 0, len(c))
	for k := range c {
		out = append(out, k)
	}
	return out
}

// TraceparentHeader 是 W3C 的传播头名。
const TraceparentHeader = "traceparent"

// InjectTraceparent 从 ctx 里取出当前链路上下文，序列化成可存库/入队的字符串。
//
// 没有活跃 span 时返回**空串**（而不是编一个）：调用方据此区分
// 「入队时本来就没有链路」与「有链路但没传过去」，后者才是要排查的 bug。
func InjectTraceparent(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	carrier := mapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	return carrier.Get(TraceparentHeader)
}

// ExtractTraceparent 把入队时存下的字符串还原成远端父上下文。
//
// 空串返回原 ctx（表示没有父级）—— worker 会因此起一个**新的根 span**，
// 这是正确行为：任务可能是被定时补偿任务入队的，本来就不属于任何请求链路。
func ExtractTraceparent(ctx context.Context, traceparent string) context.Context {
	if ctx == nil || traceparent == "" {
		return ctx
	}
	return otel.GetTextMapPropagator().Extract(ctx, mapCarrier{TraceparentHeader: traceparent})
}

// SpanContextCarrier 让 gin 之类的框架能把 span 上下文塞进请求作用域。
// 这里不引入框架依赖，只提供一个字符串键名常量，避免各处拼错。
const SpanContextKey = "otel_span_ctx"
