// Ada marketing site: one static page, built by Vite into dist/.
//
// base "./" is a deliberate deviation from Vite's GitHub Pages guide, which
// says '/<REPO>/' (vitejs/vite docs/guide/static-deploy.md at 57fea00). A
// relative base lets one bundle serve both hosts, GitHub Pages under
// /ada-hardware/ and CloudFront at /. It works because the site is one
// index.html with hash anchors and no client routing.
//
// The images stay in site/assets/ (their home since before the rebuild, and
// where app/src-tauri/icons/gen_app_icon.py writes icon.png). The page imports
// them, so the build hashes them. og:image must be a stable absolute URL, so
// the one plugin below also copies assets/board.png to dist/og/board.png.
import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

function ogImage(): Plugin {
  return {
    name: "ada-og-image",
    apply: "build",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "og/board.png",
        source: readFileSync(new URL("./assets/board.png", import.meta.url)),
      });
    },
  };
}

export default defineConfig({
  base: "./",
  publicDir: false,
  plugins: [react(), tailwindcss(), ogImage()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: { outDir: "dist" },
});
