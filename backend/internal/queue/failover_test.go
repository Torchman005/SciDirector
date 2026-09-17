package queue

// 本文件落地**阶段三验收 B5**：「断开 Redis 后，队列中的任务在恢复后继续执行」。
//
// 为什么必须真的把 Redis 杀掉再拉起来：
// 这个属性由三件事共同决定 —— Asynq 的重投机制、`queue.NewServer` 的参数、
// 以及**部署用的 Redis 持久化配置**。单测里 mock 掉 Redis 就什么都证明不了；
// 而只做人工故障注入的代价是「没人会在每次改动后重做一遍」，于是它迟早悄悄失效。
//
// 这里用**私有 Redis 实例**（独立端口 + 独立目录），而不是公用的 6379：
// 断线测试必须能杀 Redis，杀公用的那个会连带影响同一次 `go test` 里其它包的用例，
// 表现为难以归因的偶发失败。
//
// 两个用例分别对应两种真实故障形态：
//   1. 任务**还在排队**没被取走时 Redis 挂了 —— 靠持久化原样恢复。
//   2. 任务**正在执行**时 Redis 挂了 —— 租约失效后由 recoverer 重新投递。
// 第 2 种受 Asynq 硬编码的租约（30s）与 recoverer 轮询（60s）所限，
// 至少要一分多钟，因此**默认不跑**，见 slowFailoverEnabled 的说明。

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// slowFailoverEnabled 决定是否运行「在执行中被 Redis 断线」那个用例。
//
// 它需要 1~2 分钟（Asynq 的租约 30s、recoverer 轮询 60s 都是硬编码的），
// 而 `make test` / CI 的主目标必须保持秒级 —— 长耗时用例混在默认目标里，
// 最后一定会被整体加 skip 或在超时后被忽略，等于没写。
//
// 显式开：SCID_TEST_REDIS_FAILOVER=1 go test ./internal/queue/ -run TestB5 -v
func slowFailoverEnabled() bool {
	v := strings.TrimSpace(os.Getenv("SCID_TEST_REDIS_FAILOVER"))
	return v == "1" || strings.EqualFold(v, "true")
}

// redisServerBin 定位 redis-server 可执行文件。
//
// 刻意不把它塞进 PATH 依赖里：开发机上 Redis 可能来自镜像、也可能是解压出来的
// 静态构建，用环境变量显式指定比猜路径可靠。
func redisServerBin(t *testing.T) string {
	t.Helper()
	if p := strings.TrimSpace(os.Getenv("SCID_TEST_REDIS_BIN")); p != "" {
		return p
	}
	p, err := exec.LookPath("redis-server")
	if err != nil {
		t.Skipf("找不到 redis-server（可设 SCID_TEST_REDIS_BIN 指定），跳过 B5 故障注入测试: %v", err)
	}
	return p
}

// assertDeployRedisIsDurable 把部署配置纳入断言。
//
// 「Redis 重启后任务还在」的前提是**持久化真的开着**。这个前提藏在
// `deploy/redis/redis.conf` 里，而测试自己启动的实例用的是显式参数 ——
// 两者一旦分叉，测试会继续绿着，线上却会丢任务。
// 因此这里直接校验部署配置本身。
func assertDeployRedisIsDurable(t *testing.T) {
	t.Helper()
	path := filepath.Join("..", "..", "..", "deploy", "redis", "redis.conf")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("读不到部署 Redis 配置（%s），跳过 B5: %v", path, err)
	}

	cfg := map[string]string{}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if k, v, ok := strings.Cut(line, " "); ok {
			cfg[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}

	if got := cfg["appendonly"]; got != "yes" {
		t.Errorf("deploy/redis/redis.conf 的 appendonly = %q，期望 \"yes\"："+
			"关掉 AOF 之后「断开 Redis 恢复后任务继续执行」不再成立，队列里的任务会直接消失", got)
	}
	if got := cfg["appendfsync"]; got != "everysec" {
		t.Errorf("deploy/redis/redis.conf 的 appendfsync = %q，期望 \"everysec\"", got)
	}
}

// privateRedis 是一个由测试自己拉起、可以随时杀掉的 Redis 实例。
type privateRedis struct {
	bin  string
	dir  string
	port int
	addr string
	cmd  *exec.Cmd
}

func newPrivateRedis(t *testing.T) *privateRedis {
	t.Helper()
	bin := redisServerBin(t)

	// 先占一个端口再释放：redis-server 无法把「随机端口」回传给调用方，
	// 而端口冲突时它会直接退出。窗口极小，且失败信息会明确指出是端口问题。
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("申请端口失败: %v", err)
	}
	port := lis.Addr().(*net.TCPAddr).Port
	_ = lis.Close()

	dir := t.TempDir()
	r := &privateRedis{
		bin:  bin,
		dir:  dir,
		port: port,
		addr: net.JoinHostPort("127.0.0.1", strconv.Itoa(port)),
	}
	t.Cleanup(func() { r.stop() })
	r.start(t)
	return r
}

// start 拉起（或重新拉起）Redis。
//
// 参数刻意与 `deploy/redis/redis.conf` 的持久化设置保持一致：
// 用 `--save ""` 关掉 RDB、只留 AOF，这样能证明的正是 AOF 在起作用，
// 而不是被 RDB 快照顺带救了。
func (r *privateRedis) start(t *testing.T) {
	t.Helper()
	cmd := exec.Command(r.bin,
		"--port", strconv.Itoa(r.port),
		"--bind", "127.0.0.1",
		"--dir", r.dir,
		"--appendonly", "yes",
		"--appendfsync", "everysec",
		"--save", "",
		"--daemonize", "no",
		"--protected-mode", "no",
		"--logfile", filepath.Join(r.dir, fmt.Sprintf("redis-%d.log", r.port)),
	)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		t.Fatalf("启动私有 Redis 失败: %v", err)
	}
	r.cmd = cmd
	r.waitReady(t)
}

// kill 模拟**进程崩溃**（SIGKILL），而不是优雅关闭。
//
// 这一点很关键：优雅关闭会让 Redis 主动落盘，从而掩盖「持久化策略是否真的够用」。
// 我们要验证的正是「机房掉电、进程被 OOM Killer 杀掉」之后的恢复能力。
func (r *privateRedis) kill(t *testing.T) {
	t.Helper()
	if r.cmd == nil || r.cmd.Process == nil {
		return
	}
	if err := r.cmd.Process.Kill(); err != nil {
		t.Fatalf("杀掉 Redis 失败: %v", err)
	}
	_, _ = r.cmd.Process.Wait()
	r.cmd = nil

	// 确认端口真的没了，否则后续断言可能连到残留进程上，结论完全失真。
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if conn, err := net.DialTimeout("tcp", r.addr, 200*time.Millisecond); err != nil {
			return
		} else {
			_ = conn.Close()
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("Redis 已被杀但 %s 仍然可连，故障注入没有真正生效", r.addr)
}

func (r *privateRedis) stop() {
	if r.cmd != nil && r.cmd.Process != nil {
		_ = r.cmd.Process.Kill()
		_, _ = r.cmd.Process.Wait()
		r.cmd = nil
	}
}

// waitReady 用最原始的 PING 探测就绪，不依赖 redis-cli 是否安装。
func (r *privateRedis) waitReady(t *testing.T) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		if err := r.ping(); err == nil {
			return
		} else {
			lastErr = err
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("Redis 未能在 15s 内就绪（%s）: %v", r.addr, lastErr)
}

func (r *privateRedis) ping() error {
	conn, err := net.DialTimeout("tcp", r.addr, 500*time.Millisecond)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := conn.Write([]byte("PING\r\n")); err != nil {
		return err
	}
	line, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil {
		return err
	}
	if !strings.HasPrefix(line, "+PONG") {
		return fmt.Errorf("PING 返回 %q", strings.TrimSpace(line))
	}
	return nil
}

// redisCfg 返回指向本实例的连接配置。
func (r *privateRedis) redisCfg() config.RedisConfig {
	return config.RedisConfig{Addr: r.addr, DB: 0}
}

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------

// countingHandler 记录某类任务被真正执行了几次。
type countingHandler struct {
	ran atomic.Int32
	// started 在每次开始执行时发一个信号，供测试等待「任务已经被取走」。
	started chan struct{}
	// block 非 nil 时，处理函数会一直等它关闭 —— 用来把任务**卡在执行中**。
	block chan struct{}
	// failTimes 指定前 N 次执行返回错误（用于构造重试）。
	failTimes int32
}

func (h *countingHandler) handle(ctx context.Context, _ *asynq.Task) error {
	n := h.ran.Add(1)
	if h.started != nil {
		select {
		case h.started <- struct{}{}:
		default:
		}
	}
	if h.block != nil {
		select {
		case <-h.block:
		case <-ctx.Done():
			// Redis 断线导致租约失效时，Asynq 会取消 ctx。
			// 如实返回错误：这条任务要不要重投由 Asynq 决定，不是 handler 说了算。
			return ctx.Err()
		}
	}
	if n <= h.failTimes {
		return fmt.Errorf("模拟第 %d 次执行失败", n)
	}
	return nil
}

// newFailoverServer 用**项目自己的** NewServer 构造 Asynq 服务端。
//
// 必须走 NewServer 而不是在测试里自己拼一份 asynq.Config：
// B5 要验证的正是「这套配置在断线后还能把任务捡回来」，
// 换一份配置就等于换掉了被测对象。
func newFailoverServer(r *privateRedis, h *countingHandler) (*asynq.Server, *asynq.ServeMux, string) {
	const taskType = "task:b5:probe"

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	qcfg := config.QueueConfig{
		Concurrency: 1,
		Queues:      map[string]int{QueueCritical: 1},
		MaxRetry:    3,
		// 退避压到 1 秒：recoverer 重投时用的就是这个函数，
		// 默认 30 秒起会让本就一分多钟的用例再翻一倍。
		RetryBackoff: time.Second,
		// 任务级超时同时决定**租约长度**，因此它直接决定「多久之后才可能被重投」。
		TaskTimeout: 20 * time.Second,
	}

	srv := NewServer(r.redisCfg(), qcfg, logger)
	mux := NewMux()
	mux.HandleFunc(taskType, h.handle)
	return srv, mux, taskType
}

// TestB5QueuedTasksSurviveRedisRestart 覆盖最基础也最致命的一种：
// 任务**已经入队但还没被取走**，此时 Redis 进程崩溃。
//
// 持久化没配好的话，这些任务会消失得无影无踪 —— 用户提交了生成请求、拿到 202、
// 然后永远等不到任何事件，而队列里空空如也，排查时会怀疑是「任务没投进去」。
func TestB5QueuedTasksSurviveRedisRestart(t *testing.T) {
	assertDeployRedisIsDurable(t)

	r := newPrivateRedis(t)
	ctx := context.Background()

	// 先入队，**此时还没有任何 worker 在消费** —— 任务只能躺在队列里。
	client := asynq.NewClient(asynq.RedisClientOpt{Addr: r.addr})
	t.Cleanup(func() { _ = client.Close() })

	const n = 3
	for i := 0; i < n; i++ {
		task := asynq.NewTask("task:b5:probe", []byte(fmt.Sprintf("payload-%d", i)))
		if _, err := client.EnqueueContext(ctx, task,
			asynq.Queue(QueueCritical),
			asynq.MaxRetry(3),
			asynq.Timeout(20*time.Second),
		); err != nil {
			t.Fatalf("投递第 %d 条任务失败: %v", i, err)
		}
	}

	// appendfsync everysec 意味着**最多 1 秒**的写入可能随崩溃丢失。
	// 不等过这个窗口就杀进程，测试会偶发失败，而且失败现象看起来像
	// 「持久化没用」—— 会把人引到完全错误的方向上。
	time.Sleep(1500 * time.Millisecond)

	r.kill(t)
	r.start(t)

	// 重启之后、消费之前先确认任务真的还在：把「持久化是否生效」与
	// 「Asynq 是否会重投」这两件事分开断言，失败时才能一眼看出是哪一环断了。
	insp := asynq.NewInspector(asynq.RedisClientOpt{Addr: r.addr})
	t.Cleanup(func() { _ = insp.Close() })
	info, err := insp.GetQueueInfo(QueueCritical)
	if err != nil {
		t.Fatalf("重启后读取队列信息失败: %v", err)
	}
	if info.Pending < n {
		t.Fatalf("Redis 崩溃重启后队列只剩 %d 条待处理任务（期望 %d）—— "+
			"持久化没起作用，用户提交的任务被静默丢弃", info.Pending, n)
	}

	h := &countingHandler{}
	srv, mux, _ := newFailoverServer(r, h)
	if err := srv.Start(mux); err != nil {
		t.Fatalf("启动 Asynq 服务端失败: %v", err)
	}
	defer srv.Shutdown()

	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if h.ran.Load() >= n {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if got := h.ran.Load(); got != n {
		t.Fatalf("恢复后只执行了 %d 条任务（期望 %d）—— 队列中的任务没有继续执行", got, n)
	}
}

// TestB5InFlightTaskRecoveredAfterRedisRestart 覆盖更难的一种：
// 任务**正在执行**时 Redis 崩溃。
//
// 这时任务已经离开 pending 队列、处于 active 状态，只能靠 Asynq 的租约机制回收。
// 具体时间线（三个数字都来自 Asynq 源码，不是拍脑袋）：
//   - 心跳每 `HealthCheckInterval`（本项目配 15s）一次，每次把租约续到「此刻 + 30s」
//     （`internal/rdb.ExtendLease`，LeaseDuration 硬编码 30s）；
//   - 续租失败累计到一个租约长度后，任务被判为租约过期，handler 的 ctx 被取消；
//   - recoverer 每 60s 轮询一次，且只回收**过期已满 30s** 的任务
//     （故意留的余量，用于容忍时钟偏移）。
//
// **关键陷阱（本用例第一版就踩了）**：断线必须持续够久。
// 如果 Redis 很快回来，下一次心跳就把租约续上了，正在执行的任务根本不会被打断、
// 会正常跑完 —— 测试看着「恢复了」，但 recoverer 从头到尾没参与，
// 等于什么都没验证，而且还会让人误以为这条路是通的。
// 因此这里让断线持续一个租约长度 + 一个心跳周期，把任务真正留在 active 集合里。
func TestB5InFlightTaskRecoveredAfterRedisRestart(t *testing.T) {
	if !slowFailoverEnabled() {
		t.Skip("慢用例：需要 2~4 分钟（Asynq 的租约 30s、recoverer 轮询 60s、过期余量 30s 均为硬编码）。" +
			"用 SCID_TEST_REDIS_FAILOVER=1 go test ./internal/queue/ -run TestB5 -v 显式开启")
	}
	assertDeployRedisIsDurable(t)

	r := newPrivateRedis(t)
	ctx := context.Background()

	// block 让任务卡在执行中，从而制造出「active 状态下断线」这个现场。
	h := &countingHandler{started: make(chan struct{}, 4), block: make(chan struct{})}
	srv, mux, taskType := newFailoverServer(r, h)
	if err := srv.Start(mux); err != nil {
		t.Fatalf("启动 Asynq 服务端失败: %v", err)
	}
	defer srv.Shutdown()

	client := asynq.NewClient(asynq.RedisClientOpt{Addr: r.addr})
	t.Cleanup(func() { _ = client.Close() })

	if _, err := client.EnqueueContext(
		ctx,
		asynq.NewTask(taskType, []byte("inflight")),
		asynq.Queue(QueueCritical),
		asynq.MaxRetry(3),
		asynq.Timeout(20*time.Second),
	); err != nil {
		t.Fatalf("投递任务失败: %v", err)
	}

	select {
	case <-h.started:
	case <-time.After(15 * time.Second):
		t.Fatal("任务没有开始执行，无法制造「执行中断线」的场景")
	}

	// 让任务真的开始跑一会再断线 —— 否则可能它还没被取走，就退化成上面那个用例了。
	time.Sleep(500 * time.Millisecond)
	r.kill(t)

	// 断线期间 worker 必须**活着**（不能因为 Redis 没了就退出），
	// 否则恢复之后没有任何人去消费队列。
	const outage = 50 * time.Second
	time.Sleep(outage)
	r.start(t)

	// 保险：万一 ctx 取消这条路没走到，也放开 handler，避免用例靠超时兜底。
	close(h.block)

	// 等 recoverer 把这条任务捡回来重跑。给足余量，免得慢机器上误判。
	deadline := time.Now().Add(3 * time.Minute)
	for time.Now().Before(deadline) {
		if h.ran.Load() >= 2 {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}

	if got := h.ran.Load(); got < 2 {
		t.Fatalf("执行中遭遇 Redis 断线后任务只跑了 %d 次，没有被 recoverer 重投 —— "+
			"这类任务会永久停留在 active 状态：任务永远不推进、前端一直等、"+
			"而队列里既没有 pending 也没有失败，没有任何错误可报", got)
	}
}
