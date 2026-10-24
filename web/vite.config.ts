import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

// 产物直接落到后端的静态目录，由同一个 Python 服务托管 —— 单一端口、单一域名、无跨域
export default defineConfig({
  plugins: [vue()],
  base: "/",
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "../server/static",
    emptyOutDir: false,
    assetsDir: "app-assets",
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:3111" },
  },
});
