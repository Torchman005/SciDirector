package pbconv

// 本文件覆盖 `payload_json` 的结构化解析（阶段五成本核算的接收端）。
//
// 为什么这条边界需要测试：
// `payload_json` 是两侧**唯一没有 proto 约束**的通道 —— 字段名写错不会有编译错误，
// 只会在运行时表现为「成本一直是 0」。这类缺陷没有任何报错，只能靠测试把它钉住。
// 因此下面的用例里，字段名是**照抄 Python 线上格式**的字面量，不引用任何常量：
// 一旦有人改了任一侧的命名，这里必须跟着变红。

import (
	"strings"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

// realFinalPayload 是 Python `_final_event` 真实产出的形状
// （ai/scidirector_ai/graph/builder.py）。改这个字面量前先去核对那边。
const realFinalPayload = `{"summary":{"total_tokens":350,"calls":3},` +
	`"cost":{"llm_prompt_tokens":100,"llm_completion_tokens":250,` +
	`"llm_total_tokens":350,"llm_calls":3}}`

func TestEventFromPipelineParsesLLMUsage(t *testing.T) {
	ev := EventFromPipeline(&pb.PipelineEvent{
		JobId:       "job-1",
		Node:        "final",
		PayloadJson: realFinalPayload,
	})

	got, ok := ev.Payload["llm_usage"].(domain.LLMUsage)
	if !ok {
		t.Fatalf("payload 里没有结构化的 llm_usage，实际: %#v", ev.Payload["llm_usage"])
	}
	if got.PromptTokens != 100 || got.CompletionTokens != 250 || got.TotalTokens != 350 || got.Calls != 3 {
		t.Fatalf("LLM 用量解析错误: %+v", got)
	}
	if _, ok := ev.Payload["raw"]; !ok {
		t.Fatal("原始 payload_json 必须保留：结构化解析只是加一层方便读取，不该丢掉原文")
	}
}

// 反向控制：没有 cost 字段的 payload（例如 plan 事件）不该凭空造出一个零值用量。
// 否则任务会在还没调用过任何 LLM 时就「拥有」一份用量记录，
// 让「跑没跑过」这件事变得无法判断。
func TestEventFromPipelineWithoutCostLeavesUsageAbsent(t *testing.T) {
	ev := EventFromPipeline(&pb.PipelineEvent{
		JobId:       "job-1",
		Node:        "plan",
		PayloadJson: `{"shots":[{"shot_id":"s1"}]}`,
	})

	if v, ok := ev.Payload["llm_usage"]; ok {
		t.Fatalf("无 cost 字段时不应产生 llm_usage，实际: %#v", v)
	}
}

// payload_json 是「快变结构」的逃生口，解析失败不能连累状态迁移
// —— 但也不能静默：否则 Python 改了字段名，这边只表现为「成本一直是 0」，
// 没有任何报错可查。因此错误必须出现在 payload 里。
func TestEventFromPipelineReportsBadPayloadJSONInsteadOfDroppingEvent(t *testing.T) {
	ev := EventFromPipeline(&pb.PipelineEvent{
		JobId:       "job-1",
		ShotId:      "s1",
		Node:        "final",
		Status:      pb.ShotStatus_SHOT_STATUS_APPROVED,
		PayloadJson: `{"cost": {`,
	})

	if ev.JobID != "job-1" || ev.ShotID != "s1" {
		t.Fatalf("坏 payload 不该让整条事件丢失: %+v", ev)
	}
	if ev.Status != domain.StatusApproved {
		t.Fatalf("状态迁移必须照常生效，实际 %q", ev.Status)
	}
	msg, ok := ev.Payload["raw_parse_error"].(string)
	if !ok || msg == "" {
		t.Fatalf("坏 payload 必须留下可见的解析错误，实际: %#v", ev.Payload["raw_parse_error"])
	}
	if ev.Payload["raw"] != `{"cost": {` {
		t.Fatal("原文仍须保留，便于人工定位")
	}
}

// cost 字段存在但类型不对（例如 Python 侧误传字符串）时，
// 应当报错而不是静默给出 0：0 和「解析失败」在成本上含义完全不同。
func TestEventFromPipelineRejectsWrongCostType(t *testing.T) {
	ev := EventFromPipeline(&pb.PipelineEvent{
		JobId:       "job-1",
		PayloadJson: `{"cost":{"llm_total_tokens":"350"}}`,
	})

	if _, ok := ev.Payload["llm_usage"]; ok {
		t.Fatal("类型不符时不应产生用量记录")
	}
	msg, _ := ev.Payload["raw_parse_error"].(string)
	if !strings.Contains(msg, "cannot unmarshal") {
		t.Fatalf("应留下类型错误说明，实际: %q", msg)
	}
}
