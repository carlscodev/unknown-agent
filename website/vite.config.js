import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: { outDir: "dist", sourcemap: true },
  server: { host: "0.0.0.0", allowedHosts: true },
});
