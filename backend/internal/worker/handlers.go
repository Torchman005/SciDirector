package worker

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/media"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// ---------------------------------------------------------------------------
// 任务封装
// ---------------------------------------------------------------------------
//
// 把 asynq.Task 的解析提前到 handler 边界，好处有二：
//  1) 载荷格式错误（坏 JSON / 缺字段）在这里就以「不可重试」的方式终结，
//     而不是让 Asynq 傻乎乎地重试 5 次同样的坏数据；
//  2) handler 内部只面对强类型的领域载荷。

// GenerateTask 是已解析的主链路任务。
type GenerateTask struct {
	Task    *asynq.Task
	Payload *queue.GenerateJobPayload
}

// RenderShotTask 是已解析的单镜头重做任务。
type RenderShotTask struct {
	Task    *asynq.Task
	Payload *queue.RenderShotPayload
}

// ComposeTask 是已解析的合成任务。
type ComposeTask struct {
	Task    *asynq.Task
	Payload *queue.ComposeJobPayload
}

// RegisterHandlers 把三类任务的处理器注册到 ServeMux 上。
func RegisterHandlers(mux *asynq.ServeMux, p *Processor) {
	mux.HandleFunc(queue.TaskGenerateJob, func(ctx context.Context, t *asynq.Task) error {
		payload, err := queue.DecodeGenerateJob(t)
		if err != nil {
			// 包上 asynq.SkipRetry：格式错误的任务必须立刻归档，重试毫无意义。
			return fmt.Errorf("%w: %w", asynq.SkipRetry, err)
		}
		return p.HandleGenerateJob(ctx, GenerateTask{Task: t, Payload: payload})
	})

	mux.HandleFunc(queue.TaskRenderShot, func(ctx context.Context, t *asynq.Task) error {
		payload, err := queue.DecodeRenderShot(t)
		if err != nil {
			return fmt.Errorf("%w: %w", asynq.SkipRetry, err)
		}
		return p.HandleRenderShot(ctx, RenderShotTask{Task: t, Payload: payload})
	})

	mux.HandleFunc(queue.TaskComposeJob, func(ctx context.Context, t *asynq.Task) error {
		payload, err := queue.DecodeComposeJob(t)
		if err != nil {
			return fmt.Errorf("%w: %w", asynq.SkipRetry, err)
		}
		return p.HandleComposeJob(ctx, ComposeTask{Task: t, Payload: payload})
	})
}

// ---------------------------------------------------------------------------
// 媒体合成
// ---------------------------------------------------------------------------

// shotItem 是合成阶段的一个输入片段（包级定义，便于排序与并发索引）。
type shotItem struct {
	index int    // 分镜序号，决定在成片中的位置
	path  string // 视频文件路径
}

// HandleComposeJob 把已通过审查的镜头合成为最终成片。
//
// 流水线：收集产物 -> 逐一 probe 校验 -> 归一化到统一规格 -> concat -> 混流。
//
// 为什么必须逐个 probe + 归一化，而不是直接 concat？
// 因为 Manim、headless 浏览器、代码动画三类渲染器的输出在分辨率、帧率、
// 像素格式、时基上几乎必然不同。直接 concat 轻则花屏，重则时长错乱、
// 音画不同步 —— 而这类问题在成片出来之前完全不可见，返工代价极高。
func (p *Processor) HandleComposeJob(ctx context.Context, task ComposeTask) error {
	jobID := task.Payload.JobID
	ctx = logging.WithJob(ctx, jobID)
	lg := logging.FromContext(ctx).With("job_id", jobID)

	job, err := p.store.GetJob(ctx, jobID)
	if err != nil {
		if errors.Is(err, store.ErrJobNotFound) {
			lg.Warn("任务不存在，忽略合成消息")
			return nil
		}
		return err
	}

	// 收集所有已通过镜头的视频产物。未通过的镜头一律不参与合成 ——
	// 「部分成片」会误导用户，正确做法是保持 PARTIAL 状态并提示缺少哪些镜头。
	items := make([]shotItem, 0, len(job.Shots))
	for _, s := range job.Shots {
		if s.Status != domain.StatusApproved {
			continue
		}
		if s.Artifact == nil || s.Artifact.VideoPath == "" {
			lg.Warn("已通过的镜头缺少视频产物，跳过", "shot_id", s.ShotID)
			continue
		}
		items = append(items, shotItem{index: s.Index, path: s.Artifact.VideoPath})
	}
	if len(items) == 0 {
		return fmt.Errorf("worker: 任务 %s 没有可合成的镜头产物", jobID)
	}
	// 按分镜序号排序：顺序即叙事顺序，绝不能依赖存储顺序。
	sort.Slice(items, func(i, j int) bool { return items[i].index < items[j].index })

	if err := p.transitionJob(ctx, jobID, domain.JobComposing, "compose", 0,
		fmt.Sprintf("开始合成 %d 个分镜片段…", len(items)), nil); err != nil {
		lg.Warn("更新合成状态失败", "error", err.Error())
	}

	workDir := p.jobWorkDir(jobID)
	normDir := filepath.Join(workDir, "normalized")
	if err := os.MkdirAll(normDir, 0o755); err != nil {
		return fmt.Errorf("worker: 创建合成工作目录失败: %w", err)
	}

	// 阶段一：归一化。
	//
	// 两道防线分工必须说清楚，否则很容易「以为限制了、其实没有」：
	//
	//	Pool           限制**本任务内**同时在飞的 goroutine 数 → 内存占用是 O(limit) 而非 O(分镜数)
	//	Runner 全局闸门 限制**整个 worker 进程**的 ffmpeg 子进程数 → 真正的 OOM 防线
	//
	// 早期实现是「无脑 fan-out + 每任务新建信号量」：能限制并发度，但分镜表一大
	// 就会瞬间产生同样多的 goroutine 阻塞在信号量上，且每任务一个信号量会让
	// 全局上限随并发任务数成倍放大。两处都已收敛。
	pool := media.NewPool(p.cfg.Media.MaxParallel)
	normPaths := make([]string, len(items))

	// 任一分支失败即取消其余分支：正在跑的 ffmpeg 会收到取消并退出，不白烧 CPU。
	// pool.Run 保证返回时**所有**分支都已收敛，因此下面可以安全地读 normPaths。
	err = pool.Run(ctx, len(items), func(gctx context.Context, i int) error {
		probe, perr := p.media.Probe(gctx, items[i].path)
		if perr != nil {
			return fmt.Errorf("分镜 %d 产物无效: %w", items[i].index, perr)
		}

		// 已经符合目标规格就跳过转码：转码既有损又耗时，能省则省。
		if probe.Width == p.cfg.Media.Width &&
			probe.Height == p.cfg.Media.Height &&
			int(probe.FPS+0.5) == p.cfg.Media.FPS &&
			probe.PixFmt == "yuv420p" {
			normPaths[i] = items[i].path
			return nil
		}

		out := filepath.Join(normDir, fmt.Sprintf("norm_%03d.mp4", items[i].index))
		if nerr := p.media.Normalize(gctx, items[i].path, out,
			p.cfg.Media.Width, p.cfg.Media.Height, p.cfg.Media.FPS); nerr != nil {
			return fmt.Errorf("分镜 %d 归一化失败: %w", items[i].index, nerr)
		}
		normPaths[i] = out
		return nil
	})
	if err != nil {
		// 只要有一个片段不可用，就不能产出「缺一段」的成片。
		// 宁可失败并转人工提示，也不要交付一部中间少了几个镜头的视频。
		msg := "合成前置校验失败：" + err.Error()
		_, _ = p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
			j.Status = domain.JobPartial
			j.Error = msg
			return nil
		})
		_, _ = p.emit(ctx, &domain.Event{
			JobID: jobID, Node: "compose", Message: msg, Error: msg,
			Timestamp: time.Now().UTC(),
		})
		return fmt.Errorf("worker: %s", msg)
	}

	// 阶段二：concat。归一化后参数一致，可用 -c copy 无损拼接（速度极快）。
	listPath := filepath.Join(workDir, "concat.txt")
	if err := media.WriteConcatList(listPath, normPaths); err != nil {
		return err
	}
	mergedPath := filepath.Join(workDir, "merged.mp4")
	if err := p.media.Concat(ctx, listPath, mergedPath); err != nil {
		return fmt.Errorf("worker: 合并分镜失败: %w", err)
	}

	// 阶段三：产出最终文件。
	// 当前版本各片段自带音轨；全片统一 TTS 配音与字幕烧制属于阶段三的增强项，
	// MuxFinal 已经支持 audioPath / subtitlePath 参数，届时无需改动调用契约。
	finalPath := filepath.Join(workDir, "final.mp4")
	if err := p.media.MuxFinal(ctx, mergedPath, "", "", finalPath); err != nil {
		return fmt.Errorf("worker: 生成成片失败: %w", err)
	}

	final, err := p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		j.FinalVideoPath = finalPath
		j.Status = domain.JobCompleted
		j.Error = ""
		return nil
	})
	if err != nil {
		return err
	}

	_, _ = p.emit(ctx, &domain.Event{
		JobID: jobID, Node: "compose", Message: "成片合成完成",
		Progress: 1.0, Payload: map[string]any{"final_video_path": finalPath},
		Timestamp: time.Now().UTC(),
	})
	lg.Info("合成完成", "final_path", finalPath, "shots", len(normPaths), "progress", final.Progress)
	return nil
}
