package store

// 租户配额的状态：当前「在跑」的任务集合（阶段五·多租户）。
//
// ## 为什么用「现算」而不是「计数器」
//
// 计数器（创建时 +1、结束时 -1）需要**两侧都别忘了**：worker 崩溃、任务被手工清理、
// 进程中途重启，都会让计数永远偏高 —— 而表现是「这个租户再也提交不了任务」，
// 且没有任何日志能说明为什么。这类「泄漏的计数」在生产上极难排查。
//
// 这里改成维护一个**集合**（jobID -> 创建时间），每次检查时按任务的**真实状态**
// 剔除已经结束的条目。集合因此是自愈的：任何一次检查都会顺手修正历史遗留的脏数据。
// 代价是每次创建任务要多读几次 Redis（上限 = 配额 + 少量），相对于一次视频生成
// 的耗时可以忽略。
//
// ## 键名带租户前缀
//
// 与任务本身的键（`scid:job:<id>`，不带租户）不同，这里**带**租户前缀。
// 原因：这个键的内容本来就是「按租户聚合」的，前缀让它天然按租户隔离，
// 也便于按租户做运维（`SCAN scid:tenant:*`）。

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

func tenantActiveKey(tenantID string) string {
	return "scid:tenant:" + tenantID + ":active"
}

// pruneActive 剔除已经结束（或已不存在）的任务，返回仍在跑的数量。
//
// 「已不存在」也剔除：任务可能因为保留期到期而被清理，那它显然不该继续占配额。
func (s *Store) pruneActive(ctx context.Context, tenantID string) (int, error) {
	key := tenantActiveKey(tenantID)

	ids, err := s.rdb.ZRange(ctx, key, 0, -1).Result()
	if err != nil {
		return 0, fmt.Errorf("store: 读取租户在跑任务失败: %w", err)
	}
	if len(ids) == 0 {
		return 0, nil
	}

	stale := make([]any, 0, len(ids))
	for _, id := range ids {
		job, err := s.GetJob(ctx, id)
		if err != nil {
			// 读不到就当作已结束：宁可少算一个（放行）也不要多算（误拒）。
			// 误拒的表现是「用户提交不了任务且不知为何」，比偶尔放宽一次严重得多。
			stale = append(stale, id)
			continue
		}
		if !domain.JobActive(job.Status) {
			stale = append(stale, id)
		}
	}
	if len(stale) > 0 {
		if err := s.rdb.ZRem(ctx, key, stale...).Err(); err != nil {
			return 0, fmt.Errorf("store: 清理已结束任务失败: %w", err)
		}
	}
	return len(ids) - len(stale), nil
}

// CountActiveJobs 返回该租户当前在跑的任务数（会顺手清理已结束的条目）。
func (s *Store) CountActiveJobs(ctx context.Context, tenantID string) (int, error) {
	return s.pruneActive(ctx, tenantID)
}

// TrackActiveJob 把一个新任务记入租户的在跑集合。
//
// 即使这一步失败也不该阻断任务创建：配额是**保护性**能力，
// 它自己出问题时正确的行为是「这次不计数」，而不是让用户提交不了任务。
// 因此调用方应当记录警告并继续 —— 下一轮检查会按真实状态重新算出来。
func (s *Store) TrackActiveJob(ctx context.Context, tenantID, jobID string, at time.Time) error {
	if tenantID == "" || jobID == "" {
		return nil
	}
	if err := s.rdb.ZAdd(ctx, tenantActiveKey(tenantID), redis.Z{
		Score:  float64(at.Unix()),
		Member: jobID,
	}).Err(); err != nil {
		return fmt.Errorf("store: 记录租户在跑任务失败: %w", err)
	}
	// 给个宽限期：任务最长的生命周期之外还留着无意义，且集合本来就靠现算自愈。
	_ = s.rdb.Expire(ctx, tenantActiveKey(tenantID), 30*24*time.Hour).Err()
	return nil
}
