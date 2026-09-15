# =============================================================================
# SciDirector AI 大脑（Python）镜像
# -----------------------------------------------------------------------------
# 体积换能力：Manim 需要 LaTeX，D3/ECharts 渲染需要 headless 浏览器。
# 这两个依赖让镜像达到数 GB，但它们正是本项目「科学严谨性」的来源，
# 因此通过构建参数 RENDER_EXTRAS 允许在纯规划/审查场景下裁剪。
#
# 构建：
#   docker build -f ai/Dockerfile -t scid-ai .
# =============================================================================
FROM python:3.11-slim AS base

# ---- 系统依赖 ----
# 说明每一项为什么需要：
#   ffmpeg      : 抽帧（供 VLM 审查）与产物校验
#   texlive-*   : Manim 渲染公式，LaTeX 是 MathTex 的硬依赖
#   libpango/cairo : Manim 的文字排版
#   fonts-noto-cjk : 中文字形，缺失会让所有中文变成方块
#   libgl1/libglib2.0 : Manim/OpenGL 相关运行时
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        pkg-config \
        libcairo2-dev \
        libpango1.0-dev \
        libgl1 \
        libglib2.0-0 \
        fonts-noto-cjk \
        fonts-noto-mono \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# LaTeX：单独一层，便于在不改 Python 代码时复用缓存。
# 用 texlive 的 medium 集合（含 amsmath/latex-base），full 会让镜像再大好几 GB。
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-extra \
        texlive-fonts-recommended \
        texlive-science \
        dvisvgm \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- Python 依赖 ----
# 先拷 requirements 再拷源码：让依赖层在源码变动时仍然命中缓存。
COPY ai/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# Playwright 浏览器（D3/ECharts 录帧用）。
# --with-deps 会顺带装系统库；这里显式安装 chromium 即可满足 HTML 录帧需求。
ARG INSTALL_BROWSER=1
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        pip install playwright && playwright install --with-deps chromium ; \
    else \
        echo "[build] 跳过 Playwright 浏览器安装" ; \
    fi

# ---- 应用代码 ----
COPY ai/ /app/

# 预编译 Manim 的 LaTeX 前言：首次渲染的 TeX 编译极慢（几十秒），
# 预热后可以显著降低第一个数学镜头的时延。
RUN python -c "import manim" 2>/dev/null || echo "[build] manim 未安装，跳过预热"

# 非 root 运行：沙盒执行的代码即使逃逸，也不该直接拿到 root。
RUN useradd --create-home --uid 10001 scid \
    && mkdir -p /data/work /data/sandbox \
    && chown -R scid:scid /app /data
USER scid

ENV SCID_MEDIA_WORK_DIR=/data/work \
    SCID_SANDBOX_WORK_DIR=/data/sandbox \
    SCID_AI_HTTP_HOST=0.0.0.0 \
    SCID_AI_HTTP_PORT=8000 \
    SCID_AI_GRPC_HOST=0.0.0.0 \
    SCID_AI_GRPC_PORT=50051

EXPOSE 8000 50051

# 容器内健康检查用 Python 自带 urllib，避免为了探针再装 curl 依赖。
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "scidirector_ai.main"]
