package worker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/archive"
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
	index     int    // 分镜序号，决定在成片中的位置
	path      string // 视频文件路径
	narration string // 画外音原文，字幕来源
}

// subtitleTailMarginSec 是字幕末尾安全边距。
//
// 不设这个边距时，最后一条字幕会恰好结束于视频末尾 —— 而那个组合会让
// 封装阶段的时长裁剪把字幕包压成零长（字幕流还在、内容为空）。
const subtitleTailMarginSec = 0.1

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
		items = append(items, shotItem{
			index:     s.Index,
			path:      s.Artifact.VideoPath,
			narration: s.Narration,
		})
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
	// durations[i] 是归一化**之后**的时长，转场 offset 必须用它而不是原始时长：
	// 归一化的 fps 变换会带来毫秒级差异，而 offset 是累积量，误差会逐段放大。
	durations := make([]float64, len(items))

	// 任一分支失败即取消其余分支：正在跑的 ffmpeg 会收到取消并退出，不白烧 CPU。
	// pool.Run 保证返回时**所有**分支都已收敛，因此下面可以安全地读 normPaths。
	err = pool.Run(ctx, len(items), func(gctx context.Context, i int) error {
		probe, perr := p.media.Probe(gctx, items[i].path)
		if perr != nil {
			return fmt.Errorf("分镜 %d 产物无效: %w", items[i].index, perr)
		}
		durations[i] = probe.DurationSec

		// 已经符合目标规格就跳过转码：转码既有损又耗时，能省则省。
		//
		// 判定条件必须包含 HasAudio：无音轨的片段混进合成流程会让
		// concat 错位、让 acrossfade 直接报错。宁可多转一次码，也不要放进去。
		spec := p.media.DefaultNormalizeSpec()
		if probe.Width == spec.Width &&
			probe.Height == spec.Height &&
			int(probe.FPS+0.5) == spec.FPS &&
			probe.PixFmt == "yuv420p" &&
			probe.HasAudio {
			normPaths[i] = items[i].path
			return nil
		}

		out := filepath.Join(normDir, fmt.Sprintf("norm_%03d.mp4", items[i].index))
		if nerr := p.media.Normalize(gctx, items[i].path, out, spec); nerr != nil {
			return fmt.Errorf("分镜 %d 归一化失败: %w", items[i].index, nerr)
		}
		normPaths[i] = out
		// 归一化会改变时长（帧率对齐、补边），重新探测取准。
		normProbe, nperr := p.media.Probe(gctx, out)
		if nperr != nil {
			return fmt.Errorf("分镜 %d 归一化产物无效: %w", items[i].index, nperr)
		}
		durations[i] = normProbe.DurationSec
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

	// 阶段二：把归一化后的片段接起来。
	//
	// 两条路径，代价差别很大，因此必须显式决策并把结论写进事件：
	//   - 硬切：concat demuxer + -c copy，无重编码，最快；
	//   - 转场：xfade 必须解码再编码，整条成片都要重来一遍。
	//
	// 是否启用由 PlanTransitions 判定（纯函数，可单测），它同时给出
	// 「为什么没启用」的原因 —— 否则「配置了转场却没生效」只能靠读源码回答。
	plan := media.PlanTransitions(durations, p.media.Transition())
	mergedPath := filepath.Join(workDir, "merged.mp4")

	if plan.Enabled {
		if err := p.media.ConcatWithTransition(ctx, normPaths, mergedPath, plan); err != nil {
			return fmt.Errorf("worker: 带转场合并分镜失败: %w", err)
		}
		lg.Info("已使用转场合成",
			"transition", string(plan.Type),
			"duration_sec", plan.Duration,
			"out_duration_sec", plan.OutDuration)
	} else {
		// 降级原因必须可见：静默硬切会让配置错误永远不被发现。
		lg.Info("使用硬切合成", "reason", plan.Reason)

		listPath := filepath.Join(workDir, "concat.txt")
		if err := media.WriteConcatList(listPath, normPaths); err != nil {
			return err
		}
		if err := p.media.Concat(ctx, listPath, mergedPath); err != nil {
			return fmt.Errorf("worker: 合并分镜失败: %w", err)
		}
	}

	// 阶段三：生成字幕。
	//
	// 字幕窗口必须建立在**转场之后的**时间轴上：转场是交叠而非插入，
	// 成片比片段之和短 (n-1)×T。若按原始时长累加去定位字幕，
	// 每过一个转场就往后偏 T 秒，越往后错得越明显 ——
	// 而片头几秒看起来完全正常，因此极容易被漏掉。
	subtitlePath := ""
	if p.cfg.Media.SubtitleEnabled {
		narrations := make([]string, len(items))
		for i := range items {
			narrations[i] = items[i].narration
		}
		windows := media.PlanShotWindows(durations, plan)
		cues := media.PlanCues(windows, narrations, p.subtitleOptions())

		// 用**探测到的**成片真实时长裁剪字幕，而不是用方案预测值 ——
		// 两者会有毫秒级差异，而越界字幕是观众能直接看到的错误。
		// 末尾边距同时避开「字幕恰好结束于视频末尾」这个会让封装
		// 把字幕压成零长的退化组合。
		if merged, perr := p.media.Probe(ctx, mergedPath); perr == nil {
			cues = media.ClampCuesToDuration(cues, merged.DurationSec, subtitleTailMarginSec)
		} else {
			lg.Warn("探测成片时长失败，字幕未做裁剪", "error", perr.Error())
		}

		if len(cues) > 0 {
			subtitlePath = filepath.Join(workDir, "final.srt")
			if err := media.WriteSRT(subtitlePath, cues); err != nil {
				// 字幕生成失败不应让整部片子失败：画面才是主体。
				lg.Warn("字幕生成失败，将产出无字幕成片", "error", err.Error())
				subtitlePath = ""
			} else {
				lg.Info("字幕已生成", "cues", len(cues), "path", subtitlePath)
			}
		} else {
			lg.Info("没有可用的画外音文本，跳过字幕")
		}
	}

	// 阶段四：产出最终文件。
	//
	// audioPath 仍传空：全片统一 TTS 配音尚未接入（见 ROADMAP 阶段三待办）。
	// 接口已经就绪，届时只需把合成的音轨路径传进来，不必改动调用契约。
	finalPath := filepath.Join(workDir, "final.mp4")
	if err := p.media.MuxFinal(ctx, mergedPath, "", subtitlePath, finalPath); err != nil {
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
		Progress: 1.0,
		Payload: map[string]any{
			"final_video_path": finalPath,
			"subtitle_path":    subtitlePath,
			"transition":       string(plan.Type),
			"transition_used":  plan.Enabled,
			"out_duration_sec": plan.OutDuration,
		},
		Timestamp: time.Now().UTC(),
	})
	lg.Info("合成完成", "final_path", finalPath, "shots", len(normPaths),
		"subtitle", subtitlePath, "progress", final.Progress)

	// 收尾：归档 + 本地清理。**失败不改变任务结果** ——
	// 产物已经生成、任务已经成功，因为对象存储抖动就把它判成失败，
	// 代价远大于收益（用户看到的是"失败"，而片子其实好好的）。
	p.finalizeArtifacts(ctx, jobID, finalPath, subtitlePath, workDir, lg)

	return nil
}

// finalizeArtifacts 归档交付物并按策略清理本地中间产物。
//
// 顺序不能反：**先归档、后清理**。反过来会在归档失败时把唯一的副本删掉。
// 虽然归档失败时我们本来就不会执行清理（见下），但把顺序写死比依赖条件更可靠。
func (p *Processor) finalizeArtifacts(
	ctx context.Context,
	jobID, finalPath, subtitlePath, workDir string,
	lg *slog.Logger,
) {
	archived := true

	if p.archive.Enabled() {
		type item struct {
			path string
			kind archive.ObjectKind
		}
		items := []item{{path: finalPath, kind: archive.KindFinal}}
		if subtitlePath != "" {
			items = append(items, item{path: subtitlePath, kind: archive.KindSubtitle})
		}

		for _, it := range items {
			key := archive.ObjectKey(jobID, it.kind, filepath.Base(it.path))
			uri, err := p.archive.Put(ctx, it.path, key)
			if err != nil {
				archived = false
				lg.Error("产物归档失败", "path", it.path, "key", key, "error", err.Error())
				continue
			}
			lg.Info("产物已归档", "path", it.path, "uri", uri, "backend", p.archive.Kind())
		}

		_, _ = p.emit(ctx, &domain.Event{
			JobID: jobID, Node: "compose",
			Message:   fmt.Sprintf("产物已归档到 %s", p.archive.Kind()),
			Payload:   map[string]any{"backend": p.archive.Kind(), "ok": archived},
			Timestamp: time.Now().UTC(),
		})
	}

	// 归档失败时**不清理**：本地那份可能就是唯一的副本。
	// 这是"宁可占盘，不可丢件"的取舍 —— 磁盘可以加，数据丢了找不回来。
	if !archived {
		lg.Warn("归档未全部成功，跳过本地清理以保留唯一副本")
		return
	}

	entries, err := archive.CollectEntries(workDir)
	if err != nil {
		lg.Warn("扫描本地产物失败，跳过清理", "error", err.Error())
		return
	}
	plan := archive.PlanCleanup(entries, archive.CleanupOptions{
		KeepAll:        p.cfg.Archive.KeepAll,
		KeepNormalized: p.cfg.Archive.KeepNormalized,
	})
	if len(plan) == 0 {
		return
	}

	removed, failed := archive.Cleanup(plan)
	var freed int64
	for _, e := range entries {
		for _, p := range plan {
			if e.Path == p {
				freed += e.SizeBytes
			}
		}
	}
	lg.Info("本地中间产物已清理",
		"removed", removed, "failed", len(failed), "freed_mb", freed>>20)
	for path, ferr := range failed {
		lg.Warn("删除中间产物失败", "path", path, "error", ferr.Error())
	}
}

// subtitleOptions 把配置映射成字幕参数。
func (p *Processor) subtitleOptions() media.SubtitleOptions {
	opt := media.DefaultSubtitleOptions()
	if p.cfg.Media.SubtitleMaxCharsPerCue > 0 {
		opt.MaxCharsPerCue = p.cfg.Media.SubtitleMaxCharsPerCue
	}
	if p.cfg.Media.SubtitleMinCueSec > 0 {
		opt.MinCueSec = p.cfg.Media.SubtitleMinCueSec
	}
	if p.cfg.Media.SubtitleMaxCueSec > 0 {
		opt.MaxCueSec = p.cfg.Media.SubtitleMaxCueSec
	}
	return opt
}
