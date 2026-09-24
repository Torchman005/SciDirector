package config

// backend/Dockerfile 里的 Go 版本必须**满足 backend/go.mod 的 go 指令**。
//
// 为什么值得一条用例：这两处分别演进，而它们不一致的唯一表现是
// **`docker compose build` 失败**：
//
//	go.mod requires go >= 1.24.0 (running go 1.23.12; GOTOOLCHAIN=local)
//
// 而本地开发用的是宿主机的 Go（比镜像里的新），跑 `go test` / `go build` 完全正常 ——
// 也就是说"改依赖时把 go 指令顶上去"这件事**在本地永远看不出来**，
// 只有真正构建镜像的人才撞得到。本项目真实发生过：加入 OpenTelemetry 后
// go.mod 升到 1.24，而 Dockerfile 还钉着 1.23，api/worker 镜像直接构建不出来。

import (
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"testing"
)

var (
	goDirectiveRe = regexp.MustCompile(`(?m)^go\s+(\d+)\.(\d+)`)
	dockerGoTagRe = regexp.MustCompile(`(?m)^FROM\s+golang:(\d+)\.(\d+)`)
)

func goVersionPair(t *testing.T, content, what string, re *regexp.Regexp) (int, int) {
	t.Helper()
	m := re.FindStringSubmatch(content)
	if m == nil {
		t.Fatalf("%s 里找不到 Go 版本，正则可能已过期", what)
	}
	major, _ := strconv.Atoi(m[1])
	minor, _ := strconv.Atoi(m[2])
	return major, minor
}

func TestDockerfileGoVersionSatisfiesGoMod(t *testing.T) {
	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatalf("解析仓库根目录失败: %v", err)
	}
	goMod, err := os.ReadFile(filepath.Join(root, "backend", "go.mod"))
	if err != nil {
		t.Skipf("找不到 go.mod（%v），跳过镜像版本检查", err)
	}
	dockerfile, err := os.ReadFile(filepath.Join(root, "backend", "Dockerfile"))
	if err != nil {
		t.Skipf("找不到 backend/Dockerfile（%v），跳过镜像版本检查", err)
	}

	wantMajor, wantMinor := goVersionPair(t, string(goMod), "go.mod", goDirectiveRe)
	gotMajor, gotMinor := goVersionPair(t, string(dockerfile), "backend/Dockerfile", dockerGoTagRe)

	if gotMajor != wantMajor || gotMinor < wantMinor {
		t.Fatalf(
			"镜像里的 Go 版本(golang:%d.%d)低于 go.mod 要求的 %d.%d：\n"+
				"  `docker compose build api` 会以 "+
				"`go.mod requires go >= %d.%d.0 (running go …)` 失败，\n"+
				"  而本地用宿主 Go 跑 go build/test 一切正常 —— 因此这类不一致只能靠这条用例拦住。\n"+
				"  修法：把 backend/Dockerfile 的 FROM golang:… 升到 >= %d.%d。",
			gotMajor, gotMinor, wantMajor, wantMinor, wantMajor, wantMinor, wantMajor, wantMinor,
		)
	}
}
