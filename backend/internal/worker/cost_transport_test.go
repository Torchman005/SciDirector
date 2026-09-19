package worker

// 本文件把成本核算跑在**真实的 gRPC 通道**上。
//
// 为什么不能只靠直接构造 `pb.PipelineEvent` 的用例：
// 那种用例把「Go 与 Python 之间的传输」整个跳过了。而成本归零最常见的两种真实原因
// 恰恰都发生在这一段 —— proto 字段没被赋值（`payload_json` 在流式传输中没带上）、
// 或者客户端侧的接收上限截断了消息。两者在直接构造事件的用例里**永远不会暴露**。
//
// 因此这里起一个真实的 gRPC server（扮演 Python），让它通过 `RunPipeline` 流式
// 吐出**与 Python 产出一字不差**的 payload_json，再由 Go 侧真实客户端接收、落库、读回。
//
// 唯一被假掉的是「怎么算出这些 token」——那属于 LLM 服务商，不在本系统的责任范围内；
// 而「算出来的数字能不能活着走到存储」正是本文件要证明的事。
// 注意：mock LLM 模式下 Python 不产生用量（见 llm.py），
// 所以线上用 mock 跑出来的成本是 0，那**不能**作为本链路的证据 —— 这个文件才是。

import (
	"context"
	"io"
	"log/slog"
	"net"
	"testing"
	"time"

	goredis "github.com/redis/go-redis/v9"
	"google.golang.org/grpc"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/archive"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// costAIServer 只实现 RunPipeline：按顺序把预先给定的事件流吐出去。
type costAIServer struct {
	pb.UnimplementedAiDirectorServiceServer
	events []*pb.PipelineEvent
}

func (s *costAIServer) RunPipeline(_ *pb.RunPipelineRequest, stream grpc.ServerStreamingServer[pb.PipelineEvent]) error {
	for _, ev := range s.events {
		if err := stream.Send(ev); err != nil {
			return err
		}
	}
	return nil
}

// applyCostEventsOverGRPC 起一个真实 gRPC server，让 Processor 通过真实客户端消费事件。
//
// events 以**构造函数**的形式传入而不是切片：事件里的 job_id 必须与真正落库的任务一致，
// 而任务是在本函数里创建的。传切片就得先猜一个 job_id 再回头改，
// 那种「先塞占位再补」的写法一旦漏改就会让用例悄悄退化成不验证任何东西。
func applyCostEventsOverGRPC(t *testing.T, build func(jobID string) []*pb.PipelineEvent) (*store.Store, string, context.Context) {
	t.Helper()

	addr := requireRedis(t)
	ctx := context.Background()

	// 每个用例从干净的库开始：断言「用量到底是多少」时，残留任务会让结果取决于执行顺序。
	rdb := goredis.NewClient(&goredis.Options{Addr: addr, DB: testRedisDB})
	if err := rdb.FlushDB(ctx).Err(); err != nil {
		t.Fatalf("清空测试库失败: %v", err)
	}
	_ = rdb.Close()

	st, err := store.New(ctx, config.RedisConfig{Addr: addr, DB: testRedisDB})
	if err != nil {
		t.Fatalf("构造 Store 失败: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	jobID := seedCostJob(t, ctx, st)

	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("监听 gRPC 端口失败: %v", err)
	}
	gs := grpc.NewServer()
	pb.RegisterAiDirectorServiceServer(gs, &costAIServer{events: build(jobID)})
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := &config.Config{
		Env:      "test",
		LogLevel: "error",
		Redis:    config.RedisConfig{Addr: addr, DB: testRedisDB},
		Queue: config.QueueConfig{
			Queues:       map[string]int{queue.QueueCritical: 1, queue.QueueDefault: 1},
			MaxRetry:     3,
			RetryBackoff: time.Second,
			TaskTimeout:  time.Minute,
		},
		// StreamTimeout 必须显式给值：RunPipeline 是服务端流式调用，
		// 而它的零值会让 context.WithTimeout 立刻到期，报出「Python 大脑不可达」——
		// 一个与真实原因（配置缺字段）毫无关系的错误信息。
		AI: config.AIConfig{
			Addr:             lis.Addr().String(),
			UnaryTimeout:     30 * time.Second,
			StreamTimeout:    time.Minute,
			MaxRecvMsgSizeMB: 8,
		},
		Pipeline: config.PipelineConfig{ShotMaxAttempts: 3, CriticScoreThreshold: 0.75},
		Archive:  config.ArchiveConfig{Backend: "none"},
	}

	aiClient, err := ai.NewClient(cfg.AI, logger)
	if err != nil {
		t.Fatalf("构造 ai 客户端失败: %v", err)
	}
	t.Cleanup(func() { _ = aiClient.Close() })

	q := queue.NewClient(cfg.Redis, cfg.Queue)
	t.Cleanup(func() { _ = q.Close() })

	proc := NewProcessor(cfg, st, aiClient, q, nil, archive.NoopArchiver{}, logger)
	task := GenerateTask{Payload: &queue.GenerateJobPayload{JobID: jobID, RawScript: "脚本"}}
	if err := proc.HandleGenerateJob(ctx, task); err != nil {
		t.Fatalf("执行生成任务失败: %v", err)
	}
	return st, jobID, ctx
}

// 端到端：Python 产出 -> gRPC 流 -> 事件转换 -> 落库 -> 读回成本快照。
func TestCostSurvivesRealGRPCTransport(t *testing.T) {
	st, jobID, ctx := applyCostEventsOverGRPC(t, func(jobID string) []*pb.PipelineEvent {
		return []*pb.PipelineEvent{
			{
				JobId: jobID, Node: "plan", TsUnixMs: time.Now().UnixMilli(),
				// 分镜表照抄 Python 的真实形状：tag/engine 是**数字枚举**。
				// 写成 "MATH" 这样的字符串会让反序列化整体失败，
				// 表现为「导演跑完了但一个镜头都没有」—— 两侧单测却都是绿的。
				PayloadJson: `{"outline":"大纲","shots":[{"shot_id":"s1","index":0,` +
					`"narration":"来自计划事件的画外音","visual_brief":"标题淡入",` +
					`"tag":1,"engine":1,"duration_sec":4.5,"keywords":["开场"]}]}`,
			},
			finalEvent(jobID, costPayload),
		}
	})

	job, err := st.GetJob(ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	if job.LLMUsage == nil {
		t.Fatal("经真实 gRPC 通道后用量丢失：说明 payload_json 没被正确传出或接收")
	}
	if job.LLMUsage.TotalTokens != 350 || job.LLMUsage.PromptTokens != 100 {
		t.Fatalf("跨通道后用量不符: %+v", job.LLMUsage)
	}
	// 分镜表与成本都必须活着到达，才算这条路径真的走通 ——
	// 只验成本的话，一个「事件整体没被消费」的实现也能碰巧通过（成本恰好来自别处）。
	shot := job.FindShot("s1")
	if shot == nil {
		t.Fatal("plan 事件里的分镜未同步")
	}
	if shot.Narration != "来自计划事件的画外音" {
		t.Fatalf("plan 事件内容未生效，实际 %q", shot.Narration)
	}

	// 分镜表是**整体替换**语义：plan 事件之后只应剩下它带来的那一个镜头。
	// 断言这一点是为了确认「事件真的被消费了」，而不是恰好成本来自别处。
	if len(job.Shots) != 1 || job.FindShot("s2") != nil {
		t.Fatalf("plan 事件未按整体替换语义生效: %d 个镜头", len(job.Shots))
	}
	// 推导项必须跟着**当前任务状态**走，这正是把它们做成就地现算的意义：
	//   - render_sec 来自被保留的产物（plan 重发不重置渲染结果，这是刻意的：
	//     否则断点续跑会白烧一遍渲染成本）；
	//   - tts_chars 来自**新的**画外音文本 —— 若实现改成在写路径上缓存，
	//     这里就会停留在旧文案的字数上。
	snap := job.CostSnapshot()
	if snap.RenderSec != 1.5 {
		t.Fatalf("render_sec 应来自被保留的产物（1.5），实际 %v", snap.RenderSec)
	}
	if want := len([]rune("来自计划事件的画外音")); snap.TTSChars != want {
		t.Fatalf("tts_chars 应跟随新的画外音文本（%d），实际 %d", want, snap.TTSChars)
	}
	if snap.TTSShots != 1 {
		t.Fatalf("tts_shots 应为 1，实际 %d", snap.TTSShots)
	}
}

// payload_json 是字符串字段。若它超过客户端接收上限，消息会被截断或拒收，
// 表现为「成本永远为 0」而没有任何明显报错 —— 这个用例把「上限」这件事钉住：
// 只要有人把 MaxRecvMsgSizeMB 调小到不合理，或者换成用 proto 字段传这些数据，它就会红。
func TestCostPayloadSurvivesLargePayload(t *testing.T) {
	big := make([]byte, 200_000)
	for i := range big {
		big[i] = 'x'
	}
	payload := `{"summary":{"note":"` + string(big) + `"},"cost":{"llm_prompt_tokens":11,` +
		`"llm_completion_tokens":22,"llm_total_tokens":33,"llm_calls":2}}`
	if len(payload) >= 8<<20 {
		t.Fatal("构造的 payload 超过了客户端上限，用例本身失效")
	}

	st, jobID, ctx := applyCostEventsOverGRPC(t, func(jobID string) []*pb.PipelineEvent {
		return []*pb.PipelineEvent{finalEvent(jobID, payload)}
	})

	job, err := st.GetJob(ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	if job.LLMUsage == nil || job.LLMUsage.TotalTokens != 33 {
		t.Fatalf("大 payload 中的用量丢失: %+v", job.LLMUsage)
	}
}
