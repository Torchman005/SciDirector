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

// jobActiveIndexKey 是**全局**在跑任务索引（用于状态对账）。
//
// 与租户集合分开：配额要按租户切分，而对账要一次拿到所有在跑任务 ——
// 用同一个集合就得先枚举租户，那又需要 SCAN。
const jobActiveIndexKey = "scid:jobs:active"

// pruneActive 剔除已经结束（或已不存在）的任务，返回**仍在跑**的任务 ID。
//
// 「已不存在」也剔除：任务可能因为保留期到期而被清理，那它显然不该继续占配额，
// 也不该被对账反复拿出来检查。
//
// 这是「自愈」的核心：任何一次读取都会顺手修正历史遗留的脏数据，
// 因此不需要"创建 +1、结束 -1"那样要求两侧都不出错的计数器。
func (s *Store) pruneActive(ctx context.Context, key string) ([]string, error) {
	ids, err := s.rdb.ZRange(ctx, key, 0, -1).Result()
	if err != nil {
		return nil, fmt.Errorf("store: 读取在跑任务失败: %w", err)
	}
	if len(ids) == 0 {
		return nil, nil
	}

	stale := make([]any, 0, len(ids))
	live := make([]string, 0, len(ids))
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
			continue
		}
		live = append(live, id)
	}
	if len(stale) > 0 {
		if err := s.rdb.ZRem(ctx, key, stale...).Err(); err != nil {
			return nil, fmt.Errorf("store: 清理已结束任务失败: %w", err)
		}
	}
	return live, nil
}

// CountActiveJobs 返回该租户当前在跑的任务数（会顺手清理已结束的条目）。
func (s *Store) CountActiveJobs(ctx context.Context, tenantID string) (int, error) {
	live, err := s.pruneActive(ctx, tenantActiveKey(tenantID))
	if err != nil {
		return 0, err
	}
	return len(live), nil
}

// ListActiveJobIDs 返回全局「在跑」的任务 ID（按创建时间升序，最多 limit 个）。
//
// 存在的原因：任务是以 `scid:job:<id>` 这种**单键**存的，没有任何索引，
// 因此「有哪些任务还在跑」这个问题无法直接回答。用 `SCAN` 遍历键空间可以做，
// 但那与键的总数成正比，且会扫到大量带 TTL 的历史任务 ——
// 在一个生产实例上做周期性的全量扫描并不合适。
//
// 改为维护一个全局在跑集合（与租户配额集合同一套自愈机制）：
// 写入时登记、读取时按任务**真实状态**剔除。
// 升序返回是刻意的：最早创建的任务最可能是「卡住」的那个，先修它。
func (s *Store) ListActiveJobIDs(ctx context.Context, limit int) ([]string, error) {
	live, err := s.pruneActive(ctx, jobActiveIndexKey)
	if err != nil {
		return nil, err
	}
	if limit > 0 && len(live) > limit {
		live = live[:limit]
	}
	return live, nil
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
	// 同时写两个集合：租户维度的用于配额，全局维度的用于状态对账。
	// 用 Pipeline 一次往返，避免为同一件事多付一次 RTT。
	pipe := s.rdb.Pipeline()
	z := redis.Z{Score: float64(at.Unix()), Member: jobID}
	pipe.ZAdd(ctx, tenantActiveKey(tenantID), z)
	pipe.ZAdd(ctx, jobActiveIndexKey, z)
	// 给个宽限期：任务最长的生命周期之外还留着无意义，且集合本来就靠现算自愈。
	pipe.Expire(ctx, tenantActiveKey(tenantID), 30*24*time.Hour)
	pipe.Expire(ctx, jobActiveIndexKey, 30*24*time.Hour)
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("store: 记录在跑任务失败: %w", err)
	}
	return nil
}
