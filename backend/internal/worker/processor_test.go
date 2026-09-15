package worker

import (
	"encoding/json"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

// pythonPayloadSample 是 **Python 侧真实产出**的 payload_json 样例
// （由 scidirector_ai.pbconv.shots_payload_json 生成，未做任何手工修改）。
//
// 为什么要把这段字面量固化在这里：它是 Python -> Go 的**实际数据通道**。
// Go 用 encoding/json 反序列化到 pb.ShotSpec，其字段名来自 proto 的
// snake_case，枚举是 int32 —— 一旦 Python 侧改成字符串枚举或驼峰字段名，
// 这里的反序列化会把字段全部留成零值，表现为"导演跑完了但一个镜头都没有"，
// 而两侧的单元测试都还是绿的。
//
// Python 侧有对称的测试（ai/tests/test_pbconv.py::TestShotsPayloadJSON）
// 用同一份样例断言输出格式，两侧共同构成跨语言契约的回归网。
const pythonPayloadSample = `{
  "outline": "大纲",
  "shots": [
    {"shot_id": "job-x-s000", "index": 0, "narration": "开场旁白", "visual_brief": "标题淡入",
     "tag": 4, "engine": 5, "duration_sec": 4.5, "keywords": ["开场"]},
    {"shot_id": "job-x-s001", "index": 1, "narration": "公式推导", "visual_brief": "居中展示公式",
     "tag": 1, "engine": 1, "duration_sec": 12.0, "keywords": ["公式", "推导"]}
  ]
}`

// TestParsePythonPayload 验证 Go 能正确解析 Python 产出的分镜表。
func TestParsePythonPayload(t *testing.T) {
	var wrap struct {
		Shots   []json.RawMessage `json:"shots"`
		Outline string            `json:"outline"`
	}
	if err := json.Unmarshal([]byte(pythonPayloadSample), &wrap); err != nil {
		t.Fatalf("样例 payload 本身不是合法 JSON: %v", err)
	}
	if wrap.Outline != "大纲" {
		t.Errorf("outline 期望 %q，实际 %q", "大纲", wrap.Outline)
	}
	if len(wrap.Shots) != 2 {
		t.Fatalf("分镜数期望 2，实际 %d", len(wrap.Shots))
	}

	// 逐条反序列化到真实的 pb 结构 —— 这一步才是真正的契约验证。
	var first pb.ShotSpec
	if err := json.Unmarshal(wrap.Shots[0], &first); err != nil {
		t.Fatalf("反序列化分镜失败（字段名或枚举编码不匹配？）: %v", err)
	}

	if first.ShotId != "job-x-s000" {
		t.Errorf("shot_id 未解析出来：%q（字段名可能不是 snake_case）", first.ShotId)
	}
	if first.Narration != "开场旁白" {
		t.Errorf("narration 未解析出来：%q", first.Narration)
	}
	if first.DurationSec != 4.5 {
		t.Errorf("duration_sec 期望 4.5，实际 %v", first.DurationSec)
	}
	// 枚举必须是数值：Python 若发 "AMBIENCE" 字符串，这里会解析失败或留成 0。
	if first.Tag != pb.SceneTag_SCENE_TAG_AMBIENCE {
		t.Errorf("tag 期望 AMBIENCE(%d)，实际 %d —— 枚举可能被发成了字符串",
			pb.SceneTag_SCENE_TAG_AMBIENCE, first.Tag)
	}
	if first.Engine != pb.RenderEngine_RENDER_ENGINE_STOCK {
		t.Errorf("engine 期望 STOCK(%d)，实际 %d",
			pb.RenderEngine_RENDER_ENGINE_STOCK, first.Engine)
	}
	if len(first.Keywords) != 1 || first.Keywords[0] != "开场" {
		t.Errorf("keywords 未正确解析：%v", first.Keywords)
	}

	var second pb.ShotSpec
	if err := json.Unmarshal(wrap.Shots[1], &second); err != nil {
		t.Fatalf("反序列化第二个分镜失败: %v", err)
	}
	if second.Tag != pb.SceneTag_SCENE_TAG_MATH {
		t.Errorf("tag 期望 MATH，实际 %d", second.Tag)
	}
	if second.Engine != pb.RenderEngine_RENDER_ENGINE_MANIM {
		t.Errorf("engine 期望 MANIM，实际 %d", second.Engine)
	}
}

// TestSyncShotsFromPayloadParses 验证从 payload_json 同步分镜表。
func TestSyncShotsFromPayloadParses(t *testing.T) {
	job := &domain.Job{JobID: "job-x", Shots: []*domain.Shot{}}

	if err := syncShotsFromPayload(job, pythonPayloadSample); err != nil {
		t.Fatalf("同步失败: %v", err)
	}

	if len(job.Shots) != 2 {
		t.Fatalf("期望 2 个镜头，实际 %d", len(job.Shots))
	}

	first := job.Shots[0]
	if first.ShotID != "job-x-s000" {
		t.Errorf("shot_id 错误：%q", first.ShotID)
	}
	if first.Tag != domain.TagAmbience {
		t.Errorf("tag 期望 AMBIENCE，实际 %q", first.Tag)
	}
	// 引擎必须由标签推导，而不是照抄 payload 里的值。
	if first.Engine != domain.EngineStock {
		t.Errorf("engine 期望 stock，实际 %q", first.Engine)
	}
	if first.Status != domain.StatusPending {
		t.Errorf("新镜头应当是 PENDING，实际 %q", first.Status)
	}
	if first.Index != 0 {
		t.Errorf("index 期望 0，实际 %d", first.Index)
	}

	second := job.Shots[1]
	if second.Tag != domain.TagMath || second.Engine != domain.EngineManim {
		t.Errorf("第二个镜头路由错误：tag=%q engine=%q", second.Tag, second.Engine)
	}
	// keywords 是导演用来检索 Few-shot 的依据，丢了会拉低一次通过率。
	if len(second.Keywords) != 2 {
		t.Errorf("keywords 丢失：%v", second.Keywords)
	}
}

// TestSyncShotsFromPayloadPreservesProgress 验证重复收到 plan 事件时
// **不重置**已完成的渲染进度。
//
// 场景：任务中断后断点续跑，导演会重新发一次分镜表。
// 若这里整体覆盖，已经渲染好的镜头会被打回 PENDING，白烧一遍渲染成本。
func TestSyncShotsFromPayloadPreservesProgress(t *testing.T) {
	existing := &domain.Shot{
		ShotID:  "job-x-s000",
		JobID:   "job-x",
		Index:   0,
		Tag:     domain.TagAmbience,
		Status:  domain.StatusApproved,
		Attempt: 2,
		Artifact: &domain.Artifact{
			ArtifactID:  "a1",
			ShotID:      "job-x-s000",
			VideoPath:   "/data/work/job-x/shot_000/out.mp4",
			DurationSec: 4.5,
		},
	}
	job := &domain.Job{JobID: "job-x", Shots: []*domain.Shot{existing}}

	if err := syncShotsFromPayload(job, pythonPayloadSample); err != nil {
		t.Fatalf("同步失败: %v", err)
	}

	kept := job.FindShot("job-x-s000")
	if kept == nil {
		t.Fatal("同步后原镜头丢失")
	}
	if kept.Status != domain.StatusApproved {
		t.Errorf("已通过的镜头被打回成 %q —— 续跑会白烧一轮渲染", kept.Status)
	}
	if kept.Attempt != 2 {
		t.Errorf("尝试次数被重置为 %d，熔断判定会失效", kept.Attempt)
	}
	if kept.Artifact == nil || kept.Artifact.VideoPath == "" {
		t.Error("渲染产物被清空了")
	}
	// 但叙事元数据应当被更新（导演的产出是权威快照）。
	if kept.VisualBrief != "标题淡入" {
		t.Errorf("视觉意图未被更新：%q", kept.VisualBrief)
	}
}

// TestSyncShotsFromPayloadRejectsBadJSON 验证坏载荷返回错误而不是 panic。
func TestSyncShotsFromPayloadRejectsBadJSON(t *testing.T) {
	job := &domain.Job{JobID: "job-x"}
	if err := syncShotsFromPayload(job, "{ 这不是合法 JSON"); err == nil {
		t.Error("坏 JSON 应当返回错误")
	}
	if err := syncShotsFromPayload(job, `{"shots": [{"index": "不是数字"}]}`); err == nil {
		t.Error("字段类型错误应当返回错误")
	}
}

// TestSyncShotsFromPayloadEmptyShots 空分镜表不应改变现状。
func TestSyncShotsFromPayloadEmptyShots(t *testing.T) {
	job := &domain.Job{JobID: "job-x", Shots: []*domain.Shot{{ShotID: "keep-me"}}}
	if err := syncShotsFromPayload(job, `{"outline": "x", "shots": []}`); err != nil {
		t.Fatalf("空分镜表不该报错: %v", err)
	}
	if len(job.Shots) != 1 || job.Shots[0].ShotID != "keep-me" {
		t.Error("空分镜表覆盖了已有镜头")
	}
}

// TestSyncShotsFromPayloadFixesInvalidTag 非法标签必须降级而不是让任务卡死。
func TestSyncShotsFromPayloadFixesInvalidTag(t *testing.T) {
	payload := `{"shots": [{"index": 0, "narration": "n", "tag": 99, "engine": 99,
	                       "duration_sec": 5.0}]}`
	job := &domain.Job{JobID: "job-x"}

	if err := syncShotsFromPayload(job, payload); err != nil {
		t.Fatalf("同步失败: %v", err)
	}
	if job.Shots[0].Tag != domain.TagAmbience {
		t.Errorf("非法标签应当降级为 AMBIENCE，实际 %q", job.Shots[0].Tag)
	}
	// 降级后引擎必须重新推导，否则会用 payload 里的非法值。
	if job.Shots[0].Engine != domain.EngineStock {
		t.Errorf("引擎应当按降级后的标签推导为 stock，实际 %q", job.Shots[0].Engine)
	}
}

// TestSyncShotsFromPayloadDerivesID 缺少 shot_id 时按索引稳定派生。
func TestSyncShotsFromPayloadDerivesID(t *testing.T) {
	payload := `{"shots": [{"index": 0, "narration": "a"}, {"index": 1, "narration": "b"}]}`
	job := &domain.Job{JobID: "job-x"}

	if err := syncShotsFromPayload(job, payload); err != nil {
		t.Fatalf("同步失败: %v", err)
	}
	if job.Shots[0].ShotID != domain.ShotID("job-x", 0) {
		t.Errorf("派生的 shot_id 不稳定：%q", job.Shots[0].ShotID)
	}
	if job.Shots[1].ShotID != domain.ShotID("job-x", 1) {
		t.Errorf("派生的 shot_id 不稳定：%q", job.Shots[1].ShotID)
	}
}
