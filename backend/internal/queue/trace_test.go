package queue

// 本文件覆盖**入队时的链路上下文传递**（阶段五·可观测性）。
//
// 为什么这条路径值得单独的集成测试：
// 它断掉时的症状是「两侧各自都有完整可看的链路，只是不在同一棵树上」——
// 没有报错、没有失败，只是拿 HTTP 请求的 trace_id 去 Tempo 查不到 worker 那一段。
// 我在本轮就真实踩过一次，而且**第一版修错了位置**：以为是 worker 侧没解析，
// 实际是 api 中间件把带 span 的 ctx 装回请求的时机放在了 `c.Next()` 之后，
// handler 里拿到的仍是没有 span 的上下文。
//
// 用真实 Redis 而不是替身：本用例要断言的正是「worker 侧最终能读到的东西」，
// 也就是**落进 Redis 的那份任务载荷**。用内存替身会把「到底写进去了什么」一起假掉。

import (
	"context"
	"encoding/json"
	"net"
	"testing"
	"time"

	"github.com/hibiken/asynq"
	goredis "github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/obs"
)

const traceTestDB = 15 // 与 queue 包其它集成测试共用（同包内用例串行）

func requireRedisForTrace(t *testing.T) string {
	t.Helper()
	addr := "127.0.0.1:6379"
	conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
	if err != nil {
		t.Skipf("Redis 不可达（%s），跳过链路传递集成测试: %v", addr, err)
	}
	_ = conn.Close()
	return addr
}

// withSpan 返回一个带着活跃 span 的 ctx，以及期望的 traceparent。
func withSpan(t *testing.T) (context.Context, string) {
	t.Helper()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()))
	prevTP := otel.GetTracerProvider()
	prevProp := otel.GetTextMapPropagator()
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))
	t.Cleanup(func() {
		_ = tp.Shutdown(context.Background())
		otel.SetTracerProvider(prevTP)
		otel.SetTextMapPropagator(prevProp)
	})

	ctx, span := tp.Tracer("test").Start(context.Background(), "http-handler")
	t.Cleanup(func() { span.End() })
	return ctx, obs.InjectTraceparent(ctx)
}

// 从队列里把刚落进去的任务取回来，看它**实际**写了什么。
func readEnqueuedPayload(t *testing.T, addr string, wantType string) []byte {
	t.Helper()
	insp := asynq.NewInspector(asynq.RedisClientOpt{Addr: addr, DB: traceTestDB})
	t.Cleanup(func() { _ = insp.Close() })

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		for _, q := range []string{QueueCritical, QueueDefault, QueueLow} {
			tasks, err := insp.ListPendingTasks(q)
			if err != nil {
				continue
			}
			for _, task := range tasks {
				if task.Type == wantType {
					return task.Payload
				}
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("5 秒内没能在队列里找到 %s 任务", wantType)
	return nil
}

func newTraceClient(t *testing.T, addr string) *Client {
	t.Helper()
	rdb := goredis.NewClient(&goredis.Options{Addr: addr, DB: traceTestDB})
	if err := rdb.FlushDB(context.Background()).Err(); err != nil {
		t.Fatalf("清空测试库失败: %v", err)
	}
	_ = rdb.Close()
	return NewClient(config.RedisConfig{Addr: addr, DB: traceTestDB}, config.QueueConfig{
		Queues:   map[string]int{QueueCritical: 1, QueueDefault: 1, QueueLow: 1},
		MaxRetry: 3, RetryBackoff: time.Second, TaskTimeout: time.Minute,
	})
}

func TestEnqueueGenerateJobCarriesTraceparent(t *testing.T) {
	addr := requireRedisForTrace(t)
	c := newTraceClient(t, addr)
	t.Cleanup(func() { _ = c.Close() })

	ctx, want := withSpan(t)
	if want == "" {
		t.Fatal("用例自身没建立 span，测不出任何东西")
	}

	if _, err := c.EnqueueGenerateJob(ctx, &GenerateJobPayload{
		JobID: "job-trace-1", RawScript: "脚本", TargetDurationSec: 30, Locale: "zh-CN",
	}); err != nil {
		t.Fatalf("入队失败: %v", err)
	}

	var payload GenerateJobPayload
	if err := json.Unmarshal(readEnqueuedPayload(t, addr, TaskGenerateJob), &payload); err != nil {
		t.Fatalf("解析载荷失败: %v", err)
	}
	if payload.Traceparent != want {
		t.Fatalf("载荷里的 traceparent 与入队时的链路不一致：\nwant %s\ngot  %s", want, payload.Traceparent)
	}
	// worker 侧据此起子 span；这里顺带确认它真的能解析回同一条链路。
	restored := obs.ExtractTraceparent(context.Background(), payload.Traceparent)
	if got := obs.TraceIDFromContext(restored); got != obs.ParseTraceparent(want) {
		t.Fatalf("还原出的 trace ID 不一致：want %s got %s", obs.ParseTraceparent(want), got)
	}
}

// 反向控制：入队时**没有**活跃 span（例如定时补偿任务、或从后台 goroutine 入队），
// 载荷里就应当是空 —— 而不是编一个 ID。worker 侧据此新建一条根链路，
// 那是正确行为；编一个假父级反而会让链路指向一个不存在的东西。
func TestEnqueueWithoutSpanLeavesTraceparentEmpty(t *testing.T) {
	addr := requireRedisForTrace(t)
	c := newTraceClient(t, addr)
	t.Cleanup(func() { _ = c.Close() })

	if _, err := c.EnqueueGenerateJob(context.Background(), &GenerateJobPayload{
		JobID: "job-trace-2", RawScript: "脚本", TargetDurationSec: 30, Locale: "zh-CN",
	}); err != nil {
		t.Fatalf("入队失败: %v", err)
	}

	var payload GenerateJobPayload
	if err := json.Unmarshal(readEnqueuedPayload(t, addr, TaskGenerateJob), &payload); err != nil {
		t.Fatalf("解析载荷失败: %v", err)
	}
	if payload.Traceparent != "" {
		t.Fatalf("无活跃 span 时不该编造 traceparent，实际 %q", payload.Traceparent)
	}
}

// 三类任务都必须带上 —— 入队点有六处，只覆盖一处等于没覆盖：
// 漏掉的那类任务会在链路里凭空消失，而没有任何报错。
func TestAllTaskTypesCarryTraceparent(t *testing.T) {
	addr := requireRedisForTrace(t)
	ctx, want := withSpan(t)

	cases := []struct {
		name     string
		taskType string
		enqueue  func(*Client) error
	}{
		{"render_shot", TaskRenderShot, func(c *Client) error {
			_, err := c.EnqueueRenderShot(ctx, &RenderShotPayload{
				JobID: "job-trace-3", ShotID: "s0", Attempt: 1, TriggeredBy: "api",
			})
			return err
		}},
		{"compose", TaskComposeJob, func(c *Client) error {
			_, err := c.EnqueueComposeJob(ctx, &ComposeJobPayload{JobID: "job-trace-4"})
			return err
		}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := newTraceClient(t, addr)
			t.Cleanup(func() { _ = c.Close() })
			if err := tc.enqueue(c); err != nil {
				t.Fatalf("入队失败: %v", err)
			}
			raw := readEnqueuedPayload(t, addr, tc.taskType)

			// 三类载荷的结构不同，因此用通用的 map 取值：
			// 这样断言的是**线上 JSON**，而不是某个 Go 结构体字段 ——
			// 字段被内嵌或改名时，payload 的序列化形态才是真正要保证的东西。
			var generic map[string]any
			if err := json.Unmarshal(raw, &generic); err != nil {
				t.Fatalf("解析载荷失败: %v", err)
			}
			if generic["traceparent"] != want {
				t.Fatalf("%s 的载荷未携带 traceparent：%v", tc.taskType, generic["traceparent"])
			}
		})
	}
}
