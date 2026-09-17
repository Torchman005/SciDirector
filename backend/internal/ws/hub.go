// Package ws 实现面向审核台的 WebSocket 广播中心。
//
// 职责边界（刻意保持窄）：
//   - 本包只负责「连接管理 + 按 job 扇出」，不感知任务语义；
//   - 事件从哪来（Redis Pub/Sub 还是进程内直接调用）由上层决定；
//   - 这样同一套 Hub 在单实例与多实例部署下都能用。
//
// 并发模型：每个连接一个读协程 + 一个写协程，共享一个带缓冲的发送 channel。
// 关键约束：**禁止在广播路径上阻塞**——某个客户端卡住不能拖垮整个任务的事件流，
// 因此发送 channel 满时直接断开该客户端（前端会重连并用 last_event_id 补齐）。
package ws

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	// writeWait 是单条消息的写超时。
	writeWait = 10 * time.Second
	// pongWait 是等待客户端 pong 的上限；超过即认为连接已死。
	pongWait = 60 * time.Second
	// pingPeriod 必须小于 pongWait，否则服务端会先判定超时。
	pingPeriod = 45 * time.Second
	// maxMessageSize 限制上行消息体，防止恶意/失控客户端耗尽内存。
	maxMessageSize = 64 << 10 // 64 KiB
	// sendBuffer 是每连接的发送缓冲。够大以吸收突发事件，又小到不会掩盖消费慢的问题。
	sendBuffer = 64
)

// Message 是下行推送的统一信封。
type Message struct {
	Type string `json:"type"`           // event / snapshot / error / ack
	Seq  int64  `json:"seq,omitempty"`  // 事件序号，客户端用于断线续传
	Data any    `json:"data,omitempty"` // 具体负载
}

// InboundMessage 是上行消息（人类反馈闭环）。
type InboundMessage struct {
	Type    string `json:"type"` // feedback / ping / subscribe
	ShotID  string `json:"shot_id,omitempty"`
	Comment string `json:"comment,omitempty"`
	AfterID int64  `json:"after_id,omitempty"` // 重连时请求重放的起点
}

// Handler 处理上行消息。返回值为要回给该客户端的 ack（可为 nil）。
//
// 之所以用回调而不是在 Hub 里实现业务：Hub 不该依赖 store / queue，
// 否则会形成 ws -> store -> ... 的隐式耦合，单测也变得困难。
type Handler func(ctx context.Context, jobID string, msg InboundMessage) *Message

// Client 表示一条 WebSocket 连接。
type Client struct {
	hub   *Hub
	jobID string
	conn  *websocket.Conn
	send  chan []byte
	log   *slog.Logger
	// done 在连接被回收时关闭，用于通知写协程退出。
	// 刻意**不关闭 send channel**：并发场景下向已关闭的 channel 发送会 panic，
	// 而发送方（Broadcast / SendTo）分布在多个 goroutine 中，无法安全协调。
	// 用「关闭信号 + 非阻塞发送」代替，是更稳妥的模式。
	done chan struct{}
	// once 保证 done 只被关闭一次。
	once sync.Once
}

// Hub 是按 job 分组的连接注册表。
type Hub struct {
	mu      sync.RWMutex
	clients map[string]map[*Client]struct{}
	log     *slog.Logger
	// upgrader 的 CheckOrigin 在 dev 下放开，生产必须显式配置允许的来源。
	upgrader websocket.Upgrader
}

// NewHub 创建 Hub。allowedOrigins 为空表示允许所有来源（仅限 dev）。
//
// 列表里的 `*` 同样表示「允许所有来源」，必须显式识别：此前的实现只把
// **空列表**当作放开，而把 `*` 当成一个普通的字面来源去比对 —— 于是
// `SCID_CORS_ALLOWED_ORIGINS=*`（最自然的「全放开」写法）会让**每一次**
// WebSocket 升级都拿到 403：浏览器永远停在「重连中…」，
// 而 REST 轮询仍在更新页面，看起来只是「有点卡」，极难怀疑到实时通道上。
func NewHub(allowedOrigins []string, logger *slog.Logger) *Hub {
	allowAll := len(allowedOrigins) == 0
	allowed := make(map[string]struct{}, len(allowedOrigins))
	for _, o := range allowedOrigins {
		o = strings.TrimSpace(o)
		if o == "*" {
			allowAll = true
			continue
		}
		allowed[o] = struct{}{}
	}

	return &Hub{
		clients: make(map[string]map[*Client]struct{}),
		log:     logger,
		upgrader: websocket.Upgrader{
			ReadBufferSize:  4096,
			WriteBufferSize: 4096,
			// 前端需要能携带自定义头（如 Authorization），交由上层鉴权中间件处理。
			CheckOrigin: func(r *http.Request) bool {
				if allowAll {
					return true
				}
				_, ok := allowed[r.Header.Get("Origin")]
				return ok
			},
		},
	}
}

// ClientCount 返回指定 job 的在线连接数（用于测试与指标）。
func (h *Hub) ClientCount(jobID string) int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.clients[jobID])
}

// TotalClients 返回全部在线连接数。
func (h *Hub) TotalClients() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	n := 0
	for _, set := range h.clients {
		n += len(set)
	}
	return n
}

func (h *Hub) register(c *Client) {
	h.mu.Lock()
	defer h.mu.Unlock()
	set, ok := h.clients[c.jobID]
	if !ok {
		set = make(map[*Client]struct{})
		h.clients[c.jobID] = set
	}
	set[c] = struct{}{}
}

func (h *Hub) unregister(c *Client) {
	h.mu.Lock()
	defer h.mu.Unlock()
	set, ok := h.clients[c.jobID]
	if !ok {
		return
	}
	if _, ok := set[c]; ok {
		delete(set, c)
		// 关闭发送 channel 会让写协程退出，从而释放连接。
		c.closeSend()
	}
	if len(set) == 0 {
		// 及时回收空集合，避免长时间运行下 map 无限增长（内存泄漏的常见来源）。
		delete(h.clients, c.jobID)
	}
}

// Broadcast 向订阅了 jobID 的所有连接推送一条消息。
//
// 非阻塞语义：任一客户端发送缓冲满时**只断开该客户端**，
// 保证慢消费者不会拖慢整个任务的事件流。
func (h *Hub) Broadcast(jobID string, msg Message) {
	buf, err := json.Marshal(msg)
	if err != nil {
		h.log.Error("ws: 序列化下行消息失败", slog.String("error", err.Error()))
		return
	}

	h.mu.RLock()
	set := h.clients[jobID]
	targets := make([]*Client, 0, len(set))
	for c := range set {
		targets = append(targets, c)
	}
	h.mu.RUnlock()

	for _, c := range targets {
		select {
		case <-c.done:
			// 连接正在回收，跳过。
		case c.send <- buf:
		default:
			// 缓冲满：该客户端消费不过来。断开它，前端重连后可用 last_event_id 补齐。
			h.log.Warn("ws: 客户端发送缓冲已满，主动断开",
				slog.String("job_id", jobID),
				slog.Int("buffer", sendBuffer),
			)
			h.unregister(c)
			_ = c.conn.Close()
		}
	}
}

// SendTo 向单个客户端发送（仅用于 ack 等点对点消息）。
func (c *Client) SendTo(msg Message) {
	buf, err := json.Marshal(msg)
	if err != nil {
		return
	}
	select {
	case <-c.done:
		// 连接已回收：丢弃即可。
	case c.send <- buf:
	default:
		// 点对点消息丢弃即可，不值得为此断开连接。
	}
}

func (c *Client) closeSend() {
	c.once.Do(func() { close(c.done) })
}

// Serve 把 HTTP 请求升级为 WebSocket 并进入读写循环。阻塞直到连接结束。
//
// onConnect 在连接建立、读循环开始前调用，用于「加入即重放历史事件」；
// 它的返回值会作为首条消息推送给客户端。
func (h *Hub) Serve(
	w http.ResponseWriter,
	r *http.Request,
	jobID string,
	onConnect func(ctx context.Context) *Message,
	handler Handler,
) {
	conn, err := h.upgrader.Upgrade(w, r, nil)
	if err != nil {
		// Upgrade 失败时 gorilla 已经写过响应，这里只记日志。
		h.log.Warn("ws: 协议升级失败", slog.String("error", err.Error()))
		return
	}

	client := &Client{
		hub:   h,
		jobID: jobID,
		conn:  conn,
		send:  make(chan []byte, sendBuffer),
		done:  make(chan struct{}),
		log:   h.log.With(slog.String("job_id", jobID)),
	}
	h.register(client)
	h.log.Info("ws: 客户端已连接", slog.String("job_id", jobID), slog.Int("online", h.ClientCount(jobID)))

	// 写协程：所有对 conn 的写操作都收敛在这里。
	// gorilla/websocket 不允许并发写，这是必须遵守的硬约束。
	go client.writePump()

	// 连接建立后先补发历史，前端刷新页面才不会丢失上下文。
	if onConnect != nil {
		if msg := onConnect(r.Context()); msg != nil {
			client.SendTo(*msg)
		}
	}

	client.readPump(r.Context(), handler)

	// readPump 返回即代表连接结束，回收资源。
	h.unregister(client)
	_ = conn.Close()
	h.log.Info("ws: 客户端已断开", slog.String("job_id", jobID))
}

// readPump 处理上行消息，并在退出时关闭连接。
func (c *Client) readPump(ctx context.Context, handler Handler) {
	c.conn.SetReadLimit(maxMessageSize)
	_ = c.conn.SetReadDeadline(time.Now().Add(pongWait))
	c.conn.SetPongHandler(func(string) error {
		// 每收到一次 pong 就续期，实现「静默超时检测」。
		return c.conn.SetReadDeadline(time.Now().Add(pongWait))
	})

	for {
		_, raw, err := c.conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure) {
				c.log.Warn("ws: 读取消息异常结束", slog.String("error", err.Error()))
			}
			return
		}
		if handler == nil {
			continue
		}

		var msg InboundMessage
		if err := json.Unmarshal(raw, &msg); err != nil {
			c.SendTo(Message{Type: "error", Data: "消息格式非法，期望 JSON"})
			continue
		}
		if ack := handler(ctx, c.jobID, msg); ack != nil {
			c.SendTo(*ack)
		}
	}
}

// writePump 串行化所有写操作，并周期发送 ping 维持连接。
func (c *Client) writePump() {
	ticker := time.NewTicker(pingPeriod)
	defer ticker.Stop()

	for {
		select {
		case <-c.done:
			// 连接被回收：发送关闭帧后退出。
			// 注意 SetWriteDeadline 是必要的，否则在对端不可达时 Close 帧可能永久阻塞。
			_ = c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			_ = c.conn.WriteMessage(websocket.CloseMessage, []byte{})
			return
		case buf := <-c.send:
			_ = c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if err := c.conn.WriteMessage(websocket.TextMessage, buf); err != nil {
				c.log.Debug("ws: 写消息失败，连接关闭", slog.String("error", err.Error()))
				return
			}
		case <-ticker.C:
			_ = c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}
