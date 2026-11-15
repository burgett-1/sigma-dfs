import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import path from "node:path";

// In `npm run dev`, serve ../data (written by the Python pipeline) at /data.
function serveData() {
  const dataDir = path.resolve(__dirname, "../data");
  return {
    name: "serve-data",
    configureServer(server) {
      server.middlewares.use("/data", (req, res, next) => {
        const file = path.join(dataDir, decodeURIComponent(req.url.split("?")[0]));
        if (!file.startsWith(dataDir) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return next();
        res.setHeader("Cache-Control", "no-store");
        fs.createReadStream(file).pipe(res);
      });
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), serveData()],
});
