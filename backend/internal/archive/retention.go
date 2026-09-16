package archive

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Entry 是本地卷上的一个候选清理对象。
type Entry struct {
	Path      string
	Kind      ObjectKind
	SizeBytes int64
}

// CleanupOptions 是保留策略开关。
type CleanupOptions struct {
	// KeepAll 为 true 时什么都不删。
	// 排查线上问题时常需要保留现场，因此必须有一个一刀切的开关，
	// 而不是让工程师逐个 kind 去争论。
	KeepAll bool
	// KeepNormalized 保留归一化中间产物。
	// 逐镜头重做时它可以省掉一次转码，但体积接近成片，默认不保留。
	KeepNormalized bool
}

// PlanCleanup 决定归档成功后哪些本地文件可以删除。
//
// 这是**纯函数**：不碰 IO、不看时间，只依据类别与开关做判断，
// 因此策略本身可以被完整单测。删文件是不可逆操作，
// 把它做成纯函数是为了让"什么情况下会删什么"成为可被测试断言的事情，
// 而不是散落在若干 if 里的隐式行为。
//
// 策略与理由：
//
//	成片 / 字幕      保留。播放、回放、人工审核都要用；而且它们是交付物。
//	单镜头片段       保留。HITL 打回重做依赖它（局部重渲染更是直接拿它当原片），
//	                 删掉就等于每次重做都得整镜重渲 —— 与降本目标直接冲突。
//	归一化中间产物   默认删。体积接近成片，且可由原片重新生成。
//	抽帧 PNG         始终删。体积大、数量多、可随时从视频重建，
//	                 是本地卷被撑爆的首要原因。
func PlanCleanup(entries []Entry, opt CleanupOptions) []string {
	if opt.KeepAll {
		return nil
	}

	var doomed []string
	for _, e := range entries {
		switch e.Kind {
		case KindFrame:
			doomed = append(doomed, e.Path)
		case KindNormalized:
			if !opt.KeepNormalized {
				doomed = append(doomed, e.Path)
			}
		case KindFinal, KindSubtitle, KindShot:
			// 保留：见上面的理由。
		default:
			// 未知类别一律保留。删除是不可逆的，
			// 遇到没见过的类型时保守处理才是正确默认值。
		}
	}
	sort.Strings(doomed)
	return doomed
}

// Cleanup 按计划删除文件，返回实际删除数量与失败列表。
//
// 删除失败不返回错误：清理是"尽力而为"的收尾动作，
// 不该因为它把一次已经成功的任务判成失败。
// 失败项由调用方记日志，供人工排查磁盘占用问题。
func Cleanup(plan []string) (removed int, failed map[string]error) {
	failed = map[string]error{}
	for _, p := range plan {
		if err := os.RemoveAll(p); err != nil {
			failed[p] = err
			continue
		}
		removed++
	}
	return removed, failed
}

// CollectEntries 扫描一个目录，按扩展名推断每个文件的类别。
//
// 推断规则刻意保持简单（目录名 + 扩展名），因为这里的输入是我们自己
// 在合成流程里生成的已知结构，不需要通用的文件分类器。
// 未知文件一律归为未知类别 —— 而 PlanCleanup 对未知类别是保留的。
func CollectEntries(root string) ([]Entry, error) {
	var entries []Entry
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			// 单个条目读不到不应让整个扫描失败：清理是尽力而为的。
			return nil //nolint:nilerr // 有意忽略
		}
		if info.IsDir() {
			return nil
		}
		entries = append(entries, Entry{
			Path:      path,
			Kind:      inferKind(path),
			SizeBytes: info.Size(),
		})
		return nil
	})
	if err != nil {
		return nil, err
	}
	return entries, nil
}

// inferKind 依据路径推断类别。
func inferKind(path string) ObjectKind {
	slash := filepath.ToSlash(path)
	ext := strings.ToLower(filepath.Ext(path))
	dir := strings.ToLower(filepath.ToSlash(filepath.Dir(path)))

	switch {
	case ext == ".srt" || ext == ".ass":
		return KindSubtitle
	case ext == ".png":
		return KindFrame
	case strings.Contains(slash, "/normalized/"):
		return KindNormalized
	case strings.HasSuffix(dir, "frames"):
		return KindFrame
	default:
		return KindShot
	}
}
