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
	Archive  ArchiveConfig
	Obs      ObservabilityConfig
}

// ObservabilityConfig 描述链路追踪与指标（阶段五·可观测性）。
type ObservabilityConfig struct {
	// ServiceName 写进每个 span 的 service.name，用于在 Grafana/Tempo 里区分 api 与 worker。
	ServiceName string
	// OTLPEndpoint 是 OTLP/gRPC 端点（如 127.0.0.1:4317）。
	// **为空表示不导出追踪** —— 本地不跑 collector 是常态，
	// 此时应当是无害的 no-op，而不是启动失败。
	OTLPEndpoint string
	// Insecure 用明文 gRPC 连 collector。本地/内网应为 true。
	Insecure bool
	// SampleRatio 采样比例 (0,1]。1 表示全采（本地联调默认如此）。
	SampleRatio float64
	// MetricsPath 是 Prometheus 抓取路径。为空表示不暴露 /metrics。
	MetricsPath string
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
	FFmpegBin  string
	FFprobeBin string
	// MaxParallel 是整个 worker 进程内**同时**运行的 ffmpeg/ffprobe 进程数上限。
	// 它是防 OOM 的全局闸门，而不是「每个任务」的上限。
	MaxParallel int
	// CommandTimeout 是单条 ffmpeg/ffprobe 命令的硬超时。
	// 没有它，一个卡死的进程会永久占住一个并发槽位；占满 MaxParallel 个之后
	// 整条流水线静默停摆（没有任何错误可报），比直接崩溃更难排查。
	CommandTimeout   time.Duration
	WorkDir          string
	KeepIntermediate bool
	FPS              int
	Width            int
	Height           int
	// Transition 是跨镜头转场类型（none 表示硬切，走最快的 concat -c copy 路径）。
	// 任何非 none 的取值都需要重编码整条成片，是明确的性能代价。
	Transition string
	// TransitionDurationSec 是转场时长；会被最短片段自动压住。
	TransitionDurationSec float64
	// 统一调色参数。零值表示不调整；见 media.ColorProfile 的说明。
	ColorSaturation float64
	ColorContrast   float64
	ColorGamma      float64
	ColorBrightness float64
	// SubtitleEnabled 控制是否生成并封装软字幕。
	// 软字幕（mov_text）可开关、不破坏画面，因此默认开启。
	SubtitleEnabled bool
	// SubtitleMaxCharsPerCue 是单条字幕的长度上限（按语音权重单位计）。
	SubtitleMaxCharsPerCue int
	// SubtitleMinCueSec / SubtitleMaxCueSec 是单条字幕的显示时长上下限。
	SubtitleMinCueSec float64
	SubtitleMaxCueSec float64
}

// PipelineConfig 描述业务流水线的熔断与阈值。
type PipelineConfig struct {
	ShotMaxAttempts      int
	CriticScoreThreshold float64
}

// ArchiveConfig 描述产物归档与本地保留策略。
type ArchiveConfig struct {
	// Backend 取值 none / local / s3。
	// 默认 none：单机开发与 CI 没有对象存储，缺省必须是一条能跑通的路径。
	Backend string
	// LocalDir 是 local 后端的根目录。
	LocalDir string
	// KeepAll 为 true 时不做任何本地清理（排查线上问题时保留现场）。
	KeepAll bool
	// KeepNormalized 保留归一化中间产物（逐镜头重做时可省一次转码）。
	KeepNormalized bool

	MinioEndpoint  string
	MinioAccessKey string
	MinioSecretKey string
	MinioBucket    string
	MinioUseSSL    bool
	MinioRegion    string
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
			CommandTimeout:   getDuration("SCID_FFMPEG_CMD_TIMEOUT", 10*time.Minute),
			WorkDir:          getEnv("SCID_MEDIA_WORK_DIR", "./.data/work"),
			KeepIntermediate: getBool("SCID_MEDIA_KEEP_INTERMEDIATE", true),
			FPS:              getInt("SCID_RENDER_FPS", 30),
			Width:            getInt("SCID_RENDER_WIDTH", 1920),
			Height:           getInt("SCID_RENDER_HEIGHT", 1080),
			// 默认 fade：科普视频里镜头之间直切会显得生硬。
			// 代价是整条成片需要重编码 —— 追求速度可设为 none 走 copy 路径。
			Transition:            getEnv("SCID_TRANSITION", "fade"),
			TransitionDurationSec: getFloat("SCID_TRANSITION_DURATION_SEC", 0.4),
			// 默认全为 0（不调整）。只统一规格与色彩范围，不做创作性调色 ——
			// 把「技术一致性」和「艺术风格」分开，后者应当由导演智能体决定。
			ColorSaturation: getFloat("SCID_COLOR_SATURATION", 0),
			ColorContrast:   getFloat("SCID_COLOR_CONTRAST", 0),
			ColorGamma:      getFloat("SCID_COLOR_GAMMA", 0),
			ColorBrightness: getFloat("SCID_COLOR_BRIGHTNESS", 0),
			// 字幕默认开启：科普视频没有字幕几乎不可用（静音观看场景占很大比例）。
			SubtitleEnabled:        getBool("SCID_SUBTITLE_ENABLED", true),
			SubtitleMaxCharsPerCue: getInt("SCID_SUBTITLE_MAX_CHARS", 18),
			SubtitleMinCueSec:      getFloat("SCID_SUBTITLE_MIN_CUE_SEC", 0.8),
			SubtitleMaxCueSec:      getFloat("SCID_SUBTITLE_MAX_CUE_SEC", 8.0),
		},
		Pipeline: PipelineConfig{
			ShotMaxAttempts:      getInt("SCID_SHOT_MAX_ATTEMPTS", 3),
			CriticScoreThreshold: getFloat("SCID_CRITIC_SCORE_THRESHOLD", 0.75),
		},
		Obs: loadObservabilityConfig(),
		Archive: ArchiveConfig{
			Backend:        getEnv("SCID_ARCHIVE_BACKEND", "none"),
			LocalDir:       getEnv("SCID_ARCHIVE_LOCAL_DIR", "./.data/archive"),
			KeepAll:        getBool("SCID_ARCHIVE_KEEP_ALL", false),
			KeepNormalized: getBool("SCID_ARCHIVE_KEEP_NORMALIZED", false),

			// 对象存储的连接参数。
			//
			// 环境变量名统一为 `SCID_S3_*`，因为后端已不再限定为 MinIO：
			// 原先默认的 MinIO 开源版**已归档停更**（不再提供安全更新），
			// compose 改用同为 S3 兼容、且在活跃维护的 RustFS。
			// 名字里带 MINIO 而实际连的是别家，是排查时最费时间的那种误导。
			//
			// `SCID_MINIO_*` 保留为**兼容回退**：老部署的 .env 里写的是它，
			// 直接改名会让那些机器上的归档配置一夜之间全部失效
			// （而且是静默失效 —— 端点是空串时归档直接报错或被跳过）。
			MinioEndpoint:  getEnvFirst([]string{"SCID_S3_ENDPOINT", "SCID_MINIO_ENDPOINT"}, ""),
			MinioAccessKey: getEnvFirst([]string{"SCID_S3_ACCESS_KEY", "SCID_MINIO_ACCESS_KEY"}, ""),
			MinioSecretKey: getEnvFirst([]string{"SCID_S3_SECRET_KEY", "SCID_MINIO_SECRET_KEY"}, ""),
			MinioBucket:    getEnvFirst([]string{"SCID_S3_BUCKET", "SCID_MINIO_BUCKET"}, "scidirector"),
			MinioUseSSL:    getBoolFirst([]string{"SCID_S3_USE_SSL", "SCID_MINIO_USE_SSL"}, false),
			MinioRegion:    getEnvFirst([]string{"SCID_S3_REGION", "SCID_MINIO_REGION"}, ""),
		},
	}

	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Validate 集中校验跨字段约束。分开写是为了让单测可以直接构造 Config 校验。
// loadObservabilityConfig 读取可观测性配置。
//
// 缺省是**全关**：默认不导出追踪、不暴露 /metrics。
// 理由与「默认归档后端是 none」一致 —— 没配就什么都不做，
// 而不是去连一个不存在的 collector 然后让启动失败或刷一屏错误日志。
func loadObservabilityConfig() ObservabilityConfig {
	return ObservabilityConfig{
		ServiceName:  getEnv("SCID_OTEL_SERVICE_NAME", "scidirector-api"),
		OTLPEndpoint: getEnv("SCID_OTEL_ENDPOINT", ""),
		// 本地/内网几乎都用明文；要 TLS 时显式设成 false。
		Insecure:    getBool("SCID_OTEL_INSECURE", true),
		SampleRatio: getFloat("SCID_OTEL_SAMPLE_RATIO", 1.0),
		MetricsPath: getEnv("SCID_METRICS_PATH", ""),
	}
}

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

// getEnvFirst 按顺序取第一个「有值」的环境变量。
//
// 用于同一个配置项存在新旧两个变量名时的**平滑迁移**：新名优先，
// 旧名保留为回退。之所以不能直接改名，是因为失效方式是静默的 ——
// 老机器上的 .env 里写的是旧名，改名后那些值一律读不到，
// 而空端点在归档路径上的表现（跳过 / 报错）与「没配置」无法区分。
func getEnvFirst(keys []string, def string) string {
	for _, k := range keys {
		if v, ok := os.LookupEnv(k); ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return def
}

// getBoolFirst 与 getEnvFirst 同理，用于布尔项。
func getBoolFirst(keys []string, def bool) bool {
	for _, k := range keys {
		if v, ok := os.LookupEnv(k); ok && strings.TrimSpace(v) != "" {
			switch strings.ToLower(strings.TrimSpace(v)) {
			case "1", "true", "yes", "on":
				return true
			case "0", "false", "no", "off":
				return false
			}
		}
	}
	return def
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
