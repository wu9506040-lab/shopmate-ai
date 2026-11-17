import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Vite 配置：开发代理复刻 nginx 分流规则（deploy/frontend/nginx.conf）
// 三部分架构：会话/鉴权/管理台路由 → Java 中台(8081)；其余 /api → Python AI(8000)
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // ---- Java 中台承载的路径（与 nginx.conf 一致，长前缀在前）----
      '/api/chat': {
        target: 'http://127.0.0.1:8081',
        changeOrigin: true,
      },
      '/api/auth': {
        target: 'http://127.0.0.1:8081',
        changeOrigin: true,
      },
      '/api/conversations': {
        target: 'http://127.0.0.1:8081',
        changeOrigin: true,
      },
      '/api/admin/handoff': {
        target: 'http://127.0.0.1:8081',
        changeOrigin: true,
      },
      '/api/admin/analytics/board': {
        target: 'http://127.0.0.1:8081',
        changeOrigin: true,
      },
      // ---- 兜底：其余路径走 Python AI 服务 ----
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});