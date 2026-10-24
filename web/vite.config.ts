import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 产物直接落到后端的静态目录，由同一个 Python 服务托管 —— 单一端口、单一域名、无跨域。
// 刻意不配 alias：本项目所有引用都是相对路径，多一层路径映射只会让「文件在哪」变模糊。
export default defineConfig({
  plugins: [vue()],
  base: "/",
  build: {
    outDir: "../server/static",
    emptyOutDir: false,
    assetsDir: "app-assets",
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:3111" },
  },
});
