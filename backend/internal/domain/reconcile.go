// 状态对账：比对 Go 的 Redis 状态与 LangGraph 的 checkpoint（阶段五）。
//
// ## 为什么会有不一致
//
// 这是**刻意的双写**（见 docs/DESIGN.md）：Redis 保存「对外可见的任务/镜头状态」，
// checkpoint 保存「图能从哪里续跑」。两者以 job_id 对齐、由事件流单向同步 ——
// 也就是说 Redis 是**被事件推着走**的。于是只要事件在中途丢了（进程被杀、
// 订阅断了、写事件失败但任务状态已推进），两侧就会分叉，而**没有任何一处会报错**。
//
// ## 判断规则是纯函数
//
// 「什么算不一致」必须可被完整单测：它一旦算错，表现是「对账报告全是误报」，
// 而误报会让人干脆不再看这份报告 —— 那比没有对账更糟。
// 因此这里不碰 IO：输入是两侧的快照，输出是差异列表。

package domain

import (
	"fmt"
	"sort"
)

// DivergenceKind 是差异的种类。
//
// 用字符串而不是枚举常量再映射：它要进日志与 JSON，
// 而"种类"是会增加的（将来可能加新检查项），把每次新增都变成"改两处映射"
// 只会让人倾向于不复用这个机制。
type DivergenceKind string

const (
	// DivUnknown 表示无法判断。
	//
	// 单独成一类而不是"没问题"：checkpoint 后端不是持久化的时候（MemorySaver），
	// 进程重启后什么都读不到 —— 那时「checkpoint 里没有这个线程」是**预期**的，
	// 不是不一致。把它算成不一致会让报告在开发环境里永远有噪声。
	DivUnknown DivergenceKind = "unknown"
	// DivCheckpointMissing：Redis 认为任务还在跑，但 checkpoint 里没有该线程。
	// 可能是任务刚入队还没开始，也可能是 checkpoint 已被清理 —— 需要结合
	// 「任务创建多久了」判断，因此这里只报告、不自动修。
	DivCheckpointMissing DivergenceKind = "checkpoint_missing"
	// DivStuckJob：Redis 里是非终态，而 checkpoint 显示图**已经跑完**。
	//
	// 这是最典型、也最值得自动修的一类：流水线结束了，但最后那次状态迁移
	// 没落到 Redis。表现为任务永远停在 RENDERING，用户看着 100% 却等不到成片。
	DivStuckJob DivergenceKind = "stuck_job"
	// DivCheckpointAhead：Redis 已是终态，而 checkpoint 显示图还没跑完。
	// 这种情况**不能**自动修：图可能还会继续推进并发出新事件，
	// 把 Redis 拉回非终态是危险的（用户可能已经看到"已完成"）。
	DivCheckpointAhead DivergenceKind = "checkpoint_ahead"
	// DivProgressAhead：checkpoint 的游标比 Redis 认识的镜头数更靠前。
	// 说明 Redis 丢过分镜表事件。只报告：补分镜需要重放 plan 事件，不该在这里凭空造。
	DivProgressAhead DivergenceKind = "progress_ahead"
	// DivShotUnknownToRedis：checkpoint 里有 Redis 不知道的镜头。
	DivShotUnknownToRedis DivergenceKind = "shot_unknown_to_redis"
	// DivArtifactOnOneSide：checkpoint 说某镜头有产物，Redis 里却没有（或反之）。
	// 只报告：两侧的"产物"含义不同（checkpoint 记的是图内产出，Redis 记的是可用成片）。
	DivArtifactOnOneSide DivergenceKind = "artifact_on_one_side"
)

// Divergence 是一条具体差异。
type Divergence struct {
	Kind    DivergenceKind `json:"kind"`
	ShotID  string         `json:"shot_id,omitempty"`
	Message string         `json:"message"`
	// Repairable 表示这条差异有**安全**的自动修复动作。
	Repairable bool `json:"repairable"`
}

// ReconcileReport 是一次对账的结论。
type ReconcileReport struct {
	JobID string `json:"job_id"`
	// CheckpointBackend 与 CheckpointDetail 如实带出：报告必须能自我解释
	// 「为什么这里写着 unknown」，否则读报告的人还得去翻服务日志。
	CheckpointBackend string       `json:"checkpoint_backend"`
	CheckpointDetail  string       `json:"checkpoint_detail"`
	RedisStatus       JobStatus    `json:"redis_status"`
	Divergences       []Divergence `json:"divergences"`
}

// Consistent 报告是否没有发现差异（unknown 不算差异，见 DivUnknown 的说明）。
func (r ReconcileReport) Consistent() bool {
	for _, d := range r.Divergences {
		if d.Kind != DivUnknown {
			return false
		}
	}
	return true
}

// CheckpointFacts 是「checkpoint 侧」对账所需的事实（由 Python 提供，见 proto）。
type CheckpointFacts struct {
	// Durable 表示后端是否持久化。非持久时无法对账 —— 必须显式传进来，
	// 而不是让本函数去猜后端名字：猜错的表现是"把预期行为报成不一致"。
	Durable bool
	Backend string
	Detail  string
	// Found 表示 checkpoint 里是否存在该线程。
	Found    bool
	Finished bool
	Cursor   int
	Shots    map[string]CheckpointShotFacts
}

// CheckpointShotFacts 是单个镜头在 checkpoint 侧的事实。
type CheckpointShotFacts struct {
	Attempt     int
	HasArtifact bool
}

// Reconcile 比对两侧快照，返回差异列表。
//
// 纯函数：不碰 IO、不看时间（「任务创建多久了」这类判断由调用方在决定是否
// 自动修之前做，避免把时间因素混进"有没有差异"这个纯逻辑里）。
func Reconcile(job *Job, cp CheckpointFacts, redisStatus JobStatus) ReconcileReport {
	report := ReconcileReport{
		CheckpointBackend: cp.Backend,
		CheckpointDetail:  cp.Detail,
		RedisStatus:       redisStatus,
	}
	if job == nil {
		return report
	}
	report.JobID = job.JobID

	// 后端不持久时，对账**根本无从谈起**：内存 checkpoint 在进程重启后什么都没有，
	// 这不是"不一致"，而是"没有可比对象"。如实说明，而不是报一堆假的差异。
	if !cp.Durable {
		report.Divergences = append(report.Divergences, Divergence{
			Kind: DivUnknown,
			Message: fmt.Sprintf("checkpoint 后端为 %s（非持久化），无法对账：%s",
				orDefault(cp.Backend, "未知"), orDefault(cp.Detail, "进程重启后状态即丢失")),
		})
		return report
	}

	redisTerminal := !JobActive(redisStatus)

	if !cp.Found {
		if redisTerminal {
			// 任务已结束、checkpoint 也没了：正常（可能已过保留期），不报差异。
			return report
		}
		report.Divergences = append(report.Divergences, Divergence{
			Kind: DivCheckpointMissing,
			Message: "Redis 中任务未结束，但 checkpoint 里没有该线程：" +
				"可能尚未开始，也可能是 checkpoint 已被清理（需要看任务已创建多久）",
		})
		return report
	}

	switch {
	case !redisTerminal && cp.Finished:
		// 最典型的双写不一致：图跑完了，最后的状态迁移没落到 Redis。
		report.Divergences = append(report.Divergences, Divergence{
			Kind: DivStuckJob,
			Message: fmt.Sprintf(
				"checkpoint 显示图已跑完，而 Redis 仍为 %s：最后的状态迁移丢失（任务会永远停在非终态）",
				redisStatus),
			Repairable: true,
		})
	case redisTerminal && !cp.Finished:
		report.Divergences = append(report.Divergences, Divergence{
			Kind: DivCheckpointAhead,
			Message: fmt.Sprintf(
				"Redis 已是终态 %s，而 checkpoint 显示图尚未跑完：图可能继续推进并发出新事件",
				redisStatus),
		})
	}

	// 游标比 Redis 的镜头数更靠前 ⇒ Redis 丢过分镜表事件。
	if cp.Cursor > len(job.Shots) {
		report.Divergences = append(report.Divergences, Divergence{
			Kind: DivProgressAhead,
			Message: fmt.Sprintf(
				"checkpoint 游标=%d 超过了 Redis 的镜头数=%d：分镜表事件可能丢失",
				cp.Cursor, len(job.Shots)),
		})
	}

	// 逐镜头比对。**只比对两侧都认识的事实**，不做状态推导：
	// 「镜头算 APPROVED 还是 AWAITING_HUMAN」属于状态机，两侧各推一份迟早会分叉。
	redisShots := make(map[string]*Shot, len(job.Shots))
	for _, s := range job.Shots {
		if s != nil {
			redisShots[s.ShotID] = s
		}
	}

	ids := make([]string, 0, len(cp.Shots))
	for id := range cp.Shots {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	for _, id := range ids {
		facts := cp.Shots[id]
		shot, ok := redisShots[id]
		if !ok {
			report.Divergences = append(report.Divergences, Divergence{
				Kind:    DivShotUnknownToRedis,
				ShotID:  id,
				Message: "checkpoint 里有该镜头，Redis 中不存在（分镜表事件可能丢失）",
			})
			continue
		}
		hasArtifact := shot.Artifact != nil
		if facts.HasArtifact != hasArtifact {
			report.Divergences = append(report.Divergences, Divergence{
				Kind:   DivArtifactOnOneSide,
				ShotID: id,
				Message: fmt.Sprintf("产物存在性不一致：checkpoint=%v Redis=%v",
					facts.HasArtifact, hasArtifact),
			})
		}
	}
	return report
}

func orDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

// ---------------------------------------------------------------------------
// 修复
// ---------------------------------------------------------------------------

// TerminalStatusFromShots 依据**Redis 自己的镜头状态**推出任务应有的终态。
//
// 这是唯一自动修复动作的依据，因此它必须只用 Redis 侧的数据：
// 这样"修复"只是把一次**没落地的状态迁移补完**，而不是引入 checkpoint 侧的信息
// 去覆盖对外状态 —— 后者会让对账变成"用内部状态改写用户看到的东西"，
// 风险远大于它解决的问题。
func TerminalStatusFromShots(job *Job) JobStatus {
	if job == nil || len(job.Shots) == 0 {
		// 没有任何镜头却已经跑完：流水线没产出可交付内容。
		return JobFailed
	}
	st := job.Stat()
	switch {
	case st.Approved == st.Total && st.Total > 0:
		return JobCompleted
	case st.AwaitingHuman > 0:
		// 与 worker 的收尾判断保持一致：有镜头熔断转人工 ⇒ PARTIAL，
		// 因为大部分镜头可能是好的，整片仍有交付价值。
		return JobPartial
	default:
		return JobFailed
	}
}
