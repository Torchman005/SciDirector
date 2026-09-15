import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 配置。
// 通过环境变量注入后端地址，使同一份产物可以部署到不同环境，
// 而不需要在构建后手工改写 bundle 里的字符串。
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    // 开发期代理：把 /api 与 /ws 转发到 Go 网关，
    // 这样前端代码里可以直接写相对路径，天然避免跨域问题。
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE ?? 'http://localhost:8080',
        changeOrigin: true,
      },
      '/ws': {
        target: process.env.VITE_WS_BASE ?? 'ws://localhost:8080',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
