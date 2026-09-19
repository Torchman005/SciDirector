// Package ai 封装到 Python「AI 大脑」的 gRPC 客户端。
//
// 设计要点：
//   - 连接在进程启动时建立并复用（gRPC 连接是长连接、多路复用的），
//     绝不在每次调用时 Dial —— 那会把 TLS 握手/HTTP2 建连开销放大到每次推理上。
//   - 一元 RPC 与流式 RPC 使用**不同的超时**：一次 VLM 审查可能要几十秒，
//     而 RunPipeline 覆盖整个任务的渲染周期，可能长达数十分钟。
//   - 拦截器统一注入 trace_id / job_id，让 Go 与 Python 的日志能按同一字段串联。
package ai

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/status"

	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

// ErrUnavailable 表示 Python 大脑不可达或未就绪。
// 上层据此区分「基础设施故障」（可重试）与「业务失败」（不该重试）。
var ErrUnavailable = errors.New("ai: Python 大脑不可达")

// ErrNotImplemented 表示 Python 端明确返回 UNIMPLEMENTED —— 该能力**尚未实现**。
//
// 为什么必须单独成类：这类错误**重试多少次都不会成功**。
// 若与普通错误混在一起交给 Asynq，它会按指数退避重试到上限（默认 5 次），
// 既浪费资源，又会在告警里制造一堆噪声，掩盖真正的故障。
// worker 据此返回 asynq.SkipRetry，让任务直接归档。
var ErrNotImplemented = errors.New("ai: Python 端尚未实现该能力")

// Client 是 AI 服务客户端。
type Client struct {
	conn *grpc.ClientConn
	cli  pb.AiDirectorServiceClient
	cfg  config.AIConfig
	log  *slog.Logger
}

// NewClient 建立连接。
//
// 注意：这里用 grpc.NewClient（惰性连接）而不是 DialContext（立即连接）。
// 惰性连接让 Worker 在 Python 侧尚未就绪时仍能启动，由 gRPC 自动重连 +
// 就绪探针兜底，避免容器编排中的启动顺序死锁。
func NewClient(cfg config.AIConfig, logger *slog.Logger) (*Client, error) {
	conn, err := grpc.NewClient(cfg.Addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(
			grpc.MaxCallRecvMsgSize(cfg.MaxRecvMsgSizeMB<<20),
			grpc.MaxCallSendMsgSize(cfg.MaxRecvMsgSizeMB<<20),
		),
		// 长任务场景下保持连接活跃，避免被中间设备（如 k8s Service、LB）静默断连。
		grpc.WithKeepaliveParams(keepalive.ClientParameters{
			Time:                30 * time.Second,
			Timeout:             10 * time.Second,
			PermitWithoutStream: true,
		}),
		// otelgrpc 负责把当前 span 上下文按 W3C 规范写进 gRPC metadata
		// （`traceparent`），Python 侧据此把它的 span 挂到同一条链路上 ——
		// 这是「Go span ↔ gRPC ↔ Python span 串成一棵树」的**唯一**环节。
		// 顺序：otelgrpc 在前，日志拦截器在后（后者不碰 metadata）。
		grpc.WithChainUnaryInterceptor(
			otelgrpc.UnaryClientInterceptor(),
			unaryLoggingInterceptor(logger),
		),
		grpc.WithChainStreamInterceptor(
			otelgrpc.StreamClientInterceptor(),
			streamLoggingInterceptor(logger),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("ai: 创建 gRPC 连接失败: %w", err)
	}
	return &Client{
		conn: conn,
		cli:  pb.NewAiDirectorServiceClient(conn),
		cfg:  cfg,
		log:  logger,
	}, nil
}

// Close 关闭连接。
func (c *Client) Close() error { return c.conn.Close() }

// Health 探测 Python 服务与沙盒的可用性。
func (c *Client) Health(ctx context.Context) (*pb.HealthResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	resp, err := c.cli.Health(ctx, &pb.HealthRequest{})
	if err != nil {
		return nil, wrapRPCError("Health", err)
	}
	return resp, nil
}

// PlanScript 只调用导演智能体：脚本 -> 分镜表。
func (c *Client) PlanScript(ctx context.Context, req *pb.PlanScriptRequest) (*pb.PlanScriptResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, c.cfg.UnaryTimeout)
	defer cancel()

	resp, err := c.cli.PlanScript(ctx, req)
	if err != nil {
		return nil, wrapRPCError("PlanScript", err)
	}
	return resp, nil
}

// GenerateShot 调用编码 + 渲染：产出单个镜头的视频片段。
func (c *Client) GenerateShot(ctx context.Context, req *pb.GenerateShotRequest) (*pb.GenerateShotResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, c.cfg.UnaryTimeout)
	defer cancel()

	resp, err := c.cli.GenerateShot(ctx, req)
	if err != nil {
		return nil, wrapRPCError("GenerateShot", err)
	}
	return resp, nil
}

// CritiqueShot 调用审查智能体（VLM）。
func (c *Client) CritiqueShot(ctx context.Context, req *pb.CritiqueShotRequest) (*pb.CritiqueShotResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, c.cfg.UnaryTimeout)
	defer cancel()

	resp, err := c.cli.CritiqueShot(ctx, req)
	if err != nil {
		return nil, wrapRPCError("CritiqueShot", err)
	}
	return resp, nil
}

// ReviseShot 把人类反馈回灌，重新生成单个镜头。
func (c *Client) ReviseShot(ctx context.Context, req *pb.ReviseShotRequest) (*pb.ReviseShotResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, c.cfg.UnaryTimeout)
	defer cancel()

	resp, err := c.cli.ReviseShot(ctx, req)
	if err != nil {
		return nil, wrapRPCError("ReviseShot", err)
	}
	return resp, nil
}

// RunPipeline 拉起整条 LangGraph 流水线，并通过 onEvent 回调逐条消费事件。
//
// 语义约定：
//   - onEvent 返回错误将**主动终止**流（用于「人工打回后取消当前任务」这类控制流）。
//   - 流正常结束（io.EOF）视为流水线跑完，返回 nil。
//   - 上下文取消返回 ctx.Err()，调用方据此区分「用户取消」与「真实失败」。
//
// 为什么用流式而不是轮询：一次生成包含 N 个镜头 × M 次尝试 × 4 个节点，
// 轮询既浪费又能延迟数秒；流式让每一步状态变化立刻抵达前端。
func (c *Client) RunPipeline(
	ctx context.Context,
	req *pb.RunPipelineRequest,
	onEvent func(*pb.PipelineEvent) error,
) error {
	streamCtx, cancel := context.WithTimeout(ctx, c.cfg.StreamTimeout)
	defer cancel()

	stream, err := c.cli.RunPipeline(streamCtx, req)
	if err != nil {
		return wrapRPCError("RunPipeline", err)
	}

	for {
		ev, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			// 服务端正常关闭流：流水线跑完。
			return nil
		}
		if err != nil {
			// 上下文被取消时，gRPC 会返回 Canceled/DeadlineExceeded；
			// 转换为标准 ctx 错误，让上层统一处理。
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return wrapRPCError("RunPipeline.Recv", err)
		}
		if onEvent == nil {
			continue
		}
		if err := onEvent(ev); err != nil {
			// 客户端主动中止：取消流并原样返回业务错误。
			cancel()
			return err
		}
	}
}

// wrapRPCError 把 gRPC 状态码翻译成对上层有意义的错误。
//
// 这一步很重要：上层需要据此决定「该不该重试」。
// 例如 Unavailable 可以交给 Asynq 重试，而 InvalidArgument 重试多少次都没用。
func wrapRPCError(op string, err error) error {
	st, ok := status.FromError(err)
	if !ok {
		return fmt.Errorf("ai: %s 调用失败: %w", op, err)
	}
	switch st.Code() {
	case codes.Unavailable, codes.DeadlineExceeded:
		return fmt.Errorf("%w: %s 调用失败（%s）: %s", ErrUnavailable, op, st.Code(), st.Message())
	case codes.Unimplemented:
		// 未实现 ≠ 故障：必须让调用方能够识别并**跳过重试**。
		return fmt.Errorf("%w: %s 调用失败（%s）: %s",
			ErrNotImplemented, op, st.Code(), st.Message())
	case codes.Canceled:
		return context.Canceled
	default:
		return fmt.Errorf("ai: %s 调用失败（%s）: %s", op, st.Code(), st.Message())
	}
}

// ---------------------------------------------------------------------------
// 拦截器：统一日志与耗时观测
// ---------------------------------------------------------------------------

func unaryLoggingInterceptor(logger *slog.Logger) grpc.UnaryClientInterceptor {
	return func(
		ctx context.Context, method string, req, reply any,
		cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption,
	) error {
		start := time.Now()
		err := invoker(ctx, method, req, reply, cc, opts...)
		lg := logging.FromContext(ctx)
		if lg == nil {
			lg = logger
		}
		if err != nil {
			lg.Error("gRPC 一元调用失败",
				slog.String("method", method),
				slog.Duration("elapsed", time.Since(start)),
				slog.String("error", err.Error()),
			)
			return err
		}
		lg.Debug("gRPC 一元调用完成",
			slog.String("method", method),
			slog.Duration("elapsed", time.Since(start)),
		)
		return nil
	}
}

func streamLoggingInterceptor(logger *slog.Logger) grpc.StreamClientInterceptor {
	return func(
		ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn,
		method string, streamer grpc.Streamer, opts ...grpc.CallOption,
	) (grpc.ClientStream, error) {
		start := time.Now()
		st, err := streamer(ctx, desc, cc, method, opts...)
		lg := logging.FromContext(ctx)
		if lg == nil {
			lg = logger
		}
		if err != nil {
			// 建流失败才算失败；流建立后的事件级错误由 RunPipeline 处理。
			lg.Error("gRPC 流建立失败",
				slog.String("method", method),
				slog.Duration("elapsed", time.Since(start)),
				slog.String("error", err.Error()),
			)
			return nil, err
		}
		lg.Info("gRPC 流已建立", slog.String("method", method))
		return st, nil
	}
}
