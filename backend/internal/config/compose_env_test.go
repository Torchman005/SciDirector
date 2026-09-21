package config

// 「文档写了、代码读了、但 compose 没透传」是一类**静默失效**：
// 照着 `.env.example` 配置容器部署，那些开关完全不生效，而且没有任何报错。
// 本项目已经因为同一类问题踩过一次（`SCID_S3_*` 与 `SCID_MINIO_*` 各写一套、
// 代码只读其中一套），阶段五的九个个开关又差点重演一遍 —— 是我在回答
// "在哪里配置" 时逐个核对才发现的。
//
// 因此这里把它做成**可执行的检查**：Compose 只会把 `docker-compose.yml` 里
// **显式列出**的变量注入容器，`.env` 的值仅仅参与 `${...}` 插值 ——
// 所以"在 `.env.example` 里写了"不等于"配置得上"。

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// stageFiveSwitches 是**必须能透传到容器**的阶段五开关。
//
// 列成显式清单而不是从源码里扫，是因为"哪些变量属于运行时开关、哪些只是
// 进程内可调"这件事无法从代码推断 —— 而这个清单本身很短、且改动需要理由。
// 新增一个需要部署时配置的开关时，把它加进来即可；忘了加，对应的用例会红。
var stageFiveSwitches = []string{
	// 多租户
	"SCID_TENANT_MODE",
	"SCID_TENANT_HEADER",
	"SCID_TENANT_MAX_ACTIVE_JOBS",
	// 状态对账
	"SCID_RECONCILE_INTERVAL",
	// 沙盒加固
	"SCID_SANDBOX_NETWORK_ISOLATION",
	"SCID_SANDBOX_READ_ONLY",
	"SCID_SANDBOX_SECCOMP",
	"SCID_SANDBOX_PYTHON_BIN",
	"SCID_CHROME",
	// 可观测性（本轮之前就已透传，一起钉住防止回退）
	"SCID_OTEL_ENDPOINT",
	"SCID_METRICS_PATH",
	// 模型服务商（多服务商支持）：容器部署必须能选服务商、配密钥。
	// 这几项曾经只透传了不带 SCID_ 前缀的 OPENAI_API_KEY，
	// 而 Python 侧读的是 SCID_OPENAI_API_KEY ⇒ 容器里**永远进 mock 模式**且无报错。
	"SCID_LLM_PROVIDER",
	"SCID_LLM_MODEL",
	"SCID_VLM_PROVIDER",
	"SCID_VLM_MODEL",
	"SCID_LLM_BASE_URL",
	"SCID_LLM_API_KEY",
	"SCID_OPENAI_API_KEY",
	"SCID_DEEPSEEK_API_KEY",
	"SCID_DASHSCOPE_API_KEY",
}

func repoRoot(t *testing.T) string {
	t.Helper()
	// 本用例的工作目录是包目录（backend/internal/config）。
	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatalf("解析仓库根目录失败: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "docker-compose.yml")); err != nil {
		t.Skipf("找不到仓库根目录（%s），跳过配置透传检查", root)
	}
	return root
}

func TestStageFiveSwitchesReachContainers(t *testing.T) {
	root := repoRoot(t)
	compose, err := os.ReadFile(filepath.Join(root, "docker-compose.yml"))
	if err != nil {
		t.Fatalf("读取 docker-compose.yml 失败: %v", err)
	}
	text := string(compose)

	for _, name := range stageFiveSwitches {
		// 必须同时出现 `SCID_X:`（键）与 `${SCID_X`（插值）：
		// 只有键写死成常量时，`.env` 里改它不会生效；
		// 只有插值而没有键时，它压根不会被注入容器。
		if !strings.Contains(text, name+":") {
			t.Errorf("docker-compose.yml 里没有 %s 这个键：容器拿不到它，"+
				"在 .env 里配置将**静默失效**", name)
		}
		if !strings.Contains(text, "${"+name) {
			t.Errorf("docker-compose.yml 里 %s 没有用 ${...} 插值："+
				".env 里设的值不会被读到", name)
		}
	}
}

// 每个**代码会读**的开关都必须在 `.env.example` 里有据可查。
//
// 反向的一侧：只在代码里悄悄加一个环境变量、却不写进模板，
// 等于"这个开关只存在于读过源码的人脑子里"。
func TestStageFiveSwitchesAreDocumented(t *testing.T) {
	root := repoRoot(t)
	envExample, err := os.ReadFile(filepath.Join(root, ".env.example"))
	if err != nil {
		t.Fatalf("读取 .env.example 失败: %v", err)
	}
	doc := string(envExample)

	for _, name := range stageFiveSwitches {
		if !strings.Contains(doc, name) {
			t.Errorf(".env.example 里没有 %s：部署的人不知道该配什么", name)
		}
	}
}

// Go 侧读到的环境变量名必须都是 `SCID_` 前缀。
//
// 这条不是为了好看：前缀统一才让"在 .env 里搜 SCID_ 就能看全所有可配项"成立。
// 混进一个不带前缀的名字，它就会在文档与部署脚本里被漏掉。
func TestGoConfigOnlyReadsScidPrefixedVars(t *testing.T) {
	src, err := os.ReadFile("config.go")
	if err != nil {
		t.Fatalf("读取 config.go 失败: %v", err)
	}

	// 只看 getEnv/getInt/... 的第一个字符串字面量参数。
	re := regexp.MustCompile(`get(?:Env|EnvFirst|Int|Bool|BoolFirst|Float|Duration|CSV)\(\s*(?:\[\]string\{)?\s*"([A-Z0-9_]+)"`)
	found := re.FindAllStringSubmatch(string(src), -1)
	if len(found) == 0 {
		t.Fatal("没有匹配到任何环境变量读取，正则可能已过期")
	}
	for _, m := range found {
		if !strings.HasPrefix(m[1], "SCID_") {
			t.Errorf("环境变量 %s 没有 SCID_ 前缀：它不会出现在 .env 的常识搜索里", m[1])
		}
	}
}
