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

// UpdateJob 以「加锁 + 读改写」的方式原子更新任务。
//
// mutate 函数在持有分布式锁的情况下被调用；返回错误则不写回（更新被放弃）。
// 这是本系统唯一允许修改已存在任务的入口，杜绝丢失更新。
func (s *Store) UpdateJob(ctx context.Context, jobID string, mutate func(*domain.Job) error) (*domain.Job, error) {
	unlock, err := s.acquireLock(ctx, jobID)
	if err != nil {
		return nil, err
	}
	defer unlock()

	job, err := s.GetJob(ctx, jobID)
	if err != nil {
		return nil, err
	}
	if err := mutate(job); err != nil {
		return nil, err
	}

	// 进度是派生值：统一在此重算，避免各调用点忘记更新导致前端进度回跳。
	job.Progress = job.ProgressRatio()
	job.UpdatedAt = time.Now().UTC()
	buf, err := json.Marshal(job)
	if err != nil {
		return nil, fmt.Errorf("store: 序列化任务失败: %w", err)
	}
	if err := s.rdb.Set(ctx, jobKey(job.JobID), buf, 7*24*time.Hour).Err(); err != nil {
		return nil, fmt.Errorf("store: 写回任务失败: %w", err)
	}
	return job, nil
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
