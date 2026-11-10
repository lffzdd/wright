import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const rootDir = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: resolve(rootDir, "../src/wright/interfaces/web/static"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Vendor code changes rarely; keeping it apart means a rebuild only
        // rewrites the small app chunk in the committed static bundle.
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react";
          if (/node_modules\/(marked|dompurify)\//.test(id)) return "markdown";
          if (id.includes("@phosphor-icons")) return "icons";
          if (id.endsWith(".css")) return "vendor";
          // Anything else (e.g. code highlighting) is left to Rollup, so lazily
          // imported libraries stay out of the first load.
          return undefined;
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
