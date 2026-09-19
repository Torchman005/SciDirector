package worker

// 周期性对账（阶段五）。
//
// ## 为什么需要"定期"而不是只在出问题时查
//
// 双写不一致的表现是**任务永远停在非终态**：用户看到进度 100% 却等不到成片，
// 而系统里没有任何错误。这种状态不会自己好，也不会有人主动去查 ——
// 它需要一个**周期性的**动作把它找出来。
//
// ## 为什么不用 Asynq 的 Scheduler
//
// Scheduler 会把"对账"变成队列里的一种任务，于是它与业务任务共享并发槽位：
// 一次积压（几十个渲染在排队）会让对账迟迟不跑，而那恰恰是最可能产生不一致、
// 也最需要它的时候。这里用一个独立的 ticker goroutine，
// 并给每次扫描一个**独立的超时**，与业务负载解耦。

import (
	"context"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/logging"
)

// sweepMaxJobs 是单次扫描最多检查的任务数。
//
// 必须有上限：对账要逐任务调一次 Python（读 Postgres），
// 任务多时一次扫描可能很久。宁可分多轮扫完，也不要让一轮扫描长时间占着
// 一个 goroutine 并让日志间隔变得不可预测。
const sweepMaxJobs = 50

// RunReconcileSweep 周期性扫描非终态任务并修复「卡住」的那一类。
//
// 阻塞直到 ctx 取消，供 cmd/worker 以 goroutine 方式调用。
// interval <= 0 表示不启用（缺省），这样单个部署可以通过配置关掉它 ——
// 多副本部署时每个副本都扫一遍是浪费，但**不会**造成错误结果
// （修复动作是幂等的：二次确认会跳过已经终态的任务）。
func (p *Processor) RunReconcileSweep(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		return
	}
	lg := logging.FromContext(ctx)
	lg.Info("状态对账已启用", "interval", interval.String())

	// 首次延迟一个周期再跑：worker 刚启动时往往正有一批任务在跑，
	// 立刻扫描只会把"刚开始、checkpoint 还没写"误报成不一致。
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			lg.Info("状态对账已停止")
			return
		case <-ticker.C:
			p.sweepOnce(ctx)
		}
	}
}

// sweepOnce 扫描一轮。**绝不向上抛异常**：对账是辅助能力，
// 它出错不该把 worker 拖下去。
func (p *Processor) sweepOnce(ctx context.Context) {
	lg := logging.FromContext(ctx)

	// 给单轮扫描一个独立超时，避免 Python 端卡住时这个 goroutine 永久挂起。
	scanCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()

	ids, err := p.store.ListActiveJobIDs(scanCtx, sweepMaxJobs)
	if err != nil {
		lg.Warn("对账扫描：列出在跑任务失败", "error", err.Error())
		return
	}
	if len(ids) == 0 {
		return
	}

	var checked, repaired, diverged int
	for _, jobID := range ids {
		if scanCtx.Err() != nil {
			lg.Warn("对账扫描超时，本轮提前结束", "checked", checked, "remaining", len(ids)-checked)
			break
		}
		outcome, rerr := p.reconciler.ReconcileJob(scanCtx, jobID, true)
		if rerr != nil {
			// 单个任务读不到 checkpoint（例如 AI 服务不可达）不该中断整轮扫描。
			lg.Warn("对账失败", "job_id", jobID, "error", rerr.Error())
			continue
		}
		checked++
		if !outcome.Report.Consistent() {
			diverged++
		}
		if outcome.Repaired {
			repaired++
		}
	}

	// 只在有值得注意的情况时打汇总，避免每个周期都刷一行日志。
	if diverged > 0 || repaired > 0 {
		lg.Warn("对账扫描完成：发现不一致",
			"checked", checked, "diverged", diverged, "repaired", repaired)
	} else {
		lg.Debug("对账扫描完成", "checked", checked)
	}
}
