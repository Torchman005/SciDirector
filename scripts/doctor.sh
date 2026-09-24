#!/bin/sh
# 本机工具链自检：`make` 目标报错时先跑它。
#
# ## 为什么单独成脚本（而不是写在 Makefile 里）
#
# 因为最常见的"make 报错"就是**根本没有 make**：本机没有时你看到的是
# `bash: make: command not found`，这时 `make doctor` 自己就跑不起来 ——
# 一个需要 make 才能用的诊断工具，在最需要它的场景下恰好不可用。
# 所以这里用纯 sh 实现，`sh scripts/doctor.sh` 与 `make doctor` 等价。
#
# 只读、不改任何东西；每一项都打印"能不能用"以及**缺失时哪个目标会失败**。

set -u

cd "$(dirname "$0")/.." || exit 1

pass() { printf '  \033[32m✓\033[0m %-9s %s\n' "$1" "$2"; }
fail() { printf '  \033[31m✗\033[0m %-9s %s\n' "$1" "$2"; }
info() { printf '  \033[36m·\033[0m %-9s %s\n' "$1" "$2"; }

echo "SciDirector 环境自检"
echo "  仓库根目录: $(pwd)"
echo

# --- make 自身 -------------------------------------------------------------
if command -v make >/dev/null 2>&1; then
	pass make "$(command -v make)"
else
	fail make "未安装 —— 用不了 make 目标，但下面的命令可以照常用："
	echo "              make doctor      → sh scripts/doctor.sh"
	echo "              make dev-infra   → docker compose up -d redis postgres rustfs"
	echo "              make dev-ai      → sh -c '. ./scripts/load-env.sh; cd ai && python -m scidirector_ai.main'"
	echo "              make dev-api     → sh -c '. ./scripts/load-env.sh; cd backend && go run ./cmd/api'"
	echo "              make dev-worker  → sh -c '. ./scripts/load-env.sh; cd backend && go run ./cmd/worker'"
	echo "              make dev-web     → cd web && npm run dev"
	echo "              make test-python → cd ai && python -m pytest -q"
	echo "              make test-go     → cd backend && go test ./internal/..."
	echo "              make up / down   → docker compose up -d --build / docker compose down"
	echo "            Debian/Ubuntu 装它：apt-get install -y make（Alpine: apk add make）"
fi

# --- Go --------------------------------------------------------------------
if command -v go >/dev/null 2>&1; then
	pass go "$(go version 2>/dev/null)"
else
	fail go "未安装 —— build-go / test-go / dev-api / dev-worker 会失败"
fi

# --- Python（含依赖自检）---------------------------------------------------
PY=""
if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON}" ]; then
	PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ]; then
	PY="${VIRTUAL_ENV}/bin/python"
elif [ -x .venv/bin/python ]; then
	PY="$(pwd)/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
	PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
	PY="$(command -v python)"
fi

if [ -z "$PY" ]; then
	fail python "找不到解释器 —— Python 目标全部不可用"
else
	info python "$PY  $("$PY" -V 2>&1)"
	if "$PY" -c 'import fastapi, grpc, langgraph, pydantic, openai' 2>/dev/null; then
		pass "依赖" "已就绪（fastapi/grpc/langgraph/pydantic/openai）"
	else
		fail "依赖" "缺少项目依赖 —— dev-ai / test-python 会失败"
		echo "              修法：make venv  或  python3 -m venv .venv && .venv/bin/pip install -r ai/requirements.txt"
		echo "              或指定解释器：make dev-ai PYTHON=/path/to/venv/bin/python"
	fi
fi

# --- 其它 ------------------------------------------------------------------
if command -v node >/dev/null 2>&1; then
	pass node "$(node -v 2>/dev/null)（dev-web / build-web）"
else
	fail node "未安装 —— dev-web / build-web 会失败"
fi

if docker compose version >/dev/null 2>&1; then
	pass docker "$(docker compose version 2>/dev/null | head -1)（dev-infra / up）"
else
	fail docker "不可用 —— dev-infra / up / logs 会失败"
fi

if command -v ffmpeg >/dev/null 2>&1; then
	pass ffmpeg "$(command -v ffmpeg)（抽帧与合成）"
else
	fail ffmpeg "未安装 —— 渲染与合成会失败"
fi

if command -v protoc >/dev/null 2>&1; then
	pass protoc "$(command -v protoc)"
else
	info protoc "未装系统版；make proto-go 会自动回退到 grpc_tools 自带的 protoc"
fi

if [ -n "${SCID_CHROME:-}" ]; then
	if [ -x "$SCID_CHROME" ]; then
		pass 浏览器 "$SCID_CHROME"
	else
		fail 浏览器 "SCID_CHROME 指向的文件不存在：$SCID_CHROME"
	fi
else
	info 浏览器 "未设置 SCID_CHROME；HTML 引擎走 Playwright 的默认安装路径"
fi

# --- 配置 ------------------------------------------------------------------
echo
if [ -f .env ]; then
	pass .env "存在（make dev-* 与 docker compose 都会读它）"
else
	info .env "不存在 —— 复制模板：cp .env.example .env，然后填模型服务商密钥"
fi
info 说明 "容器里没有 make（三个镜像都没装）：make 是开发机上的工具，请在宿主机执行"
