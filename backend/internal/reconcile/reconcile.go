// Package reconcile 是状态对账的执行者：取两侧快照 -> 比对 -> （必要时）修复（阶段五）。
//
// 放在独立包里而不是 worker 里，是因为**两个进程都要用它**：
// worker 用它做周期扫描，api 用它提供按需对账接口。
// 留在 worker 里会让 api 反向依赖 worker（一个背着媒体/归档/队列的包），
// 而 api 只需要"store + ai 客户端"这两样东西。
//
// 分层：判断规则在 `domain.Reconcile`（纯函数，可完整单测），
// 这里只负责取数据、落数据、写事件。
//
// ## 自动修复的边界
//
// **只修一类**：Redis 非终态 + checkpoint 显示图已跑完（`DivStuckJob`）。
// 这一类的修复动作是"把一次没落地的状态迁移补完"，依据完全来自 Redis 自己
// 的镜头状态（`domain.TerminalStatusFromShots`），不引入 checkpoint 侧的信息
// 去覆盖对外状态 —— 后者等于"用内部状态改写用户看到的东西"，风险远大于它解决的问题。
//
// 其余各类**只报告不修**：它们的"正确做法"依赖运维判断
// （任务是不是该重投？checkpoint 是不是被误删？），自动动手比不动更危险。

package reconcile

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// Reconciler 持有对账所需的两样东西：对外状态（Redis）与续跑状态（AI 服务的 checkpoint）。
type Reconciler struct {
	store *store.Store
	ai    *ai.Client
	log   *slog.Logger
}

// New 构造对账器。
func New(st *store.Store, aiClient *ai.Client, logger *slog.Logger) *Reconciler {
	if logger == nil {
		logger = slog.Default()
	}
	return &Reconciler{store: st, ai: aiClient, log: logger}
}

// Outcome 是一次对账的结果（供日志与 HTTP 接口返回）。
type Outcome struct {
	Report domain.ReconcileReport `json:"report"`
	// Repaired 表示本次是否真的执行了修复。
	Repaired bool `json:"repaired"`
	// RepairedTo 是修复后的任务状态（未修复时为空）。
	RepairedTo domain.JobStatus `json:"repaired_to,omitempty"`
}

// ReconcileJob 对单个任务做一次对账（并可选择自动修复）。
//
// repair=false 时纯只读，用于"先看看有没有问题"的场景（HTTP 接口默认如此）。
func (r *Reconciler) ReconcileJob(ctx context.Context, jobID string, repair bool) (*Outcome, error) {
	job, err := r.store.GetJob(ctx, jobID)
	if err != nil {
		return nil, err
	}
	lg := logging.FromContext(ctx).With("job_id", jobID)

	snapshot, err := r.ai.GetCheckpointSnapshot(ctx, &pb.CheckpointSnapshotRequest{JobId: jobID})
	if err != nil {
		// 取不到 checkpoint 时不猜：如实返回错误，由调用方决定是重试还是记录。
		// 把它当成"没有差异"是最危险的处理方式 —— 那会让对账在 AI 服务不可用时
		// 永远显示"一切正常"。
		return nil, fmt.Errorf("reconcile: 读取 checkpoint 快照失败: %w", err)
	}

	cp := domain.CheckpointFacts{
		// Durable 由**后端类型**决定，而不是"found 是否为 true"：
		// 内存后端在进程重启后什么都读不到，那时 found=false 是预期行为，
		// 把它当差异会让报告在开发环境里永远有噪声（见 domain.DivUnknown）。
		Durable:  snapshot.GetBackend() == "postgres",
		Backend:  snapshot.GetBackend(),
		Detail:   snapshot.GetDetail(),
		Found:    snapshot.GetFound(),
		Finished: snapshot.GetFinished(),
		Cursor:   int(snapshot.GetCursor()),
		Shots:    make(map[string]domain.CheckpointShotFacts, len(snapshot.GetShots())),
	}
	for _, s := range snapshot.GetShots() {
		cp.Shots[s.GetShotId()] = domain.CheckpointShotFacts{
			Attempt:     int(s.GetAttempt()),
			HasArtifact: s.GetHasArtifact(),
		}
	}

	report := domain.Reconcile(job, cp, job.Status)

	// 对账本身不改变任务状态：这里的 report 是**观察结果**，
	// 因此即使报告"不一致"也不该有任何副作用（除非显式要求修复）。
	out := &Outcome{Report: report}

	if !repair {
		logIfDivergent(lg, out)
		return out, nil
	}

	for _, d := range report.Divergences {
		if d.Kind != domain.DivStuckJob || !d.Repairable {
			continue
		}
		// 依据 Redis 自己的镜头状态推出终态；用 UpdateJob 而不是 ForTenant ——
		// 这是系统级修复，不属于任何租户的请求上下文。
		updated, uerr := r.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
			// 二次确认：从读到写之间有窗口，任务可能已经自己走到了终态。
			// 不做这一步就会把一个已经正常的任务重新"修"一次，
			// 并发出一个多余且令人困惑的事件。
			if !domain.JobActive(j.Status) {
				return nil
			}
			j.Status = domain.TerminalStatusFromShots(j)
			j.Error = "" // 终态已按镜头状态推出，旧的临时错误不再适用
			return nil
		})
		if uerr != nil {
			return nil, fmt.Errorf("reconcile: 修复任务状态失败: %w", uerr)
		}
		if !domain.JobActive(updated.Status) {
			out.Repaired = true
			out.RepairedTo = updated.Status
			// 必须写事件：修复改变了用户看到的状态，而事件流是前端唯一的推送来源。
			// 不写事件的后果是"接口读出来已经完成，页面还停在 100% 转圈"。
			if _, eerr := r.store.AppendEvent(ctx, &domain.Event{
				JobID: jobID,
				Node:  "reconcile",
				Message: fmt.Sprintf(
					"对账发现流水线已结束但状态未落地，已将任务修正为 %s", updated.Status),
				Status:   "",
				Progress: updated.ProgressRatio(),
				Payload: map[string]any{
					"reason":    string(domain.DivStuckJob),
					"from":      string(job.Status),
					"to":        string(updated.Status),
					"backend":   cp.Backend,
					"reconcile": true,
				},
				Timestamp: time.Now().UTC(),
			}); eerr != nil {
				// 事件写入失败不推翻修复本身（状态已经改对了），但必须留痕。
				lg.Warn("写入对账修复事件失败", "error", eerr.Error())
			}
			lg.Warn("对账修复：任务状态已修正",
				"from", string(job.Status), "to", string(updated.Status))
		}
	}

	if !out.Repaired {
		logIfDivergent(lg, out)
	}
	return out, nil
}

// logIfDivergent 只在确有差异时打日志。
//
// 每个任务都打一条 INFO 会把日志淹掉；而对账的价值恰恰在于"有差异时能看见"。
func logIfDivergent(lg *slog.Logger, out *Outcome) {
	if out.Report.Consistent() {
		return
	}
	for _, d := range out.Report.Divergences {
		lg.Warn("对账发现不一致",
			"kind", string(d.Kind), "shot_id", d.ShotID,
			"repairable", d.Repairable, "detail", d.Message)
	}
}
