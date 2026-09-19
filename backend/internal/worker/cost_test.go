package worker

// 本文件覆盖**阶段五成本核算的落库路径**：`applyPipelineEvent` 收到带 cost 的
// final 事件后，LLM 用量是否真的写进了任务。
//
// 为什么用真实 Redis 而不是替身：
// 这里的判定依据是「重新从存储读出来的任务里有没有那份用量」——
// 也就是说，被测的正是**持久化**本身。用内存替身会把「写没写进去」这件事
// 一起假掉，测出来的绿说明不了任何问题。
//
// 覆盖范围：
//   - 任务级事件（无 shot_id）里的 cost 也会被记账（很容易被提前返回吞掉）
//   - 重复收到 final 事件时是**覆盖**而不是累加（断点续跑会重放）
//   - 无 cost 的事件不产生用量记录
//   - 坏 payload 不让事件丢失，但留下可见的解析错误

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	goredis "github.com/redis/go-redis/v9"

	"github.com/itJinYu/SciDirector/backend/internal/archive"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// costTestRedisDB 与本包其他集成测试共用 13 号库（见 idempotency_test.go 的说明）。
// 同一个包内的测试是串行的，因此可以共用；跨包必须错开。
func newCostHarness(t *testing.T) (*Processor, *store.Store, context.Context) {
	t.Helper()

	addr := requireRedis(t)
	ctx := context.Background()

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

	// 成本路径不碰 gRPC、队列与 ffmpeg，因此这三项保持 nil：
	// 一旦实现意外依赖它们，测试会立刻 panic 而不是悄悄走另一条路。
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	return NewProcessor(&config.Config{Env: "test"}, st, nil, nil, nil, archive.NoopArchiver{}, logger), st, ctx
}

func seedCostJob(t *testing.T, ctx context.Context, st *store.Store) string {
	t.Helper()
	job := &domain.Job{
		JobID:     "job-cost-1",
		RawScript: "脚本",
		Status:    domain.JobPlanning,
		Shots: []*domain.Shot{
			{ShotID: "s1", Narration: "第一段画外音", Status: domain.StatusApproved,
				Artifact: &domain.Artifact{RenderCostSec: 1.5, AudioPath: "/w/s1/speech.mp3"}},
			{ShotID: "s2", Narration: "第二段画外音", Status: domain.StatusRendering},
		},
		CreatedAt: time.Now().UTC(),
		UpdatedAt: time.Now().UTC(),
	}
	if err := st.SaveJob(ctx, job); err != nil {
		t.Fatalf("写入任务失败: %v", err)
	}
	return job.JobID
}

// 真实 Python 侧产出的 final 事件形状（见 ai/scidirector_ai/graph/builder.py）。
func finalEvent(jobID, payloadJSON string) *pb.PipelineEvent {
	return &pb.PipelineEvent{
		JobId:       jobID,
		Node:        "final",
		PayloadJson: payloadJSON,
		TsUnixMs:    time.Now().UnixMilli(),
	}
}

const costPayload = `{"summary":{"total_tokens":350,"calls":3},` +
	`"cost":{"llm_prompt_tokens":100,"llm_completion_tokens":250,` +
	`"llm_total_tokens":350,"llm_calls":3}}`

// final 事件通常不带 shot_id（它是任务级的）。若成本记账写在 shot_id 的
// 提前返回之后，这条用例就会失败 —— 而这正是实现时最容易踩的坑。
func TestApplyPipelineEventRecordsCostFromJobLevelEvent(t *testing.T) {
	proc, st, ctx := newCostHarness(t)
	jobID := seedCostJob(t, ctx, st)

	if err := proc.applyPipelineEvent(ctx, jobID, finalEvent(jobID, costPayload)); err != nil {
		t.Fatalf("应用 final 事件失败: %v", err)
	}

	// 重新读一遍：判定依据是**持久化**结果，不是内存里的对象。
	job, err := st.GetJob(ctx, jobID)
	if err != nil {
		t.Fatalf("重新读取任务失败: %v", err)
	}
	if job.LLMUsage == nil {
		t.Fatal("LLM 用量未写入任务")
	}
	if job.LLMUsage.TotalTokens != 350 || job.LLMUsage.Calls != 3 {
		t.Fatalf("LLM 用量不符: %+v", job.LLMUsage)
	}

	// 成本快照 = 持久化的 LLM 部分 + 现算的推导部分，两者必须能合起来读。
	snap := job.CostSnapshot()
	if snap.LLM.TotalTokens != 350 {
		t.Fatalf("快照丢失 LLM 部分: %+v", snap.LLM)
	}
	if snap.RenderSec != 1.5 {
		t.Fatalf("快照应现算出 1.5 秒渲染耗时，实际 %v", snap.RenderSec)
	}
	if snap.TTSShots != 1 || snap.TTSChars != len([]rune("第一段画外音")) {
		t.Fatalf("快照应只统计有配音的镜头: %+v", snap)
	}
	if snap.Shots != 2 || snap.Approved != 1 {
		t.Fatalf("规模指标不符: %+v", snap)
	}
}

// 断点续跑会重放 final 事件。若实现成累加，成本会随重放次数翻倍。
func TestApplyPipelineEventCostIsIdempotentAcrossReplay(t *testing.T) {
	proc, st, ctx := newCostHarness(t)
	jobID := seedCostJob(t, ctx, st)

	for i := 0; i < 3; i++ {
		if err := proc.applyPipelineEvent(ctx, jobID, finalEvent(jobID, costPayload)); err != nil {
			t.Fatalf("第 %d 次应用事件失败: %v", i+1, err)
		}
	}

	job, err := st.GetJob(ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	// 先判 nil 再取字段：实现出错时这里应当是**干净的失败**。
	// 直接解引用会 panic，而 panic 会中断整个包的测试运行，
	// 后面还没跑的用例根本不执行 —— 排查的人只会看到一堆无关堆栈。
	if job.LLMUsage == nil {
		t.Fatal("重放后用量丢失")
	}
	if job.LLMUsage.TotalTokens != 350 || job.LLMUsage.Calls != 3 {
		t.Fatalf("重放后用量被累加: %+v", job.LLMUsage)
	}
}

// 反向控制：没有 cost 的事件不该记账，否则「跑没跑过 LLM」就无法判断。
func TestApplyPipelineEventWithoutCostRecordsNothing(t *testing.T) {
	proc, st, ctx := newCostHarness(t)
	jobID := seedCostJob(t, ctx, st)

	ev := &pb.PipelineEvent{JobId: jobID, Node: "plan", PayloadJson: `{"shots":[]}`}
	if err := proc.applyPipelineEvent(ctx, jobID, ev); err != nil {
		t.Fatalf("应用 plan 事件失败: %v", err)
	}

	job, err := st.GetJob(ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	if job.LLMUsage != nil {
		t.Fatalf("无 cost 的事件不该产生用量记录: %+v", job.LLMUsage)
	}
}

// 坏 payload 不能让事件丢失（状态迁移必须照常发生），但错误要看得见。
func TestApplyPipelineEventSurvivesBadPayloadAndStillTransitions(t *testing.T) {
	proc, st, ctx := newCostHarness(t)
	jobID := seedCostJob(t, ctx, st)

	ev := &pb.PipelineEvent{
		JobId:       jobID,
		ShotId:      "s2",
		Node:        "final",
		Status:      pb.ShotStatus_SHOT_STATUS_APPROVED,
		PayloadJson: `{"cost": {`,
		TsUnixMs:    time.Now().UnixMilli(),
	}
	if err := proc.applyPipelineEvent(ctx, jobID, ev); err != nil {
		t.Fatalf("坏 payload 不该让事件整体失败: %v", err)
	}

	job, err := st.GetJob(ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	shot := job.FindShot("s2")
	if shot == nil {
		t.Fatal("找不到镜头 s2")
	}
	if shot.Status != domain.StatusApproved {
		t.Fatalf("状态迁移未生效，实际 %q", shot.Status)
	}
	if job.LLMUsage != nil {
		t.Fatalf("解析失败时不该写入用量: %+v", job.LLMUsage)
	}
}
