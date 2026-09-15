// Package config 负责装载、校验并冻结运行期配置。
//
// 设计约定：
//   - 所有配置项均可由环境变量覆盖，且必须有**可用的默认值**（便于本地零配置启动）。
//   - 默认值面向本地开发；生产环境一律通过环境变量显式注入。
//   - 校验在启动时一次性完成，配置错误必须让进程**快速失败**，而不是运行期才炸。
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config 是进程的完整配置快照。字段分组与 docker-compose.yml 的服务对齐。
type Config struct {
	Env      string // dev / staging / prod
	LogLevel string // debug / info / warn / error

	HTTP     HTTPConfig
	Redis    RedisConfig
	Queue    QueueConfig
	AI       AIConfig
	Media    MediaConfig
	Pipeline PipelineConfig
}

// HTTPConfig 描述 Go API 网关的监听参数。
type HTTPConfig struct {
	Addr            string
	ReadTimeout     time.Duration
	WriteTimeout    time.Duration
	ShutdownTimeout time.Duration
	// CORSAllowedOrigins 为空表示允许所有来源（仅 dev 可接受）。
	CORSAllowedOrigins []string
}

// RedisConfig 同时服务于 Asynq 队列与业务状态仓储。
type RedisConfig struct {
	Addr     string
	Password string
	DB       int
	PoolSize int
}

// QueueConfig 描述 Asynq 任务队列的策略。
type QueueConfig struct {
	Concurrency  int
	Queues       map[string]int // 队列名 -> 权重
	MaxRetry     int
	RetryBackoff time.Duration
	TaskTimeout  time.Duration
}

// AIConfig 描述到 Python 大脑的 gRPC 连接。
type AIConfig struct {
	Addr string
	// UnaryTimeout 用于单次 RPC（如 PlanScript / CritiqueShot）。
	UnaryTimeout time.Duration
	// StreamTimeout 用于 RunPipeline 这类服务端流式长连接。
	StreamTimeout time.Duration
	// MaxRecvMsgSizeMB 需要放宽：审查 RPC 可能携带多张抽帧的元数据。
	MaxRecvMsgSizeMB int
}

// MediaConfig 描述 ffmpeg 媒体处理参数。
type MediaConfig struct {
	FFmpegBin        string
	FFprobeBin       string
	MaxParallel      int
	WorkDir          string
	KeepIntermediate bool
	FPS              int
	Width            int
	Height           int
}

// PipelineConfig 描述业务流水线的熔断与阈值。
type PipelineConfig struct {
	ShotMaxAttempts      int
	CriticScoreThreshold float64
}

// Load 从环境变量装载配置。任何非法值都会返回错误，由调用方决定是否终止进程。
func Load() (*Config, error) {
	cfg := &Config{
		Env:      getEnv("SCID_ENV", "dev"),
		LogLevel: getEnv("SCID_LOG_LEVEL", "info"),
		HTTP: HTTPConfig{
			Addr:            getEnv("SCID_HTTP_ADDR", "0.0.0.0:8080"),
			ReadTimeout:     getDuration("SCID_HTTP_READ_TIMEOUT", 30*time.Second),
			WriteTimeout:    getDuration("SCID_HTTP_WRITE_TIMEOUT", 60*time.Second),
			ShutdownTimeout: getDuration("SCID_HTTP_SHUTDOWN_TIMEOUT", 15*time.Second),
			// WS 需要长连接，写超时对 WS 路由会被单独豁免（见 httpapi）。
			CORSAllowedOrigins: getCSV("SCID_CORS_ALLOWED_ORIGINS", nil),
		},
		Redis: RedisConfig{
			Addr:     getEnv("SCID_REDIS_ADDR", "localhost:6379"),
			Password: getEnv("SCID_REDIS_PASSWORD", ""),
			DB:       getInt("SCID_REDIS_DB", 0),
			PoolSize: getInt("SCID_REDIS_POOL_SIZE", 32),
		},
		Queue: QueueConfig{
			Concurrency: getInt("SCID_WORKER_CONCURRENCY", 4),
			// 优先级：关键路径（generate）权重更高，避免重做任务把新任务饿死。
			Queues: map[string]int{
				"critical": 6,
				"default":  3,
				"low":      1,
			},
			MaxRetry:     getInt("SCID_QUEUE_MAX_RETRY", 5),
			RetryBackoff: getDuration("SCID_QUEUE_RETRY_BACKOFF", 30*time.Second),
			TaskTimeout:  getDuration("SCID_QUEUE_TASK_TIMEOUT", 45*time.Minute),
		},
		AI: AIConfig{
			Addr:             getEnv("SCID_AI_GRPC_ADDR", "localhost:50051"),
			UnaryTimeout:     getDuration("SCID_AI_GRPC_TIMEOUT_SEC", 900*time.Second),
			StreamTimeout:    getDuration("SCID_AI_GRPC_STREAM_TIMEOUT_SEC", 3600*time.Second),
			MaxRecvMsgSizeMB: getInt("SCID_AI_MAX_RECV_MSG_MB", 32),
		},
		Media: MediaConfig{
			FFmpegBin:        getEnv("SCID_FFMPEG_BIN", "ffmpeg"),
			FFprobeBin:       getEnv("SCID_FFPROBE_BIN", "ffprobe"),
			MaxParallel:      getInt("SCID_FFMPEG_MAX_PARALLEL", 4),
			WorkDir:          getEnv("SCID_MEDIA_WORK_DIR", "./.data/work"),
			KeepIntermediate: getBool("SCID_MEDIA_KEEP_INTERMEDIATE", true),
			FPS:              getInt("SCID_RENDER_FPS", 30),
			Width:            getInt("SCID_RENDER_WIDTH", 1920),
			Height:           getInt("SCID_RENDER_HEIGHT", 1080),
		},
		Pipeline: PipelineConfig{
			ShotMaxAttempts:      getInt("SCID_SHOT_MAX_ATTEMPTS", 3),
			CriticScoreThreshold: getFloat("SCID_CRITIC_SCORE_THRESHOLD", 0.75),
		},
	}

	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Validate 集中校验跨字段约束。分开写是为了让单测可以直接构造 Config 校验。
func (c *Config) Validate() error {
	if c.HTTP.Addr == "" {
		return fmt.Errorf("config: SCID_HTTP_ADDR 不能为空")
	}
	if c.Redis.Addr == "" {
		return fmt.Errorf("config: SCID_REDIS_ADDR 不能为空")
	}
	if c.AI.Addr == "" {
		return fmt.Errorf("config: SCID_AI_GRPC_ADDR 不能为空")
	}
	if c.Queue.Concurrency < 1 {
		return fmt.Errorf("config: SCID_WORKER_CONCURRENCY 必须 >= 1，当前 %d", c.Queue.Concurrency)
	}
	if c.Media.MaxParallel < 1 {
		return fmt.Errorf("config: SCID_FFMPEG_MAX_PARALLEL 必须 >= 1，当前 %d", c.Media.MaxParallel)
	}
	if c.Pipeline.ShotMaxAttempts < 1 {
		return fmt.Errorf("config: SCID_SHOT_MAX_ATTEMPTS 必须 >= 1，当前 %d", c.Pipeline.ShotMaxAttempts)
	}
	// 阈值必须在 (0,1]，否则审查环节会永远通过或永远不通过。
	if c.Pipeline.CriticScoreThreshold <= 0 || c.Pipeline.CriticScoreThreshold > 1 {
		return fmt.Errorf("config: SCID_CRITIC_SCORE_THRESHOLD 必须落在 (0,1]，当前 %v",
			c.Pipeline.CriticScoreThreshold)
	}
	if c.Media.WorkDir == "" {
		return fmt.Errorf("config: SCID_MEDIA_WORK_DIR 不能为空")
	}
	switch c.Env {
	case "dev", "staging", "prod", "test":
	default:
		return fmt.Errorf("config: SCID_ENV 非法值 %q，可选 dev/staging/prod/test", c.Env)
	}
	return nil
}

// IsDev 便于在代码中做少量开发态分支（例如放开 CORS、输出 debug 日志）。
func (c *Config) IsDev() bool { return c.Env == "dev" || c.Env == "test" }

// ---------------------------------------------------------------------------
// 环境变量读取辅助函数
// 统一在此处收口，避免 getenv 散落各处导致默认值不一致。
// ---------------------------------------------------------------------------

func getEnv(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && strings.TrimSpace(v) != "" {
		return strings.TrimSpace(v)
	}
	return def
}

func getInt(key string, def int) int {
	v := getEnv(key, "")
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func getBool(key string, def bool) bool {
	v := strings.ToLower(getEnv(key, ""))
	switch v {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return def
	}
}

func getFloat(key string, def float64) float64 {
	v := getEnv(key, "")
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return def
	}
	return f
}

// getDuration 支持 "900"（裸数字按秒解释）与 "15m" 两种写法，
// 因为 docker-compose 中的 *_SEC 变量习惯写裸秒。
func getDuration(key string, def time.Duration) time.Duration {
	v := getEnv(key, "")
	if v == "" {
		return def
	}
	if n, err := strconv.Atoi(v); err == nil {
		return time.Duration(n) * time.Second
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	return d
}

func getCSV(key string, def []string) []string {
	v := getEnv(key, "")
	if v == "" {
		return def
	}
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if s := strings.TrimSpace(p); s != "" {
			out = append(out, s)
		}
	}
	return out
}
