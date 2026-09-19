// Package store 提供基于 Redis 的任务/分镜状态仓储。
//
// 设计要点：
//  1. 状态模型：整个 Job（含其所有 Shot）以一份 JSON 快照存储。
//     理由：读写模式天然是「按 job 整体读取」，分片存储反而会引入多键一致性问题。
//     代价：并发写同一 job 会互相覆盖 —— 用分布式锁 + UpdateJob 串行化来消除。
//  2. 事件流单独存 List，带自增 EventID，支持 WebSocket 断线重连后的增量重放。
//  3. 通过 Pub/Sub 广播事件，使「worker 实例」与「api 实例」可以是不同进程/机器。
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

// ErrJobNotFound 表示 Redis 中不存在该任务。
// 用哨兵错误而非裸字符串，方便上层 errors.Is 判断并映射为 HTTP 404。
var ErrJobNotFound = errors.New("store: 任务不存在")

// ErrJobConflict 表示任务状态被其他实例并发修改（乐观锁冲突），调用方可重试。
var ErrJobConflict = errors.New("store: 任务被并发修改")

// 键名规则集中在此处定义，避免各调用点拼字符串导致读写出错。
const (
	keyPrefixJob      = "scid:job:"
	keyPrefixEvents   = "scid:job:%s:events"
	keyPrefixEventSeq = "scid:job:%s:event_seq"
	keyPrefixLock     = "scid:lock:job:%s"
	keyPrefixStream   = "scid:job:%s:stream"
	keyPrefixIdem     = "scid:idem:%s"
)

// 事件流保留上限：防止超长任务把 Redis 内存吃满。
// 1000 条足够覆盖一次完整生成的所有迁移（镜头数 × 尝试数 × 节点数）。
const maxEventsRetained = 1000

// lockTTL 是任务级分布式锁的租期。必须大于单次状态更新的耗时，
// 又不能过长以免进程崩溃后长时间卡死任务。
const lockTTL = 20 * time.Second

// Store 是 Redis 仓储门面。
type Store struct {
	rdb *redis.Client
}

// New 构造仓储并做一次连通性探测，让配置错误在启动期暴露。
func New(ctx context.Context, cfg config.RedisConfig) (*Store, error) {
	rdb := redis.NewClient(&redis.Options{
		Addr:     cfg.Addr,
		Password: cfg.Password,
		DB:       cfg.DB,
		PoolSize: cfg.PoolSize,
	})
	pingCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := rdb.Ping(pingCtx).Err(); err != nil {
		_ = rdb.Close()
		return nil, fmt.Errorf("store: 连接 Redis %s 失败: %w", cfg.Addr, err)
	}
	return &Store{rdb: rdb}, nil
}

// Client 暴露底层客户端，供 Asynq 初始化复用连接配置。
func (s *Store) Client() *redis.Client { return s.rdb }

// Close 释放连接池。
func (s *Store) Close() error { return s.rdb.Close() }

// ---------------------------------------------------------------------------
// 任务读写
// ---------------------------------------------------------------------------

func jobKey(jobID string) string { return keyPrefixJob + jobID }

// SaveJob 直接覆盖写入任务快照。**仅供创建任务与测试使用**；
// 运行期的状态变更一律走 UpdateJob 以避免丢失更新。
func (s *Store) SaveJob(ctx context.Context, job *domain.Job) error {
	job.UpdatedAt = time.Now().UTC()
	buf, err := json.Marshal(job)
	if err != nil {
		return fmt.Errorf("store: 序列化任务失败: %w", err)
	}
	// 任务快照保留 7 天：足够人工事后复盘，又不至于无限膨胀。
	if err := s.rdb.Set(ctx, jobKey(job.JobID), buf, 7*24*time.Hour).Err(); err != nil {
		return fmt.Errorf("store: 写入任务失败: %w", err)
	}
	return nil
}

// GetJob 读取任务快照。不存在时返回 ErrJobNotFound。
func (s *Store) GetJob(ctx context.Context, jobID string) (*domain.Job, error) {
	buf, err := s.rdb.Get(ctx, jobKey(jobID)).Bytes()
	if errors.Is(err, redis.Nil) {
		return nil, ErrJobNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("store: 读取任务失败: %w", err)
	}
	var job domain.Job
	if err := json.Unmarshal(buf, &job); err != nil {
		return nil, fmt.Errorf("store: 反序列化任务失败: %w", err)
	}
	return &job, nil
}

// GetJobForTenant 读取任务并**校验归属**（阶段五·多租户）。
//
// 这是所有「按 job_id 访问」的对外入口，把归属检查放在**存储层**而不是各个 handler 里，
// 是为了让它无法被遗漏：handler 有八九个（查询、事件、成本、三个 HITL 操作、WS），
// 逐个记得写检查必然会漏一个 —— 而漏掉的表现是「某个接口能读到别人的任务」，
// 既不会报错也不会失败，只会在某次审计里被发现。
//
// 两种失败明确区分：任务不存在返回 ErrJobNotFound，
// 存在但不属于该租户返回 ErrTenantMismatch（由 HTTP 层统一映射成同一个 404，
// 对外不可区分，避免用 404/403 的差异枚举出哪些 job_id 存在）。
func (s *Store) GetJobForTenant(ctx context.Context, jobID, tenantID string) (*domain.Job, error) {
	job, err := s.GetJob(ctx, jobID)
	if err != nil {
		return nil, err
	}
	if !domain.JobBelongsTo(job, tenantID) {
		// 不返回 job 内容 —— 越权路径上一个字段都不该泄漏出去。
		return nil, fmt.Errorf("%w: job=%s", domain.ErrTenantMismatch, jobID)
	}
	return job, nil
}

// UpdateJob 以「加锁 + 读改写」的方式原子更新任务。
//
// mutate 函数在持有分布式锁的情况下被调用；返回错误则不写回（更新被放弃）。
// 这是本系统唯一允许修改已存在任务的入口，杜绝丢失更新。
func (s *Store) UpdateJob(ctx context.Context, jobID string, mutate func(*domain.Job) error) (*domain.Job, error) {
	return s.updateJob(ctx, jobID, "", mutate)
}

// UpdateJobForTenant 与 UpdateJob 相同，但**在锁内**校验归属（阶段五·多租户）。
//
// 校验必须放在锁内，不能放在调用方「先查再改」：
// 两步之间任务可能被删掉或改归属，检查通过之后的操作对象已经不是被检查的对象
// —— 这正是经典的 TOCTOU。放在 mutate 之前、同一次加锁之内，才真正是原子的。
func (s *Store) UpdateJobForTenant(ctx context.Context, jobID, tenantID string, mutate func(*domain.Job) error) (*domain.Job, error) {
	return s.updateJob(ctx, jobID, tenantID, mutate)
}

// updateJob 是 UpdateJob / UpdateJobForTenant 的共同实现。
// tenantID 为空表示不校验归属（内部调用、系统补偿任务走这条）。
func (s *Store) updateJob(ctx context.Context, jobID, tenantID string, mutate func(*domain.Job) error) (*domain.Job, error) {
	unlock, err := s.acquireLock(ctx, jobID)
	if err != nil {
		return nil, err
	}
	defer unlock()

	// 读**原始字节**而不是直接反序列化：写回时要把"我们不认识的字段"原样带回去，
	// 见 mergeUnknownFields 的说明。
	raw, err := s.rdb.Get(ctx, jobKey(jobID)).Bytes()
	if errors.Is(err, redis.Nil) {
		return nil, ErrJobNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("store: 读取任务失败: %w", err)
	}
	var job domain.Job
	if err := json.Unmarshal(raw, &job); err != nil {
		return nil, fmt.Errorf("store: 反序列化任务失败: %w", err)
	}
	if tenantID != "" && !domain.JobBelongsTo(&job, tenantID) {
		return nil, fmt.Errorf("%w: job=%s", domain.ErrTenantMismatch, jobID)
	}
	if err := mutate(&job); err != nil {
		return nil, err
	}

	// 进度是派生值：统一在此重算，避免各调用点忘记更新导致前端进度回跳。
	job.Progress = job.ProgressRatio()
	job.UpdatedAt = time.Now().UTC()
	buf, err := json.Marshal(&job)
	if err != nil {
		return nil, fmt.Errorf("store: 序列化任务失败: %w", err)
	}
	buf, err = mergeUnknownFields(raw, buf)
	if err != nil {
		return nil, err
	}
	if err := s.rdb.Set(ctx, jobKey(job.JobID), buf, 7*24*time.Hour).Err(); err != nil {
		return nil, fmt.Errorf("store: 写回任务失败: %w", err)
	}
	return &job, nil
}

// mergeUnknownFields 把旧 JSON 里、新 JSON 中**没有的**键原样保留下来。
//
// ## 为什么必须这么做（一个真实发生过的授权降级）
//
// 任务是整份 JSON 存在 Redis 里的，而**多个进程**都会读改写它（api 与 worker）。
// 于是「给结构体加一个字段」在不同版本并存时不是向后兼容的：
// 旧版本的进程把 JSON 反序列化进**不认识该字段**的结构体，再整份写回 ——
// 那个字段就被静默抹掉了。
//
// 本项目真实踩到：新增 tenant_id 之后只重启了 api，仍在跑的旧 worker
// 一碰任务就把 tenant_id 抹掉；而读取时「缺失 = default 租户」，
// 于是任务的主人反而读不到自己的任务（表现为 404），
// 并且它对 default 租户变得可见 —— 这是**授权降级**，不只是数据丢失。
//
// 保留未知字段让加字段变成真正的滚动兼容：新旧版本并存期间，
// 各自只改自己认识的字段，谁都不会把对方的抹掉。
// （代价是无法通过删字段来清理数据；要删就得显式处理，这比静默丢失好得多。）
func mergeUnknownFields(oldRaw, newRaw []byte) ([]byte, error) {
	var oldMap, newMap map[string]json.RawMessage
	if err := json.Unmarshal(oldRaw, &oldMap); err != nil {
		// 旧值不是合法 JSON 时不做合并：宁可写新值，也不要因为脏数据而更新失败。
		return newRaw, nil //nolint:nilerr // 见上：脏数据不应阻断更新
	}
	if err := json.Unmarshal(newRaw, &newMap); err != nil {
		return nil, fmt.Errorf("store: 序列化任务失败: %w", err)
	}
	changed := false
	for k, v := range oldMap {
		if _, ok := newMap[k]; !ok {
			newMap[k] = v
			changed = true
		}
	}
	if !changed {
		return newRaw, nil
	}
	merged, err := json.Marshal(newMap)
	if err != nil {
		return nil, fmt.Errorf("store: 合并未知字段失败: %w", err)
	}
	return merged, nil
}

// ---------------------------------------------------------------------------
// 事件流
// ---------------------------------------------------------------------------

// AppendEvent 追加一条事件并广播。
//
// 三步操作（分配 ID -> 入 List -> 发布）不是原子的，但语义上安全：
// 即使发布失败，事件也已持久化，重连的客户端可通过 ListEvents 补齐。
// 反过来若先发布后落库，客户端就可能收到一条永远不在历史里的事件（更糟）。
func (s *Store) AppendEvent(ctx context.Context, ev *domain.Event) (*domain.Event, error) {
	if ev.JobID == "" {
		return nil, errors.New("store: 事件缺少 job_id")
	}
	if ev.Timestamp.IsZero() {
		ev.Timestamp = time.Now().UTC()
	}

	seqKey := fmt.Sprintf(keyPrefixEventSeq, ev.JobID)
	id, err := s.rdb.Incr(ctx, seqKey).Result()
	if err != nil {
		return nil, fmt.Errorf("store: 分配事件序号失败: %w", err)
	}
	ev.EventID = id

	buf, err := json.Marshal(ev)
	if err != nil {
		return nil, fmt.Errorf("store: 序列化事件失败: %w", err)
	}

	listKey := fmt.Sprintf(keyPrefixEvents, ev.JobID)
	pipe := s.rdb.Pipeline()
	pipe.LPush(ctx, listKey, buf)
	pipe.LTrim(ctx, listKey, 0, maxEventsRetained-1)
	pipe.Expire(ctx, listKey, 7*24*time.Hour)
	pipe.Expire(ctx, seqKey, 7*24*time.Hour)
	// Publish 与业务写在同一 pipeline 中执行，减少往返；失败不致命。
	pipe.Publish(ctx, fmt.Sprintf(keyPrefixStream, ev.JobID), buf)
	if _, err := pipe.Exec(ctx); err != nil {
		return nil, fmt.Errorf("store: 写入事件失败: %w", err)
	}
	return ev, nil
}

// ListEvents 返回 jobID 下 eventID > afterID 的事件，按时间升序（旧 -> 新）。
// afterID = 0 表示取全量历史，用于「加入即重放」。
func (s *Store) ListEvents(ctx context.Context, jobID string, afterID int64) ([]domain.Event, error) {
	listKey := fmt.Sprintf(keyPrefixEvents, jobID)
	// List 用 LPUSH 写入，因此索引 0 是最新；LRange 取全量后在内存中反转为升序。
	bufs, err := s.rdb.LRange(ctx, listKey, 0, -1).Result()
	if err != nil {
		return nil, fmt.Errorf("store: 读取事件流失败: %w", err)
	}
	out := make([]domain.Event, 0, len(bufs))
	for i := len(bufs) - 1; i >= 0; i-- {
		var ev domain.Event
		if err := json.Unmarshal([]byte(bufs[i]), &ev); err != nil {
			// 单条损坏不应导致整条流不可用：跳过并继续。
			continue
		}
		if ev.EventID > afterID {
			out = append(out, ev)
		}
	}
	return out, nil
}

// Subscribe 订阅某个任务的事件广播，返回 channel 与取消函数。
// 用于多实例部署时 api 与 worker 分离的场景。
func (s *Store) Subscribe(ctx context.Context, jobID string) (<-chan domain.Event, func()) {
	sub := s.rdb.Subscribe(ctx, fmt.Sprintf(keyPrefixStream, jobID))
	ch := make(chan domain.Event, 64)

	subCtx, cancel := context.WithCancel(ctx)
	go func() {
		defer close(ch)
		defer func() { _ = sub.Close() }()
		for msg := range sub.Channel() {
			var ev domain.Event
			if err := json.Unmarshal([]byte(msg.Payload), &ev); err != nil {
				continue
			}
			select {
			case ch <- ev:
			case <-subCtx.Done():
				return
			}
		}
	}()
	return ch, cancel
}

// ---------------------------------------------------------------------------
// 幂等与锁
// ---------------------------------------------------------------------------

// AcquireIdempotencyKey 尝试占位一个幂等键。
//
// 场景：Asynq 在「任务已执行但确认失败」时会重新投递，导致同一镜头被重复渲染。
// 用 SETNX + TTL 保证「同一 job + shot + attempt」只被处理一次，直接省下重复的渲染成本。
func (s *Store) AcquireIdempotencyKey(ctx context.Context, key string, ttl time.Duration) (bool, error) {
	ok, err := s.rdb.SetNX(ctx, fmt.Sprintf(keyPrefixIdem, key), time.Now().UTC().Format(time.RFC3339Nano), ttl).Result()
	if err != nil {
		return false, fmt.Errorf("store: 幂等占位失败: %w", err)
	}
	return ok, nil
}

// ReleaseIdempotencyKey 在任务真正失败时释放占位，允许后续重试。
// 成功路径不释放，让 TTL 自然过期即可。
func (s *Store) ReleaseIdempotencyKey(ctx context.Context, key string) error {
	return s.rdb.Del(ctx, fmt.Sprintf(keyPrefixIdem, key)).Err()
}

// acquireLock 获取任务级分布式锁。
//
// 实现：SET NX PX + 唯一 token；释放时用 Lua 校验 token，防止「锁过期后被别人拿到，
// 自己却又把别人的锁删掉」这一经典错误。
func (s *Store) acquireLock(ctx context.Context, jobID string) (func(), error) {
	lockKey := fmt.Sprintf(keyPrefixLock, jobID)
	token := strconv.FormatInt(time.Now().UnixNano(), 36) + "-" + jobID

	deadline := time.Now().Add(10 * time.Second)
	backoff := 20 * time.Millisecond
	for {
		ok, err := s.rdb.SetNX(ctx, lockKey, token, lockTTL).Result()
		if err != nil {
			return nil, fmt.Errorf("store: 获取任务锁失败: %w", err)
		}
		if ok {
			unlock := func() {
				// 用独立 context：即使调用方的 ctx 已取消，也要尽力释放锁。
				relCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				_ = releaseLockScript.Run(relCtx, s.rdb, []string{lockKey}, token).Err()
			}
			return unlock, nil
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("%w: 获取任务 %s 的锁超时", ErrJobConflict, jobID)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(backoff):
		}
		if backoff < 400*time.Millisecond {
			backoff *= 2
		}
	}
}

// releaseLockScript 只有当值等于自己的 token 时才删除，避免误删他人锁。
var releaseLockScript = redis.NewScript(`
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
`)

// Ping 用于 readiness 探针。
func (s *Store) Ping(ctx context.Context) error { return s.rdb.Ping(ctx).Err() }
