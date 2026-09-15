package ai

import (
	"context"
	"errors"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// TestWrapRPCErrorClassifiesRetryability 验证「错误分类」这一关键行为。
//
// 为什么这个测试重要：上层（Asynq worker）**完全依赖**这里的分类来决定
// 要不要重试。分类错了的后果不是崩溃，而是静默的资源浪费与告警噪声 ——
// 例如把 UNIMPLEMENTED 当成基础设施故障，就会把一个永远不会成功的调用
// 重试到上限（本项目在手工联调时确实踩到过，见 docs/ROADMAP.md 已知问题 #1）。
func TestWrapRPCErrorClassifiesRetryability(t *testing.T) {
	cases := []struct {
		name          string
		code          codes.Code
		wantRetryable bool
		wantSentinel  error
	}{
		{
			name:          "Unavailable 属于可重试的基础设施故障",
			code:          codes.Unavailable,
			wantRetryable: true,
			wantSentinel:  ErrUnavailable,
		},
		{
			name:          "DeadlineExceeded 属于可重试的基础设施故障",
			code:          codes.DeadlineExceeded,
			wantRetryable: true,
			wantSentinel:  ErrUnavailable,
		},
		{
			name:          "Unimplemented 不可重试（重试多少次都不会成功）",
			code:          codes.Unimplemented,
			wantRetryable: false,
			wantSentinel:  ErrNotImplemented,
		},
		{
			name:          "InvalidArgument 不可重试",
			code:          codes.InvalidArgument,
			wantRetryable: false,
			wantSentinel:  nil,
		},
		{
			name:          "Internal 不可重试（由上层按任务策略处理）",
			code:          codes.Internal,
			wantRetryable: false,
			wantSentinel:  nil,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := wrapRPCError("TestMethod", status.Error(tc.code, "boom"))
			if err == nil {
				t.Fatal("wrapRPCError 不应返回 nil")
			}

			gotRetryable := errors.Is(err, ErrUnavailable)
			if gotRetryable != tc.wantRetryable {
				t.Errorf("可重试性判定错误：期望 %v，实际 %v（err=%v）",
					tc.wantRetryable, gotRetryable, err)
			}

			if tc.wantSentinel != nil && !errors.Is(err, tc.wantSentinel) {
				t.Errorf("期望错误链上能匹配 %v，实际 err=%v", tc.wantSentinel, err)
			}
			// 可重试与"未实现"必须互斥：两者都命中会让 worker 的判定失去意义。
			if errors.Is(err, ErrUnavailable) && errors.Is(err, ErrNotImplemented) {
				t.Errorf("ErrUnavailable 与 ErrNotImplemented 不应同时命中：%v", err)
			}
		})
	}
}

// TestWrapRPCErrorCanceled 验证取消被还原为 context.Canceled。
//
// 这一点会影响 worker 的分支：任务被中断（优雅关闭 / 超时）时应当**重新投递**，
// 而不是被标记为业务失败。如果这里返回的是一个普通 error，
// 一次正常的发布滚动就会被误判成"生成失败"。
func TestWrapRPCErrorCanceled(t *testing.T) {
	err := wrapRPCError("TestMethod", status.Error(codes.Canceled, "canceled by client"))
	if !errors.Is(err, context.Canceled) {
		t.Errorf("期望还原为 context.Canceled，实际 err=%v", err)
	}
	// 取消不应被当成可重试的基础设施故障，也不应被当成"未实现"。
	if errors.Is(err, ErrUnavailable) || errors.Is(err, ErrNotImplemented) {
		t.Errorf("context.Canceled 不应命中任何哨兵错误：%v", err)
	}
}

// TestWrapRPCErrorNonStatus 验证非 gRPC 错误被原样包裹（不会伪装成状态码错误）。
func TestWrapRPCErrorNonStatus(t *testing.T) {
	original := errors.New("不是 gRPC 状态错误")
	err := wrapRPCError("TestMethod", original)
	if !errors.Is(err, original) {
		t.Errorf("期望错误链保留原始错误，实际 err=%v", err)
	}
	if errors.Is(err, ErrUnavailable) || errors.Is(err, ErrNotImplemented) {
		t.Errorf("非状态错误不应命中任何哨兵：%v", err)
	}
}

// TestErrorSentinelsAreDistinct 防止未来有人把两个哨兵错误合并成同一个值 ——
// 那会让"可重试"与"未实现"的区分彻底失效，且不会有任何编译错误提示。
func TestErrorSentinelsAreDistinct(t *testing.T) {
	if errors.Is(ErrUnavailable, ErrNotImplemented) ||
		errors.Is(ErrNotImplemented, ErrUnavailable) {
		t.Fatal("ErrUnavailable 与 ErrNotImplemented 必须相互独立")
	}
}
