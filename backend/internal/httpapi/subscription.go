package httpapi

import (
	"context"
	"sync"

	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/store"
	"github.com/itJinYu/SciDirector/backend/internal/ws"
)

// jobSubscriptions 管理「按任务」的 Redis 订阅生命周期。
//
// 为什么需要它（这是多实例部署正确性的关键）：
//   - WebSocket 客户端连在 **api 进程**上；
//   - 事件由 **worker 进程**产生；
//   - 两个进程可能在不同的容器/机器上。worker 往自己进程内的 Hub 广播，
//     api 这边的客户端根本收不到。
//
// 解决办法：所有事件先写入 Redis 事件流（AppendEvent 内含 Publish），
// api 侧对每个「有客户端在线」的任务订阅该频道，再扇出给本进程的 WS 连接。
//
// 引用计数：多个客户端看同一个任务时只维持一条 Redis 订阅；
// 最后一个客户端离开时才退订，避免订阅泄漏（长期运行下会耗尽 Redis 连接）。
type jobSubscriptions struct {
	mu   sync.Mutex
	subs map[string]*jobSubscription
	st   *store.Store
	hub  *ws.Hub
}

type jobSubscription struct {
	cancel context.CancelFunc
	refs   int
}

func newJobSubscriptions(st *store.Store, hub *ws.Hub) *jobSubscriptions {
	return &jobSubscriptions{
		subs: make(map[string]*jobSubscription),
		st:   st,
		hub:  hub,
	}
}

// acquire 增加引用并在必要时建立订阅，返回释放函数。
func (s *jobSubscriptions) acquire(jobID string) func() {
	s.mu.Lock()
	defer s.mu.Unlock()

	sub, ok := s.subs[jobID]
	if !ok {
		// 用 Background 而非请求 context：订阅的生命周期由引用计数决定，
		// 不能被首个连接断开（请求结束）连带取消。
		ctx, cancel := context.WithCancel(context.Background())
		sub = &jobSubscription{cancel: cancel}
		s.subs[jobID] = sub
		go s.forward(ctx, jobID)
	}
	sub.refs++

	var once sync.Once
	return func() {
		once.Do(func() {
			s.mu.Lock()
			defer s.mu.Unlock()
			cur, ok := s.subs[jobID]
			if !ok {
				return
			}
			cur.refs--
			if cur.refs <= 0 {
				cur.cancel()
				delete(s.subs, jobID)
			}
		})
	}
}

// forward 把 Redis 频道上的事件转发给本进程的 WS 连接。
func (s *jobSubscriptions) forward(ctx context.Context, jobID string) {
	events, cancel := s.st.Subscribe(ctx, jobID)
	defer cancel()

	for ev := range events {
		s.hub.Broadcast(jobID, ws.Message{Type: "event", Seq: ev.EventID, Data: ev})
	}
	logging.FromContext(ctx).Debug("ws: 任务事件订阅已结束", "job_id", jobID)
}
