# =============================================================================
# SciDirector Go 层镜像（api 与 worker 共用）
# -----------------------------------------------------------------------------
# 用同一个 Dockerfile 构建两个可执行文件，通过构建参数 TARGET 选择产物，
# 这样依赖层只有一份缓存，且两侧不可能出现「依赖版本不一致」的问题。
#
# 构建：
#   docker build -f backend/Dockerfile --build-arg TARGET=api    -t scid-api .
#   docker build -f backend/Dockerfile --build-arg TARGET=worker -t scid-worker .
# =============================================================================

# ---------------------------------------------------------------------------
# 构建阶段
# ---------------------------------------------------------------------------
FROM golang:1.23-alpine AS builder

# 国内构建加速；如需直连可覆盖此参数。
ARG GOPROXY=https://goproxy.cn,direct
ENV GOPROXY=${GOPROXY} \
    CGO_ENABLED=0 \
    GOOS=linux

WORKDIR /src

# 先拷依赖描述并下载：让依赖层在源码变动时仍然命中缓存。
COPY backend/go.mod backend/go.sum ./backend/
RUN cd backend && go mod download

COPY backend/ ./backend/

ARG VERSION=0.1.0
# 同时构建两个二进制。它们共享 99% 的依赖与包，因此增量编译第二个几乎不花时间，
# 却换来「同一个镜像可以当 api 也可以当 worker」的部署灵活性。
RUN cd backend \
    && go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/scid-api    ./cmd/api \
    && go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/scid-worker ./cmd/worker

# ---------------------------------------------------------------------------
# 运行阶段
# ---------------------------------------------------------------------------
FROM alpine:3.20

# ca-certificates : 访问 HTTPS 上游（对象存储等）
# ffmpeg          : 媒体处理的核心外部依赖
# tzdata          : 时间戳本地化
RUN apk add --no-cache ca-certificates ffmpeg tzdata \
    && adduser -D -u 10001 -h /app scid \
    && mkdir -p /data/work \
    && chown -R scid:scid /app /data

WORKDIR /app

COPY --from=builder /out/scid-api    /app/scid-api
COPY --from=builder /out/scid-worker /app/scid-worker

ENV SCID_MEDIA_WORK_DIR=/data/work \
    SCID_ENV=prod

USER scid

# 默认跑 api；worker 由 compose 的 command 覆盖。
# 两个二进制放在同一镜像里而不是各建一个：镜像层几乎完全共享，
# 拉取体积更小，也避免运维拿错 tag 把 worker 当 api 起。
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/scid-api", "version"]

CMD ["/app/scid-api"]
