package obs

// 本文件覆盖可观测性的**跨进程传播**与**未配置时的行为**。
//
// 为什么这两块最需要测试：
//  1. 传播断掉时的症状是「两侧各自都有完整可看的链路，只是不在同一棵树上」——
//     没有报错、没有失败，只是查不到。本项目真实踩过一次（入队时
//     traceparent 没传出去，worker 那条链路自成一个根）。
//  2. 「没配置时是无害的 no-op」如果被破坏，表现会是启动失败或刷一屏导出错误，
//     而本地不跑 collector 是常态。

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// newTestProvider 建一个只在本用例内生效的 provider。
//
// 用**局部** provider 而不是全局的：`otel.SetTracerProvider` 是进程级状态，
// 用例之间会互相影响，跑单个用例过、跑全包挂是这类测试最典型的坑。
func newTestProvider(t *testing.T) (*sdktrace.TracerProvider, context.Context, func()) {
	t.Helper()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()))
	prevTP := otel.GetTracerProvider()
	prevProp := otel.GetTextMapPropagator()
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))
	ctx, span := tp.Tracer("test").Start(context.Background(), "parent")
	return tp, ctx, func() {
		span.End()
		_ = tp.Shutdown(context.Background())
		otel.SetTracerProvider(prevTP)
		otel.SetTextMapPropagator(prevProp)
	}
}

func TestInjectExtractRoundTripPreservesTraceID(t *testing.T) {
	_, ctx, done := newTestProvider(t)
	defer done()

	want := TraceIDFromContext(ctx)
	if want == "" {
		t.Fatal("活跃 span 应当有 trace ID")
	}

	// 模拟一次「入队 -> 出队」：注入成字符串，再在**干净的上下文**里还原。
	carrier := InjectTraceparent(ctx)
	if carrier == "" {
		t.Fatal("InjectTraceparent 不该返回空串")
	}
	if !strings.HasPrefix(carrier, "00-") {
		t.Fatalf("traceparent 应以版本 00 开头：%q", carrier)
	}

	restored := ExtractTraceparent(context.Background(), carrier)
	if got := TraceIDFromContext(restored); got != want {
		t.Fatalf("还原后的 trace ID 不一致：want %s got %s", want, got)
	}
}

// 反向控制：没有活跃 span 时必须返回**空串**，而不是编一个 ID。
// 调用方据此区分「本来就没有链路」与「有链路但没传过去」—— 后者才是要查的 bug。
func TestInjectTraceparentIsEmptyWithoutSpan(t *testing.T) {
	if got := InjectTraceparent(context.Background()); got != "" {
		t.Fatalf("无活跃 span 时应返回空串，实际 %q", got)
	}
	if got := TraceIDFromContext(context.Background()); got != "" {
		t.Fatalf("无活跃 span 时 trace ID 应为空，实际 %q", got)
	}
	if got := TraceIDFromContext(nil); got != "" { //nolint:staticcheck // 显式测 nil 入参
		t.Fatalf("nil ctx 应当安全返回空串，实际 %q", got)
	}
}

func TestExtractTraceparentToleratesGarbage(t *testing.T) {
	// 空串、坏格式都不该 panic，也不该产生一个假的 trace ID。
	for _, raw := range []string{
		"", "garbage", "00-", "00-abc-def-01",
		// 注意：**不要**在这里放版本 01 的例子。W3C 规定要向前兼容地解析
		// 更高版本（格式对就接受），OTel 的 propagator 正是这么做的。
		// 我的第一版把它当成了"非法输入"，测试因此挂了 —— 是断言错了，不是代码错。
		// 这条差异单独由 TestExtractTraceparentIsForwardCompatible 覆盖。
	} {
		ctx := ExtractTraceparent(context.Background(), raw)
		if got := TraceIDFromContext(ctx); got != "" {
			t.Fatalf("输入 %q 不该解析出 trace ID，实际 %q", raw, got)
		}
	}
}

func TestParseTraceparent(t *testing.T) {
	const valid = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	if got := ParseTraceparent(valid); got != "4bf92f3577b34da6a3ce929d0e0e4736" {
		t.Fatalf("解析有效 traceparent 失败：%q", got)
	}

	cases := map[string]string{
		"": "空串",
		"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7":    "缺少 flags 段",
		"00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01":  "trace ID 长度不对",
		"00-00000000000000000000000000000000-00f067aa0ba902b7-01": "全零 trace ID 非法",
		"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01": "大小写/长度正确", // 见下方单独断言
	}
	for raw, desc := range cases {
		got := ParseTraceparent(raw)
		if desc == "大小写/长度正确" {
			if got == "" {
				t.Fatalf("%s 应当能解析出来：%q", desc, raw)
			}
			continue
		}
		if got != "" {
			t.Fatalf("%s 不该解析成功，得到 %q", desc, got)
		}
	}

	// 大写 hex 不该被接受：W3C 规定是小写，接受大写会让「日志里的 ID」与
	// 「Tempo 里的 ID」出现大小写不一致的比对失败。
	if got := ParseTraceparent("00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01"); got != "" {
		t.Fatalf("大写十六进制不该被接受，得到 %q", got)
	}
}

// 未配置 endpoint 时必须是无害的 no-op：不报错、不导出，但**仍然建立
// TracerProvider**（否则 tracer 连 trace ID 都没有，日志里的 trace_id 会退化成空串）。
func TestInitWithoutEndpointIsHarmlessNoOp(t *testing.T) {
	prevTP := otel.GetTracerProvider()
	prevProp := otel.GetTextMapPropagator()
	defer func() {
		otel.SetTracerProvider(prevTP)
		otel.SetTextMapPropagator(prevProp)
	}()

	p, err := Init(context.Background(), Config{ServiceName: "test-svc", OTLPEndpoint: ""})
	if err != nil {
		t.Fatalf("未配置 endpoint 不该报错：%v", err)
	}
	if p.TracingEnabled() {
		t.Fatal("未配置 endpoint 时 TracingEnabled 必须为 false —— 谎报「已启用」比没启用更危险")
	}
	if p.Registry != nil {
		t.Fatal("未启用指标时不该建立注册表")
	}

	// 关键：即便不导出，也要能产生 trace ID。
	ctx, span := Tracer("test").Start(context.Background(), "x")
	defer span.End()
	if TraceIDFromContext(ctx) == "" {
		t.Fatal("未配置 endpoint 时仍应能产生 trace ID（否则日志里的 trace_id 会是空串）")
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatalf("Shutdown 不该报错：%v", err)
	}
}

func TestInitRejectsInvalidSampleRatio(t *testing.T) {
	// 配置本身非法属于启动期就该发现的问题，与「依赖不可用」不同。
	if _, err := Init(context.Background(), Config{SampleRatio: 1.5}); err == nil {
		t.Fatal("采样率 > 1 应当报错")
	}
}

func TestInitWithMetricsProvidesRegistry(t *testing.T) {
	prevTP := otel.GetTracerProvider()
	prevMP := otel.GetMeterProvider()
	defer func() {
		otel.SetTracerProvider(prevTP)
		otel.SetMeterProvider(prevMP)
	}()

	p, err := Init(context.Background(), Config{ServiceName: "test-svc", MetricsEnabled: true})
	if err != nil {
		t.Fatalf("Init 失败：%v", err)
	}
	defer func() { _ = p.Shutdown(context.Background()) }()
	if p.Registry == nil {
		t.Fatal("启用指标后应当有注册表")
	}
	// 运行时指标必须真的注册进去：排查「内存涨了是不是 goroutine 泄漏」时靠它。
	families, err := p.Registry.Gather()
	if err != nil {
		t.Fatalf("Gather 失败：%v", err)
	}
	names := map[string]bool{}
	for _, f := range families {
		names[f.GetName()] = true
	}
	for _, want := range []string{"go_goroutines", "process_resident_memory_bytes"} {
		if !names[want] {
			t.Fatalf("注册表里缺少 %s（排查内存/协程问题时必需）", want)
		}
	}
}

// 与 ParseTraceparent 的**刻意差异**：传播路径按 W3C 向前兼容（版本 01 也接受），
// 而 ParseTraceparent（只用于从字符串里取个 ID 展示/打日志）则只认版本 00、不做猜测。
// 把这条差异显式钉住，免得后人以为两者应当一致而"修"坏了其中一个。
func TestExtractTraceparentIsForwardCompatible(t *testing.T) {
	const v01 = "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
	ctx := ExtractTraceparent(context.Background(), v01)
	if got := TraceIDFromContext(ctx); got != "0af7651916cd43dd8448eb211c80319c" {
		t.Fatalf("传播路径应当向前兼容地接受版本 01，实际 %q", got)
	}
	if got := ParseTraceparent(v01); got != "" {
		t.Fatalf("ParseTraceparent 只认版本 00，不该接受版本 01，实际 %q", got)
	}
}
