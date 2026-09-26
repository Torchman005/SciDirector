@echo off
rem ============================================================================
rem SciDirector - Windows native launcher (no Docker / make / sh required)
rem ----------------------------------------------------------------------------
rem USAGE
rem   scripts\dev.bat check           environment self-check
rem   scripts\dev.bat build           build Go api / worker
rem   scripts\dev.bat start           start everything (one window per service)
rem   scripts\dev.bat stop            stop everything
rem   scripts\dev.bat status          show port status
rem   scripts\dev.bat run <service>   run ONE service in the foreground
rem                                   (redis / ai / api / worker / web)
rem
rem WHY THIS FILE IS PURE ASCII
rem   Batch parsing depends on the console code page. Chinese text inside a
rem   .bat breaks in several ways, and this project hit all of them:
rem     * cmd reads UTF-8 bytes as GBK, produces stray characters, and reports
rem       errors that point at completely unrelated characters;
rem     * "chcp 65001" at the top is NOT reliable - it does not always take
rem       effect before cmd has already parsed the following lines. Putting the
rem       explanatory comment ABOVE chcp made the comment itself the bug;
rem     * LF-only line endings make cmd swallow the first character of lines.
rem   None of these can be fixed by reordering. Keeping the file ASCII removes
rem   the whole class of problems: it parses identically on every console,
rem   locale and code page.
rem
rem   Chinese documentation lives in docs\WINDOWS.md (UTF-8 is safe there).
rem   .gitattributes still forces CRLF for *.bat - that part IS required.
rem
rem WHY IT EXISTS AT ALL
rem   README recommends "make dev-*", but the Makefile recipes use "sh -c" and
rem   ". ./scripts/load-env.sh". A native Windows environment has no sh, so
rem   make fails with: make (e=2): The system cannot find the file specified.
rem   This script implements the same commands in pure cmd, and also handles:
rem     1. pinning Go cache / Python deps inside the repo (like dev-env.ps1);
rem     2. loading the root .env (Windows had no equivalent of load-env.sh);
rem     3. resolving work dirs to ABSOLUTE paths, so artifacts do not split
rem        into two trees depending on the current directory;
rem     4. making "no Docker" a normal path instead of an error path;
rem     5. checking the protobuf major version - without it the AI service
rem        fails to start with a VersionError.
rem ============================================================================

setlocal EnableExtensions EnableDelayedExpansion

rem Repo root = parent of this script's directory.
for %%I in ("%~dp0..") do set "REPO=%%~fI"
cd /d "%REPO%"

rem ---------------------------------------------------------------------------
rem Detect "launched by double-click" so we can pause before exiting.
rem   Explorer starts the script as: cmd /c ""<path>\dev.bat" "
rem   so the script name appears in %cmdcmdline%. When the user runs it from an
rem   already-open cmd window, %cmdcmdline% is the shell's own command line and
rem   does NOT contain the script name - so no unnecessary keypress there.
rem ---------------------------------------------------------------------------
set "SCID_PAUSE_ON_EXIT="
echo %cmdcmdline% | find /i "%~nx0" >nul 2>&1
if not errorlevel 1 set "SCID_PAUSE_ON_EXIT=1"

rem ---------------------------------------------------------------------------
rem Overridable external paths (not in the repo; differ per machine)
rem ---------------------------------------------------------------------------
if not defined SCID_REDIS_BIN set "SCID_REDIS_BIN=D:\itJinYu_toolkit\redis\Redis-x64-5.0.14.1\redis-server.exe"
if not defined PYTHON set "PYTHON=python"

set "PORT_REDIS=6379"
set "PORT_AI_HTTP=8000"
set "PORT_AI_GRPC=50051"
set "PORT_API=8080"
set "PORT_WEB=5173"

set "TITLE_PREFIX=scid-"

goto :dispatch

rem ===========================================================================
rem SUBROUTINES
rem ===========================================================================

rem setenv - export every variable a service needs.
rem   Order matters: load .env FIRST, then fill defaults, so that .env and
rem   explicit environment variables always win over the defaults below.
:setenv
set "GOPATH=%REPO%\.gocache\gopath"
set "GOMODCACHE=%GOPATH%\pkg\mod"
set "GOCACHE=%REPO%\.gocache\build"
set "GOFLAGS=-mod=mod"
set "GOTELEMETRY=off"
set "PATH=%GOPATH%\bin;%PATH%"

rem .pylibs goes LAST on PYTHONPATH: it may contain transitively-required
rem packages (e.g. typing_extensions) whose fuller copies live in conda/venv.
rem Putting it first shadows them and produces a mysterious ImportError.
set "PYTHONPATH=%REPO%\ai;%REPO%\.pylibs"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "%REPO%\.tmp" mkdir "%REPO%\.tmp" >nul 2>&1
set "TEMP=%REPO%\.tmp"
set "TMP=%REPO%\.tmp"

call :load_env

if not defined SCID_ENV set "SCID_ENV=dev"
if not defined SCID_LOG_LEVEL set "SCID_LOG_LEVEL=info"
if not defined SCID_REDIS_ADDR set "SCID_REDIS_ADDR=localhost:%PORT_REDIS%"
if not defined SCID_AI_GRPC_ADDR set "SCID_AI_GRPC_ADDR=localhost:%PORT_AI_GRPC%"

rem These three make "no Docker" a normal path:
rem   empty Postgres DSN -> LangGraph checkpointer degrades to memory (warns)
rem   archive=none       -> no object storage needed (use "local" to keep files)
rem   empty OTLP         -> tracing becomes a no-op (deps imported lazily)
if not defined SCID_POSTGRES_DSN set "SCID_POSTGRES_DSN="
if not defined SCID_ARCHIVE_BACKEND set "SCID_ARCHIVE_BACKEND=none"
if not defined SCID_OTEL_ENDPOINT set "SCID_OTEL_ENDPOINT="

if not defined SCID_LLM_PROVIDER set "SCID_LLM_PROVIDER=mock"

rem Work dirs must be absolute. Python's sandbox_work_dir defaults to a
rem RELATIVE path and follows the process cwd - running from ai\ would write
rem to ai\.data\sandbox instead of .data\sandbox, splitting artifacts into two
rem trees. Explicit values are still respected; only relative ones are rooted.
call :abs_path SCID_MEDIA_WORK_DIR
call :abs_path SCID_SANDBOX_WORK_DIR
call :abs_path SCID_ARCHIVE_LOCAL_DIR
if not defined SCID_MEDIA_WORK_DIR set "SCID_MEDIA_WORK_DIR=%REPO%\.data\work"
if not defined SCID_SANDBOX_WORK_DIR set "SCID_SANDBOX_WORK_DIR=%REPO%\.data\sandbox"

rem Lower render specs speed up local iteration (no manim/d3 on this machine).
if not defined SCID_RENDER_WIDTH set "SCID_RENDER_WIDTH=320"
if not defined SCID_RENDER_HEIGHT set "SCID_RENDER_HEIGHT=240"
if not defined SCID_RENDER_FPS set "SCID_RENDER_FPS=15"
goto :eof

rem load_env - load the repo-root .env.
rem   Parsing is delegated to scripts\load-env.ps1 so that there is exactly ONE
rem   implementation of the .env rules (the POSIX side uses load-env.sh with the
rem   same semantics). This loop just executes the "set" lines it prints.
:load_env
if not exist "%REPO%\.env" goto :eof
for /f "usebackq delims=" %%L in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\load-env.ps1" -Format cmd`) do %%L
goto :eof

rem abs_path <VARNAME> - root a relative path at the repo; keep absolute ones.
:abs_path
call set "AP_VAL=%%%~1%%"
if not defined AP_VAL goto :eof
set "AP_REL=%AP_VAL:/=\%"
if "%AP_REL:~1,1%"==":" (
    set "%~1=%AP_REL%"
    goto :eof
)
if "%AP_REL:~0,2%"=="\\" (
    set "%~1=%AP_REL%"
    goto :eof
)
if "%AP_REL:~0,2%"==".\" set "AP_REL=%AP_REL:~2%"
set "%~1=%REPO%\%AP_REL%"
goto :eof

rem maybe_pause - hold the window open only when launched by double-click.
:maybe_pause
if defined SCID_PAUSE_ON_EXIT (
    echo.
    echo Press any key to close this window . . .
    pause >nul
)
goto :eof

rem port_busy <PORT> - sets BUSY=1/0
:port_busy
set "BUSY=0"
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /r /c:":%~1 .*LISTENING"') do set "BUSY=1"
goto :eof

rem kill_port <PORT> - terminate whatever listens on that port (with children)
:kill_port
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /r /c:":%~1 .*LISTENING"') do (
    taskkill /F /T /PID %%P >nul 2>&1
)
goto :eof

rem wait_port <PORT> <SECONDS>
:wait_port
set /a WAIT_LEFT=%~2
:wait_loop
call :port_busy %~1
if "!BUSY!"=="1" goto :eof
set /a WAIT_LEFT-=1
if !WAIT_LEFT! LEQ 0 goto :eof
rem ping is used as a portable sleep; timeout.exe misbehaves when redirected.
ping -n 2 127.0.0.1 >nul 2>&1
goto :wait_loop

rem report_port <LABEL> <PORT>
:report_port
call :port_busy %~2
if "!BUSY!"=="1" (
    echo   [UP]      %~1  ^(port %~2^)
) else (
    echo   [DOWN]    %~1  ^(port %~2^)
)
goto :eof

:status_ports
call :report_port "Redis"    %PORT_REDIS%
call :report_port "AI HTTP"  %PORT_AI_HTTP%
call :report_port "AI gRPC"  %PORT_AI_GRPC%
call :report_port "Go API"   %PORT_API%
call :report_port "Web UI"   %PORT_WEB%
goto :eof

rem probe <LABEL> <COMMAND> - print first output line AND the exit code.
rem   The exit code matters: "docker version" writes to stderr when the daemon
rem   is not running, so checking output alone would report a false [OK].
:probe
set "PROBE_NAME=%~1"
set "PROBE_OUT="
set "PROBE_RC=0"
call %~2 >"%TEMP%\scid-probe.tmp" 2>&1
set "PROBE_RC=!errorlevel!"
set /p PROBE_OUT=<"%TEMP%\scid-probe.tmp"
if !PROBE_RC! EQU 0 (
    echo   [OK]      %PROBE_NAME%
) else (
    echo   [NOT OK]  %PROBE_NAME%
)
if defined PROBE_OUT echo             !PROBE_OUT!
del "%TEMP%\scid-probe.tmp" >nul 2>&1
goto :eof

rem check_python_deps - core imports + protobuf major version
:check_python_deps
%PYTHON% -c "import fastapi, grpc, pydantic, langgraph" >nul 2>&1
if errorlevel 1 (
    echo   [MISSING] core deps: fastapi / grpc / pydantic / langgraph
    echo             fix: %PYTHON% -m pip install -r ai\requirements.txt
) else (
    echo   [OK]      core deps: fastapi / grpc / pydantic / langgraph
)
set "PB_VER="
set "PB_MAJOR="
for /f "delims=" %%V in ('%PYTHON% -c "import google.protobuf as p; print(p.__version__)" 2^>nul') do set "PB_VER=%%V"
if defined PB_VER (
    for /f "tokens=1 delims=." %%M in ("!PB_VER!") do set "PB_MAJOR=%%M"
    if !PB_MAJOR! GEQ 6 (
        echo   [OK]      protobuf !PB_VER!
    ) else (
        echo   [TOO OLD] protobuf !PB_VER! - generated code needs 6.x
        echo             the AI service will fail with VersionError
        echo             fix: %PYTHON% -m pip install --target .pylibs "protobuf>=6.33.5,<7"
    )
)
goto :eof

rem ensure_protobuf - install a matching protobuf into the repo-local .pylibs
:ensure_protobuf
set "PB_VER="
set "PB_MAJOR="
for /f "delims=" %%V in ('%PYTHON% -c "import google.protobuf as p; print(p.__version__)" 2^>nul') do set "PB_VER=%%V"
if not defined PB_VER goto :eof
for /f "tokens=1 delims=." %%M in ("!PB_VER!") do set "PB_MAJOR=%%M"
if !PB_MAJOR! GEQ 6 goto :eof
echo   [FIX] protobuf !PB_VER! does not match generated code (6.x); installing into .pylibs ...
%PYTHON% -m pip install --disable-pip-version-check --no-input --target "%REPO%\.pylibs" "protobuf>=6.33.5,<7"
goto :eof

rem ===========================================================================
rem COMMAND DISPATCH
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
:cmd_check
call :setenv
echo.
echo [check] repo root: %REPO%
echo.
echo --- toolchain ---
call :probe go      "go version"
call :probe python  "%PYTHON% --version"
call :probe node    "node --version"
call :probe npm     "npm --version"
call :probe ffmpeg  "ffmpeg -version"
call :probe protoc  "protoc --version"
call :probe docker  "docker version --format {{.Server.Version}}"
echo.
echo --- external paths ---
if exist "%SCID_REDIS_BIN%" (
    echo   [OK]      redis-server : %SCID_REDIS_BIN%
) else (
    echo   [MISSING] redis-server : %SCID_REDIS_BIN%
    echo             override with: set SCID_REDIS_BIN=^<path^>
)
echo.
echo --- resolved configuration ---
if exist "%REPO%\.env" (
    echo   .env             : loaded ^(explicit environment wins^)
) else (
    echo   .env             : absent - provider falls back to mock
)
echo   LLM provider     : %SCID_LLM_PROVIDER%
if defined SCID_POSTGRES_DSN (
    echo   Postgres DSN     : %SCID_POSTGRES_DSN%
) else (
    echo   Postgres DSN     : empty ^(checkpointer degrades to memory^)
)
echo   Archive backend  : %SCID_ARCHIVE_BACKEND%
echo   Media work dir   : %SCID_MEDIA_WORK_DIR%
echo   Sandbox work dir : %SCID_SANDBOX_WORK_DIR%
echo.
echo --- python deps ---
call :check_python_deps
echo.
echo --- ports ---
call :status_ports
echo.
call :maybe_pause
exit /b 0

rem ---------------------------------------------------------------------------
:cmd_build
call :setenv
echo [build] compiling Go binaries ...
if not exist "%REPO%\backend\bin" mkdir "%REPO%\backend\bin" >nul 2>&1
pushd "%REPO%\backend"
go build -o bin\scid-api.exe ./cmd/api
if errorlevel 1 (
    echo [build] api build FAILED
    popd
    call :maybe_pause
    exit /b 1
)
go build -o bin\scid-worker.exe ./cmd/worker
if errorlevel 1 (
    echo [build] worker build FAILED
    popd
    call :maybe_pause
    exit /b 1
)
popd
echo [build] done: backend\bin\scid-api.exe, scid-worker.exe
call :maybe_pause
exit /b 0

rem ---------------------------------------------------------------------------
:cmd_start
call :setenv
echo.
echo [start] repo root: %REPO%
echo.

rem NOTE ON "start": do NOT use  start "title" /D "dir" cmd /k ...
rem That form silently fails to create the window on this machine - while the
rem Redis line, which has no /D, worked. That asymmetry made it very confusing
rem to diagnose. Use "pushd then start" instead: the child inherits the parent's
rem current directory, which also avoids nested quoting. /k keeps the window
rem open so failures stay visible.

rem 1) Redis - the only hard dependency of the whole system.
if exist "%SCID_REDIS_BIN%" (
    call :port_busy %PORT_REDIS%
    if "!BUSY!"=="1" (
        echo   [skip]   Redis already running on %PORT_REDIS%
    ) else (
        if not exist "%REPO%\.tmp\redis-data" mkdir "%REPO%\.tmp\redis-data" >nul 2>&1
        start "%TITLE_PREFIX%redis" cmd /k ""%SCID_REDIS_BIN%" "%REPO%\.tmp\redis-manual.conf""
        echo   [start]  Redis          port %PORT_REDIS%
    )
) else (
    echo   [WARN]   redis-server not found: %SCID_REDIS_BIN%
    echo            set SCID_REDIS_BIN=^<path^>, or start your own Redis first.
)
call :wait_port %PORT_REDIS% 15

rem 2) Python brain - make protobuf match the generated code first.
call :ensure_protobuf
call :port_busy %PORT_AI_HTTP%
if "!BUSY!"=="1" (
    echo   [skip]   AI service already running on %PORT_AI_HTTP%
) else (
    pushd "%REPO%\ai"
    start "%TITLE_PREFIX%ai" cmd /k "%PYTHON% -m scidirector_ai.main"
    popd
    echo   [start]  AI brain       %PORT_AI_HTTP% HTTP / %PORT_AI_GRPC% gRPC
)

rem 3) Go side - build first if needed, so users never see "file not found".
if not exist "%REPO%\backend\bin\scid-api.exe"    call :cmd_build
if not exist "%REPO%\backend\bin\scid-worker.exe" call :cmd_build

call :port_busy %PORT_API%
if "!BUSY!"=="1" (
    echo   [skip]   Go API already running on %PORT_API%
) else (
    pushd "%REPO%\backend"
    start "%TITLE_PREFIX%api" cmd /k "bin\scid-api.exe"
    popd
    echo   [start]  Go API         port %PORT_API%
)
pushd "%REPO%\backend"
start "%TITLE_PREFIX%worker" cmd /k "bin\scid-worker.exe"
popd
echo   [start]  worker         no listening port

rem 4) Frontend
if exist "%REPO%\web\package.json" (
    if not exist "%REPO%\web\node_modules" (
        echo   [note]   frontend deps missing, running npm install ...
        pushd "%REPO%\web"
        call npm install --no-audit --no-fund
        popd
    )
    call :port_busy %PORT_WEB%
    if "!BUSY!"=="1" (
        echo   [skip]   Web UI already running on %PORT_WEB%
    ) else (
        pushd "%REPO%\web"
        start "%TITLE_PREFIX%web" cmd /k "npm run dev"
        popd
        echo   [start]  Web UI         port %PORT_WEB%
    )
)

echo.
echo [start] dispatched, waiting for services ...
call :wait_port %PORT_AI_HTTP% 60
call :wait_port %PORT_API% 30
call :wait_port %PORT_WEB% 30
echo.
call :status_ports
echo.
echo   Web UI   http://localhost:%PORT_WEB%
echo   API      http://localhost:%PORT_API%
echo   AI brain http://localhost:%PORT_AI_HTTP%/healthz
echo.
echo   To stop: scripts\dev.bat stop
call :maybe_pause
exit /b 0

rem ---------------------------------------------------------------------------
:cmd_stop
call :setenv
echo.
echo [stop] stopping SciDirector services ...
call :kill_port %PORT_WEB%
call :kill_port %PORT_API%
call :kill_port %PORT_AI_HTTP%
call :kill_port %PORT_AI_GRPC%

rem worker has no listening port; kill by image name (it is our own exe).
taskkill /F /T /IM scid-worker.exe >nul 2>&1
if not errorlevel 1 echo   [stop]   worker

rem Redis is also killed by port - note this stops any Redis on that port.
call :port_busy %PORT_REDIS%
if "!BUSY!"=="1" (
    call :kill_port %PORT_REDIS%
    echo   [stop]   Redis ^(port %PORT_REDIS%^)
)
echo [stop] done
call :maybe_pause
exit /b 0

rem ---------------------------------------------------------------------------
:cmd_status
call :setenv
echo.
call :status_ports
call :maybe_pause
exit /b 0

rem ===========================================================================
rem run <service> - run ONE service in the foreground.
rem   Use this when you prefer N terminals over dev.bat start's auto windows,
rem   or when a single service fails and you want its full error output.
rem ===========================================================================
:run_service
call :setenv
if /i "%~2"=="redis"  goto :run_redis
if /i "%~2"=="ai"     goto :run_ai
if /i "%~2"=="api"    goto :run_api
if /i "%~2"=="worker" goto :run_worker
if /i "%~2"=="web"    goto :run_web
echo Unknown service: %~2
echo Valid: redis / ai / api / worker / web
call :maybe_pause
exit /b 1

:run_redis
title %TITLE_PREFIX%redis
"%SCID_REDIS_BIN%" "%REPO%\.tmp\redis-manual.conf"
goto :run_end

:run_ai
title %TITLE_PREFIX%ai
cd /d "%REPO%\ai"
echo [ai] LLM=%SCID_LLM_PROVIDER%
echo [ai] Postgres DSN=[%SCID_POSTGRES_DSN%]  empty means in-memory checkpointer
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
echo [service exited] press any key to close . . .
pause >nul
exit /b 0

rem ===========================================================================
:usage
echo.
echo SciDirector - Windows native launcher
echo.
echo   scripts\dev.bat check          environment self-check
echo   scripts\dev.bat build          build Go api / worker
echo   scripts\dev.bat start          start all services (one window each)
echo   scripts\dev.bat stop           stop all services
echo   scripts\dev.bat status         show port status
echo   scripts\dev.bat run ^<service^>   run one service in the foreground
echo                                  redis / ai / api / worker / web
echo.
echo Environment variables you can override:
echo   SCID_REDIS_BIN     path to a native redis-server.exe
echo   PYTHON             python interpreter (conda / venv)
echo   SCID_LLM_PROVIDER  model provider; unset means mock mode
echo.
echo Chinese notes: docs\WINDOWS.md
echo.
call :maybe_pause
exit /b 1
