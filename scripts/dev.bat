@echo off
rem ============================================================================
rem 注意：chcp 必须排在**任何中文之前**。
rem   cmd.exe 是按当前码页**边解析边执行**的，而中文注释在 GBK 码页下会被
rem   错误解码，进而破坏语法（本项目实测：报出的错误指向的字符与真正的原因
rem   毫无关系）。所以这一行之后才允许出现非 ASCII 字符。
rem
rem 同时本文件必须是 **CRLF** 换行（见 .gitattributes 里的 *.cmd 规则）——
rem   LF-only 的批处理会让 cmd.exe 吃掉行首字符。
rem ============================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================================
rem SciDirector —— Windows 原生启动脚本
rem ----------------------------------------------------------------------------
rem 用途：不依赖 Docker / make / sh，直接在 cmd.exe 里把整套服务拉起来。
rem
rem 用法：
rem     scripts\dev.bat check     环境自检（工具链 + Python 依赖版本）
rem     scripts\dev.bat build     构建 Go 的 api / worker
rem     scripts\dev.bat start     启动全部（Redis + AI + API + worker + 前端）
rem     scripts\dev.bat stop      停止全部
rem     scripts\dev.bat status    查看各端口状态
rem
rem 为什么需要它：
rem   README 推荐的路径是 `make dev-*`，而 Makefile 的配方用了 `sh -c` 与
rem   `. ./scripts/load-env.sh` —— Windows 原生环境通常**没有 sh**，
rem   于是 `make dev-ai` 会以 `make (e=2): 系统找不到指定的文件` 失败。
rem   本脚本把等价命令用纯 cmd 实现，并顺手处理了三件容易踩坑的事：
rem     1. 把 Go 缓存与 Python 依赖固定在仓库内（对应 dev-env.ps1 的作用）；
rem     2. 显式设置 SCID_POSTGRES_DSN / SCID_ARCHIVE_BACKEND 等，
rem        使"没有 Docker"成为一条**正常路径**而不是报错路径；
rem     3. 检查 protobuf 运行时与生成物是否同大版本，不一致就自动补装 ——
rem        这一条不做的话，AI 服务会以 VersionError 直接起不来。
rem ============================================================================

rem 仓库根目录 = 本脚本所在目录的上一级。
for %%I in ("%~dp0..") do set "REPO=%%~fI"
cd /d "%REPO%"

rem ---------------------------------------------------------------------------
rem 可覆盖的外部路径（这些不在仓库里，每台机器可能不同）
rem ---------------------------------------------------------------------------
rem 原生 Redis：仓库不含二进制，用 SCID_REDIS_BIN 指定；未设置时用下面这个默认值。
if not defined SCID_REDIS_BIN set "SCID_REDIS_BIN=D:\itJinYu_toolkit\redis\Redis-x64-5.0.14.1\redis-server.exe"
rem Python 解释器名（conda 环境与 venv 都可用 PYTHON 覆盖）。
if not defined PYTHON set "PYTHON=python"

rem 服务端口。
set "PORT_REDIS=6379"
set "PORT_AI_HTTP=8000"
set "PORT_AI_GRPC=50051"
set "PORT_API=8080"
set "PORT_WEB=5173"

rem 窗口标题前缀：便于 stop 时一眼看出哪些窗口是本脚本拉起的。
set "TITLE_PREFIX=scid-"

rem 主流程必须**先**跳走再去定义子过程：
rem 否则执行流会"掉进"紧跟着的 :setenv 子过程体，撞上它的 goto :eof 而静默退出。
goto :dispatch

rem ===========================================================================
rem 子过程：设置环境变量（对应 . .\scripts\dev-env.ps1）
rem ===========================================================================
:setenv
set "GOPATH=%REPO%\.gocache\gopath"
set "GOMODCACHE=%GOPATH%\pkg\mod"
set "GOCACHE=%REPO%\.gocache\build"
set "GOFLAGS=-mod=mod"
set "GOTELEMETRY=off"
set "PATH=%GOPATH%\bin;%PATH%"

rem .pylibs 放在 PYTHONPATH **末尾**：它可能含有被深度依赖的包（如 typing_extensions），
rem 放前面会遮蔽 conda/venv 里版本更完整的同名包，表现为导入时莫名的
rem "cannot import name"（本项目已踩过一次）。
set "PYTHONPATH=%REPO%\ai;%REPO%\.pylibs"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem 把临时目录也放进仓库内，避免受限环境下写到仓库外被拒绝。
if not exist "%REPO%\.tmp" mkdir "%REPO%\.tmp" >nul 2>&1
set "TEMP=%REPO%\.tmp"
set "TMP=%REPO%\.tmp"

set "SCID_ENV=dev"
if not defined SCID_LOG_LEVEL set "SCID_LOG_LEVEL=info"
set "SCID_REDIS_ADDR=localhost:%PORT_REDIS%"
set "SCID_AI_GRPC_ADDR=localhost:%PORT_AI_GRPC%"
set "SCID_MEDIA_WORK_DIR=%REPO%\.data\work"

rem 以下三项让"不装 Docker"成为正常路径而不是错误路径：
rem   Postgres 留空    -> LangGraph checkpointer 显式降级为 MemorySaver（只告警不崩）
rem   归档设 none      -> 不需要对象存储；要本地留档可改成 local
rem   OTLP 端点留空    -> 链路追踪 no-op（依赖也是惰性导入的）
set "SCID_POSTGRES_DSN="
set "SCID_ARCHIVE_BACKEND=none"
set "SCID_OTEL_ENDPOINT="

rem 没有模型密钥就进 mock 模式，保证零配置能跑通全流程。
if not defined SCID_LLM_PROVIDER set "SCID_LLM_PROVIDER=mock"

rem 本机没有 manim / d3 工具链时，把渲染规格调低能显著加快联调。
if not defined SCID_RENDER_WIDTH set "SCID_RENDER_WIDTH=320"
if not defined SCID_RENDER_HEIGHT set "SCID_RENDER_HEIGHT=240"
if not defined SCID_RENDER_FPS set "SCID_RENDER_FPS=15"
goto :eof

rem ===========================================================================
rem 子过程：辅助
rem ===========================================================================

rem port_busy <端口> —— 设置 BUSY=1/0。
:port_busy
set "BUSY=0"
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /r /c:":%~1 .*LISTENING"') do set "BUSY=1"
goto :eof

rem kill_port <端口> —— 结束占用该端口的进程（含子进程）。
:kill_port
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /r /c:":%~1 .*LISTENING"') do (
    taskkill /F /T /PID %%P >nul 2>&1
)
goto :eof

rem wait_port <端口> <最多秒数> —— 轮询等待端口进入监听。
:wait_port
set /a WAIT_LEFT=%~2
:wait_loop
call :port_busy %~1
if "!BUSY!"=="1" goto :eof
set /a WAIT_LEFT-=1
if !WAIT_LEFT! LEQ 0 goto :eof
rem 用 ping 当 sleep：Windows 没有内置 sleep，timeout 在重定向场景下不可靠。
ping -n 2 127.0.0.1 >nul 2>&1
goto :wait_loop

rem report_port <名称> <端口>
:report_port
call :port_busy %~2
if "!BUSY!"=="1" (
    echo   [运行中] %~1  ^(:%~2^)
) else (
    echo   [未启动] %~1  ^(:%~2^)
)
goto :eof

rem status_ports —— 逐行打印，刻意不用"带引号的列表 + for /f delims=:"：
rem   那种写法会把列表项的引号当成内容，端口号变成 `6379"`，
rem   netstat 永远匹配不上、全部显示"未启动"。
:status_ports
call :report_port "Redis"    %PORT_REDIS%
call :report_port "AI HTTP"  %PORT_AI_HTTP%
call :report_port "AI gRPC"  %PORT_AI_GRPC%
call :report_port "Go 网关"  %PORT_API%
call :report_port "审核台"   %PORT_WEB%
goto :eof

rem probe <名称> <命令> —— 打印一条工具链探测结果（取输出的第一行）。
rem   两个必须注意的点：
rem   1. 必须用 **call**：npm / npx 这类是 .cmd 包装脚本，批处理里不加 call
rem      调用它们会**转移控制权**，外层脚本直接结束 —— 表现为探测到 npm
rem      那一行之后整个脚本莫名退出。
rem   2. 必须同时看**退出码**，不能只判断"有没有输出"：
rem      `docker version` 在守护进程没起时会把错误写到 stderr，
rem      只看输出非空就会被误报成 [OK]（本项目实测踩到）。
:probe
set "PROBE_NAME=%~1"
set "PROBE_OUT="
set "PROBE_RC=0"
call %~2 >"%TEMP%\scid-probe.tmp" 2>&1
set "PROBE_RC=!errorlevel!"
set /p PROBE_OUT=<"%TEMP%\scid-probe.tmp"
if !PROBE_RC! EQU 0 (
    echo   [OK]     %PROBE_NAME%
) else (
    echo   [不可用] %PROBE_NAME%
)
if defined PROBE_OUT echo            !PROBE_OUT!
del "%TEMP%\scid-probe.tmp" >nul 2>&1
goto :eof

rem check_python_deps —— 检查关键 Python 依赖与 protobuf 大版本。
:check_python_deps
%PYTHON% -c "import fastapi, grpc, pydantic, langgraph" >nul 2>&1
if errorlevel 1 (
    echo   [缺失] 核心依赖不齐（fastapi / grpc / pydantic / langgraph）
    echo          修复：%PYTHON% -m pip install -r ai\requirements.txt
) else (
    echo   [OK]   核心依赖：fastapi / grpc / pydantic / langgraph
)
set "PB_VER="
set "PB_MAJOR="
for /f "delims=" %%V in ('%PYTHON% -c "import google.protobuf as p; print(p.__version__)" 2^>nul') do set "PB_VER=%%V"
if defined PB_VER (
    for /f "tokens=1 delims=." %%M in ("!PB_VER!") do set "PB_MAJOR=%%M"
    if !PB_MAJOR! GEQ 6 (
        echo   [OK]   protobuf !PB_VER!
    ) else (
        echo   [偏低] protobuf !PB_VER! —— 生成物要求 6.x，AI 服务会以 VersionError 起不来
        echo          修复：%PYTHON% -m pip install --target .pylibs "protobuf^>=6.33.5,^<7"
    )
)
goto :eof

rem ensure_protobuf —— 大版本不符时自动补装到仓库内的 .pylibs。
rem   装在 .pylibs 而不是全局环境：它在 PYTHONPATH 中排在 site-packages 之前，
rem   既能正确覆盖，又不会污染 conda/venv。
:ensure_protobuf
set "PB_VER="
set "PB_MAJOR="
for /f "delims=" %%V in ('%PYTHON% -c "import google.protobuf as p; print(p.__version__)" 2^>nul') do set "PB_VER=%%V"
if not defined PB_VER goto :eof
for /f "tokens=1 delims=." %%M in ("!PB_VER!") do set "PB_MAJOR=%%M"
if !PB_MAJOR! GEQ 6 goto :eof
echo   [修复] protobuf !PB_VER! 与生成物（6.x）大版本不符，正在补装到 .pylibs ...
%PYTHON% -m pip install --disable-pip-version-check --no-input --target "%REPO%\.pylibs" "protobuf>=6.33.5,<7"
goto :eof

rem ===========================================================================
rem 主流程：子命令分发
rem ===========================================================================
:dispatch
if /i "%~1"=="check"  goto :cmd_check
if /i "%~1"=="build"  goto :cmd_build
if /i "%~1"=="start"  goto :cmd_start
if /i "%~1"=="stop"   goto :cmd_stop
if /i "%~1"=="status" goto :cmd_status
if /i "%~1"=="run"    goto :run_service
if /i "%~1"=="__run"  goto :run_service
goto :usage

rem ---------------------------------------------------------------------------
rem check：工具链与依赖自检
rem ---------------------------------------------------------------------------
:cmd_check
call :setenv
echo.
echo [check] 仓库根目录 : %REPO%
echo.
echo --- 工具链 ---
call :probe go      "go version"
call :probe python  "%PYTHON% --version"
call :probe node    "node --version"
call :probe npm     "npm --version"
call :probe ffmpeg  "ffmpeg -version"
call :probe protoc  "protoc --version"
call :probe docker  "docker version --format {{.Server.Version}}"
echo.
echo --- 外部路径 ---
if exist "%SCID_REDIS_BIN%" (
    echo   [OK]   redis-server : %SCID_REDIS_BIN%
) else (
    echo   [缺失] redis-server : %SCID_REDIS_BIN%
    echo          用 set SCID_REDIS_BIN=^<路径^> 覆盖
)
echo.
echo --- Python 依赖 ---
call :check_python_deps
echo.
echo --- 端口占用 ---
call :status_ports
echo.
exit /b 0

rem ---------------------------------------------------------------------------
rem build：编译 Go 二进制
rem ---------------------------------------------------------------------------
:cmd_build
call :setenv
echo [build] 编译 Go 二进制 ...
if not exist "%REPO%\backend\bin" mkdir "%REPO%\backend\bin" >nul 2>&1
pushd "%REPO%\backend"
go build -o bin\scid-api.exe ./cmd/api
if errorlevel 1 (
    echo [build] api 编译失败
    popd
    exit /b 1
)
go build -o bin\scid-worker.exe ./cmd/worker
if errorlevel 1 (
    echo [build] worker 编译失败
    popd
    exit /b 1
)
popd
echo [build] 完成：backend\bin\scid-api.exe, scid-worker.exe
exit /b 0

rem ---------------------------------------------------------------------------
rem start：拉起全部服务，每个服务一个独立窗口
rem ---------------------------------------------------------------------------
:cmd_start
call :setenv
echo.
echo [start] 仓库根目录 : %REPO%
echo.

rem 1) Redis：整个系统唯一的**硬依赖**。Postgres 与对象存储都可缺省。
if exist "%SCID_REDIS_BIN%" (
    call :port_busy %PORT_REDIS%
    if "!BUSY!"=="1" (
        echo   [跳过] Redis 已在 :%PORT_REDIS% 上运行
    ) else (
        if not exist "%REPO%\.tmp\redis-data" mkdir "%REPO%\.tmp\redis-data" >nul 2>&1
        start "%TITLE_PREFIX%redis" cmd /k ""%SCID_REDIS_BIN%" "%REPO%\.tmp\redis-manual.conf""
        echo   [启动] Redis          :%PORT_REDIS%
    )
) else (
    echo   [警告] 找不到 redis-server：%SCID_REDIS_BIN%
    echo          用 set SCID_REDIS_BIN=^<路径^> 指定，或先启动你自己的 Redis。
)
call :wait_port %PORT_REDIS% 15

rem 关于 start 的写法（实测结论，不要随手改）：
rem   * **不要用 `start "标题" /D "目录" cmd /k ...`** —— 本机实测该形式下新窗口
rem     根本不会被创建（Redis 那行因为没带 /D 所以正常，极具迷惑性）。
rem   * 改用 **pushd 再 start**：子进程会继承父进程的当前目录，
rem     既避开了 /D，也避免了把路径塞进命令字符串带来的嵌套引号问题。
rem   * `/k` 让窗口在服务退出后保留，便于看到报错；否则一闪而过什么都看不到。
rem   * 只有 Redis 那行需要嵌套引号（可执行文件路径可能含空格）。

rem 2) Python 大脑：先确保 protobuf 大版本与生成物一致，否则必然 VersionError。
call :ensure_protobuf
call :port_busy %PORT_AI_HTTP%
if "!BUSY!"=="1" (
    echo   [跳过] AI 服务已在 :%PORT_AI_HTTP% 上运行
) else (
    pushd "%REPO%\ai"
    start "%TITLE_PREFIX%ai" cmd /k "%PYTHON% -m scidirector_ai.main"
    popd
    echo   [启动] AI 大脑        :%PORT_AI_HTTP% (HTTP) / :%PORT_AI_GRPC% (gRPC)
)

rem 3) Go 侧：没有二进制就先编译，避免用户看到"找不到文件"。
if not exist "%REPO%\backend\bin\scid-api.exe"    call :cmd_build
if not exist "%REPO%\backend\bin\scid-worker.exe" call :cmd_build

call :port_busy %PORT_API%
if "!BUSY!"=="1" (
    echo   [跳过] Go 网关已在 :%PORT_API% 上运行
) else (
    pushd "%REPO%\backend"
    start "%TITLE_PREFIX%api" cmd /k "bin\scid-api.exe"
    popd
    echo   [启动] Go 网关        :%PORT_API%
)
pushd "%REPO%\backend"
start "%TITLE_PREFIX%worker" cmd /k "bin\scid-worker.exe"
popd
echo   [启动] worker          ^(无监听端口^)

rem 4) 前端
if exist "%REPO%\web\package.json" (
    if not exist "%REPO%\web\node_modules" (
        echo   [提示] 前端依赖未安装，正在执行 npm install ...
        pushd "%REPO%\web"
        call npm install --no-audit --no-fund
        popd
    )
    call :port_busy %PORT_WEB%
    if "!BUSY!"=="1" (
        echo   [跳过] 前端已在 :%PORT_WEB% 上运行
    ) else (
        pushd "%REPO%\web"
        start "%TITLE_PREFIX%web" cmd /k "npm run dev"
        popd
        echo   [启动] 审核台        :%PORT_WEB%
    )
)

echo.
echo [start] 已下发，等待服务就绪 ...
call :wait_port %PORT_AI_HTTP% 60
call :wait_port %PORT_API% 30
call :wait_port %PORT_WEB% 30
echo.
call :status_ports
echo.
echo   审核台  http://localhost:%PORT_WEB%
echo   网关    http://localhost:%PORT_API%
echo   大脑    http://localhost:%PORT_AI_HTTP%/healthz
echo.
echo   停止：scripts\dev.bat stop
exit /b 0

rem ---------------------------------------------------------------------------
rem stop：停止全部
rem ---------------------------------------------------------------------------
:cmd_stop
call :setenv
echo.
echo [stop] 停止 SciDirector 各服务 ...
call :kill_port %PORT_WEB%
call :kill_port %PORT_API%
call :kill_port %PORT_AI_HTTP%
call :kill_port %PORT_AI_GRPC%

rem worker 没有监听端口，按镜像名结束（它是本脚本拉起的独立 exe）。
taskkill /F /T /IM scid-worker.exe >nul 2>&1
if not errorlevel 1 echo   [停止] worker

rem Redis 也按端口结束 —— 注意这会连带停掉本机 6379 上的任何 Redis 实例。
call :port_busy %PORT_REDIS%
if "!BUSY!"=="1" (
    call :kill_port %PORT_REDIS%
    echo   [停止] Redis ^(:%PORT_REDIS%^)
)
echo [stop] 完成
exit /b 0

rem ---------------------------------------------------------------------------
rem status：端口状态
rem ---------------------------------------------------------------------------
:cmd_status
call :setenv
echo.
call :status_ports
exit /b 0

rem ===========================================================================
rem run <服务名>：在前台运行**单个**服务
rem   用途一：不想让 start 弹窗口时，开 N 个终端各跑一个（这条路径与 start
rem          完全等价，只是窗口由你自己管理）。
rem   用途二：某个服务起不来时，单独跑它就能直接看到完整报错，不用去翻窗口。
rem
rem   参数：redis | ai | api | worker | web
rem ===========================================================================
:run_service
call :setenv
if /i "%~2"=="redis"  goto :run_redis
if /i "%~2"=="ai"     goto :run_ai
if /i "%~2"=="api"    goto :run_api
if /i "%~2"=="worker" goto :run_worker
if /i "%~2"=="web"    goto :run_web
echo 未知服务：%~2
echo 可选：redis / ai / api / worker / web
exit /b 1

:run_redis
title %TITLE_PREFIX%redis
"%SCID_REDIS_BIN%" "%REPO%\.tmp\redis-manual.conf"
goto :run_end

:run_ai
title %TITLE_PREFIX%ai
cd /d "%REPO%\ai"
echo [ai] LLM=%SCID_LLM_PROVIDER%
echo [ai] Postgres DSN=[%SCID_POSTGRES_DSN%]  ^(空 = 内存 checkpointer 降级^)
%PYTHON% -m scidirector_ai.main
goto :run_end

:run_api
title %TITLE_PREFIX%api
cd /d "%REPO%\backend"
"%REPO%\backend\bin\scid-api.exe"
goto :run_end

:run_worker
title %TITLE_PREFIX%worker
cd /d "%REPO%\backend"
"%REPO%\backend\bin\scid-worker.exe"
goto :run_end

:run_web
title %TITLE_PREFIX%web
cd /d "%REPO%\web"
call npm run dev
goto :run_end

:run_end
echo.
echo [服务已退出] 按任意键关闭窗口 ...
pause >nul
exit /b 0

rem ===========================================================================
rem 用法
rem ===========================================================================
:usage
echo.
echo SciDirector —— Windows 原生启动脚本
echo.
echo   scripts\dev.bat check     环境自检
echo   scripts\dev.bat build     构建 Go 的 api / worker
echo   scripts\dev.bat start     启动全部服务（每个服务一个窗口）
echo   scripts\dev.bat stop      停止全部服务
echo   scripts\dev.bat status    查看端口状态
echo   scripts\dev.bat run ^<服务^>   在前台单独运行一个服务
echo                            （redis / ai / api / worker / web）
echo.
echo 可覆盖的环境变量：
echo   SCID_REDIS_BIN     原生 redis-server.exe 路径
echo   PYTHON             Python 解释器（conda / venv 均可）
echo   SCID_LLM_PROVIDER  模型服务商；未设置则为 mock 模式
echo.
exit /b 1
