# =============================================================================
# SciDirector 前端镜像：多阶段构建（Node 构建 -> Nginx 托管静态资源）
# -----------------------------------------------------------------------------
# 为什么用 Nginx 而不是 `vite preview`：
#   preview 是开发工具，单线程、无 gzip/缓存控制，仅用于本地验证；
#   生产必须用支持并发与长连接代理的静态服务器。
# =============================================================================

FROM node:22-alpine AS builder

WORKDIR /app

# 先拷依赖清单再安装：让依赖层在源码变动时命中缓存。
COPY web/package.json web/package-lock.json* ./
# 有 lockfile 用 npm ci（可复现），没有则退回 npm install（首次搭建场景）。
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY web/ ./

# 构建期注入后端地址：产物中的 API 基址在构建时确定。
ARG VITE_API_BASE=http://localhost:8080
ARG VITE_WS_BASE=ws://localhost:8080
ENV VITE_API_BASE=${VITE_API_BASE} \
    VITE_WS_BASE=${VITE_WS_BASE}
RUN npm run build

# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
