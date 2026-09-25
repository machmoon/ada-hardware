import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import tailwindcss from "@tailwindcss/vite";

const host = process.env.TAURI_DEV_HOST;

/** The sentence a refused build ends with. `src/config/hardening.test.ts` pins it. */
export const TEST_STORE_REFUSAL =
  "Refusing to build a store bundle with a RevenueCat Test Store key; set ADA_ALLOW_TEST_STORE=1 for a demo build.";

/**
 * A RevenueCat Test Store key (`test_…`) simulates every purchase, so a store
 * bundle carrying one would sell nothing and unlock everything. RevenueCat's
 * own rule (docs, Test Store: never submit a build configured with a Test
 * Store key) is applied here at the one point every production build passes
 * through. Dev mode is untouched, and `ADA_ALLOW_TEST_STORE=1` lets a demo
 * build through on purpose, by name.
 */
export function refuseTestStoreKey(
  mode: string,
  publicKey: string | undefined,
  allow: string | undefined
): void {
  if (mode !== "production") return;
  if (!/^test_/.test(publicKey ?? "")) return;
  if (allow === "1") return;
  throw new Error(TEST_STORE_REFUSAL);
}

// https://vite.dev/config/
export default defineConfig(async ({ mode }) => {
  // `loadEnv` reads .env, .env.local, .env.[mode] and .env.[mode].local, the
  // same files Vite inlines into `import.meta.env`, so this sees exactly the
  // key the bundle would carry.
  const env = loadEnv(mode, process.cwd(), "VITE_");
  refuseTestStoreKey(mode, env.VITE_REVENUECAT_PUBLIC_KEY, process.env.ADA_ALLOW_TEST_STORE);
  return {
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
  },
  };
});
