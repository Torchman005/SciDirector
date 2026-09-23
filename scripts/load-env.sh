#!/bin/sh
# 加载仓库根目录的 .env，供 `make dev-*` 等目标使用。
#
# ## 为什么需要它
#
# 根目录的 .env **只在 `docker compose` 插值时生效**：
#   - `make dev-api` 实际在 `backend/` 下运行，而 Go 根本不读 .env 文件，只读进程环境；
#   - `make dev-ai` 在 `ai/` 下运行，而 Python 的 `env_file=".env"` 是**相对当前目录**
#     解析的，于是它找的是 `ai/.env`（不存在）。
# 结果：照着 .env.example 配好再 `make dev-ai`，服务**静默进入 mock 模式** ——
# 内容全是占位，且没有任何报错。这个脚本把那一环补上。
#
# ## 语义（与 dotenv / compose 一致）
#
# 1. **已经在环境里的变量不覆盖**。于是 `make dev-ai SCID_AI_HTTP_PORT=18000`
#    这种命令行覆盖能生效 —— 用 `. ./.env` 会把它冲掉，表现为"端口改了不生效、
#    仍去绑旧端口"，报 address already in use，看起来像有残留进程。
# 2. 逐行解析而不是直接 `.`：.env 是给 shell/docker 的格式，
#    含 `#` 的值不能被当成注释截断。
# 3. 去掉值两侧**成对**的引号（`A="x y"` 得到 `x y`，而不是带引号的字符串）——
#    这也是 dotenv 的行为。
#
# 用法：`. scripts/load-env.sh`（在 shell 里 source，不能直接执行）
# 只读它、不改它：文件不存在时安静返回 0。

# 允许指定别的文件，默认仓库根目录的 .env。
_env_file="${SCID_ENV_FILE:-.env}"

[ -f "$_env_file" ] || return 0

while IFS= read -r _line || [ -n "$_line" ]; do
	# 去行尾回车（Windows 上编辑过的 .env 会带 \r）。
	_line=$(printf '%s' "$_line" | tr -d '\r')

	# 跳过空行与整行注释。
	case "$_line" in
		'' | '#'*) continue ;;
	esac

	# 只处理 KEY=VALUE 形式；其余（例如误写的裸词）跳过，不让整个 dev 目标起不来。
	case "$_line" in
		*=*) ;;
		*) continue ;;
	esac

	_key=${_line%%=*}
	_val=${_line#*=}

	# 去引号 + 去**行内注释**。两者不能颠倒，规则也要分开：
	#
	#   A="值 # 不是注释"   -> 引号内的 # 是值的一部分
	#   B=值   # 注释        -> # 前有空白 ⇒ 从这里截断（bash/dotenv/compose 都这样）
	#   C=ab#cd             -> # 前无空白 ⇒ 保留（密码里带 # 很常见）
	#
	# 我第一版漏了这条规则，于是 `.env.example` 里
	# `SCID_SANDBOX_MANIM_QUALITY=l   # l=480p 草稿…` 的值被整行读进去，
	# pydantic 直接报 literal_error、**服务起不来**；而 compose/dotenv 读同一个文件
	# 都是好的 —— 只有我们这一条路径坏掉，属于典型的"三个消费者两种语义"。
	case "$_val" in
		\"*\")
			_val=${_val#\"} && _val=${_val%\"}
			;;
		\'*\')
			_val=${_val#\'} && _val=${_val%\'}
			;;
		*)
			_val=$(printf '%s' "$_val" | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//')
			;;
	esac

	# 已存在（含被显式设为空串）就不动它 —— 显式环境变量优先于文件。
	if [ -n "$(printenv "$_key" 2>/dev/null)" ]; then
		continue
	fi

	export "$_key=$_val"
done < "$_env_file"

return 0
