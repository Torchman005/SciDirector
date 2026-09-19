package media

// 配音时间戳 sidecar 的读取 —— 这是 **Python 与 Go 之间的契约**。
//
// 契约坏掉的表现很隐蔽：读不到时间戳时只是**降级**（字幕退回按配音时长对齐），
// 不会报错、不会失败，成片照样出。因此必须有一条用例拿**真实的 sidecar 内容**来钉住它，
// 而不是自己编一份「看起来对」的 JSON —— 那种用例只能证明我们和自己的想象一致。

import (
	"os"
	"path/filepath"
	"testing"
)

// realSidecarSample 是 **Python 侧真实产出**的 sidecar 内容
// （由 `scidirector_ai.tts.base.write_marks_sidecar` 写出，未经手工修改）。
//
// 与 Go 侧 struct 的字段名（version / provider / duration_sec /
// marks[].{text,start_sec,duration_sec}）必须一一对应。
const realSidecarSample = `{
  "version": 1,
  "provider": "edge",
  "duration_sec": 3.7625,
  "marks": [
    {
      "text": "mock）开场：抛出问题，建立悬念。",
      "start_sec": 0.1,
      "duration_sec": 3.6625
    }
  ]
}`

func writeSidecar(t *testing.T, audioPath, content string) {
	t.Helper()
	if err := os.WriteFile(NarrationMarksPath(audioPath), []byte(content), 0o644); err != nil {
		t.Fatalf("写 sidecar 失败: %v", err)
	}
}

func TestReadNarrationMarksParsesRealPythonOutput(t *testing.T) {
	dir := t.TempDir()
	audio := filepath.Join(dir, "narration.mp3")
	if err := os.WriteFile(audio, []byte("fake"), 0o644); err != nil {
		t.Fatalf("写音频失败: %v", err)
	}
	writeSidecar(t, audio, realSidecarSample)

	marks, err := ReadNarrationMarks(audio)
	if err != nil {
		t.Fatalf("解析真实的 Python 产出失败（两侧契约可能已经对不上）: %v", err)
	}
	if len(marks) != 1 {
		t.Fatalf("期望 1 条时间戳，实际 %d", len(marks))
	}
	if marks[0].StartSec != 0.1 {
		t.Errorf("start_sec 期望 0.1，实际 %v —— 字段名可能对不上", marks[0].StartSec)
	}
	if marks[0].DurationSec != 3.6625 {
		t.Errorf("duration_sec 期望 3.6625，实际 %v", marks[0].DurationSec)
	}
	if marks[0].EndSec() != 3.7625 {
		t.Errorf("EndSec() 期望 3.7625，实际 %v", marks[0].EndSec())
	}
	if marks[0].Text == "" {
		t.Error("text 未解析出来")
	}
}

// TestReadNarrationMarksMissingSidecarIsNotAnError 覆盖最常见的情形：
// 服务商不给时间戳（或压根没接 TTS）。**这不是错误**，调用方直接回退。
func TestReadNarrationMarksMissingSidecarIsNotAnError(t *testing.T) {
	dir := t.TempDir()
	audio := filepath.Join(dir, "narration.mp3")
	if err := os.WriteFile(audio, []byte("fake"), 0o644); err != nil {
		t.Fatalf("写音频失败: %v", err)
	}

	marks, err := ReadNarrationMarks(audio)
	if err != nil {
		t.Errorf("没有 sidecar 不该报错（应静默回退），实际 %v", err)
	}
	if marks != nil {
		t.Errorf("没有 sidecar 应返回 nil，实际 %v", marks)
	}
}

// TestReadNarrationMarksBrokenSidecarIsAnError 区分「没有」与「用不了」。
//
// 后者意味着**两侧契约已经对不上**，必须让调用方留痕 ——
// 否则会一直静默地用旧精度，没人会发现时间戳早就没生效了。
func TestReadNarrationMarksBrokenSidecarIsAnError(t *testing.T) {
	cases := map[string]string{
		"不是 JSON":    `{ 这不是 JSON`,
		"版本不认识":      `{"version": 99, "marks": [{"text":"x","start_sec":0,"duration_sec":1}]}`,
		"缺 version":  `{"marks": [{"text":"x","start_sec":0,"duration_sec":1}]}`,
		"marks 类型不对": `{"version": 1, "marks": "不是数组"}`,
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			audio := filepath.Join(dir, "a.mp3")
			if err := os.WriteFile(audio, []byte("fake"), 0o644); err != nil {
				t.Fatalf("写音频失败: %v", err)
			}
			writeSidecar(t, audio, content)

			marks, err := ReadNarrationMarks(audio)
			if err == nil {
				t.Errorf("sidecar 内容为 %s 时应当报错（以便调用方留痕），实际 marks=%v", name, marks)
			}
			if marks != nil {
				t.Errorf("出错时应返回 nil，实际 %v", marks)
			}
		})
	}
}

// TestReadNarrationMarksSkipsBadEntries 单条坏记录只跳过它，不让整份时间戳作废。
func TestReadNarrationMarksSkipsBadEntries(t *testing.T) {
	dir := t.TempDir()
	audio := filepath.Join(dir, "a.mp3")
	if err := os.WriteFile(audio, []byte("fake"), 0o644); err != nil {
		t.Fatalf("写音频失败: %v", err)
	}
	writeSidecar(t, audio, `{"version":1,"marks":[
		{"text":"好的","start_sec":0.0,"duration_sec":1.0},
		{"text":"零时长","start_sec":1.0,"duration_sec":0.0},
		{"text":"负起点","start_sec":-2.0,"duration_sec":1.0}
	]}`)

	marks, err := ReadNarrationMarks(audio)
	if err != nil {
		t.Fatalf("不该报错: %v", err)
	}
	if len(marks) != 1 || marks[0].Text != "好的" {
		t.Errorf("期望只保留 1 条合法记录，实际 %+v", marks)
	}
}

// TestReadNarrationMarksEmptyMarksEqualsNoMarks 有文件但一条可用记录都没有时，
// 等同于没有时间戳（回退），而不是报错 —— 它没有「契约对不上」的含义。
func TestReadNarrationMarksEmptyMarksEqualsNoMarks(t *testing.T) {
	dir := t.TempDir()
	audio := filepath.Join(dir, "a.mp3")
	if err := os.WriteFile(audio, []byte("fake"), 0o644); err != nil {
		t.Fatalf("写音频失败: %v", err)
	}
	writeSidecar(t, audio, `{"version":1,"marks":[]}`)

	marks, err := ReadNarrationMarks(audio)
	if err != nil || marks != nil {
		t.Errorf("空 marks 应当等同「没有时间戳」(nil, nil)，实际 marks=%v err=%v", marks, err)
	}
}

// TestReadNarrationMarksEmptyAudioPath 空路径直接返回，不去 stat 空文件名。
func TestReadNarrationMarksEmptyAudioPath(t *testing.T) {
	marks, err := ReadNarrationMarks("")
	if err != nil || marks != nil {
		t.Errorf("空路径应返回 (nil, nil)，实际 marks=%v err=%v", marks, err)
	}
}

// TestNarrationMarksPathConvention 钉住命名约定 —— Python 侧必须写同一个文件名。
func TestNarrationMarksPathConvention(t *testing.T) {
	got := NarrationMarksPath("/data/work/shot_000/narration.mp3")
	want := "/data/work/shot_000/narration.mp3.marks.json"
	if got != want {
		t.Errorf("sidecar 路径约定变了：期望 %s，实际 %s（Python 侧用的是原名 + .marks.json）", want, got)
	}
}
