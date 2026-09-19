package store

// 保留未知字段的用例（阶段五·多租户期间真实踩到的问题）。
//
// 背景：任务整份 JSON 存在 Redis 里，api 与 worker **都会读改写**。
// 给结构体加字段后若只重启其中一个进程，旧进程一碰任务就把新字段抹掉。
// 本项目真实发生过：新增 tenant_id 后旧 worker 把它擦掉，
// 而「缺失 = default 租户」⇒ 任务的主人反而 404、任务对 default 可见，
// 即一次**授权降级**。

import (
	"encoding/json"
	"testing"
)

func TestMergeUnknownFieldsPreservesFieldsTheStructDoesNotKnow(t *testing.T) {
	// 模拟旧版本写下的值：含一个当前结构体里没有的字段，
	// 以及一个"将来才会加的字段"。
	oldRaw := []byte(`{"job_id":"j1","tenant_id":"acme","future_field":{"nested":[1,2]},"n":1}`)
	// 模拟新版本序列化出来的值：不认识上面那两个键。
	newRaw := []byte(`{"job_id":"j1","n":2}`)

	merged, err := mergeUnknownFields(oldRaw, newRaw)
	if err != nil {
		t.Fatalf("合并失败: %v", err)
	}

	var got map[string]any
	if err := json.Unmarshal(merged, &got); err != nil {
		t.Fatalf("合并结果不是合法 JSON: %v", err)
	}
	if got["tenant_id"] != "acme" {
		t.Fatalf("未知字段 tenant_id 必须被保留（否则就是授权降级），实际 %v", got["tenant_id"])
	}
	if _, ok := got["future_field"]; !ok {
		t.Fatal("嵌套的未知字段也必须原样保留")
	}
	// 已知字段仍以新值为准 —— 保留未知字段不能变成"旧值赢"。
	if got["n"] != float64(2) {
		t.Fatalf("已知字段应当用新值（2），实际 %v", got["n"])
	}
}

// 没有任何未知字段时应当原样返回，不做无谓的重写。
func TestMergeUnknownFieldsNoOpWhenNothingToPreserve(t *testing.T) {
	oldRaw := []byte(`{"job_id":"j1","n":1}`)
	newRaw := []byte(`{"job_id":"j1","n":2}`)

	merged, err := mergeUnknownFields(oldRaw, newRaw)
	if err != nil {
		t.Fatalf("合并失败: %v", err)
	}
	if string(merged) != string(newRaw) {
		t.Fatalf("无需合并时应原样返回，实际 %s", merged)
	}
}

// 旧值是脏数据时，不能让更新失败 —— 那会让一个任务永久无法修改。
func TestMergeUnknownFieldsToleratesCorruptOldValue(t *testing.T) {
	merged, err := mergeUnknownFields([]byte("not json at all"), []byte(`{"job_id":"j1"}`))
	if err != nil {
		t.Fatalf("旧值损坏时不该报错，实际 %v", err)
	}
	if string(merged) != `{"job_id":"j1"}` {
		t.Fatalf("应当直接写新值，实际 %s", merged)
	}
}
