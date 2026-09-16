// @vitest-environment node
//
// The store plugin is permission-gated per command, and the shell's capability
// files are compiled into the binary: a command the JS backend calls without
// its `store:allow-*` line is refused at runtime with nothing in the UI.
// That is how `settings.json` was never read by any webview until 2026-09-16
// -- `load()` calls `store.entries()`, `store:allow-entries` was missing, init
// caught the refusal and every setting came from the localStorage mirror. The
// Rust gate read the file and the wizard read the mirror, and they disagreed.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/** Every `Store` method `tauriBackend` in ./store.ts calls. */
const STORE_COMMANDS_USED = ["load", "get", "set", "entries"];

const capabilitiesDir = join(__dirname, "../../../src-tauri/capabilities");

describe("store plugin capabilities", () => {
  const files = readdirSync(capabilitiesDir).filter((name) => name.endsWith(".json"));

  it("there is at least one capability file to check", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  for (const name of files) {
    it(`${name} allows every store command the settings backend calls, for the dashboard too`, () => {
      const cap = JSON.parse(readFileSync(join(capabilitiesDir, name), "utf8")) as {
        windows?: string[];
        permissions?: unknown[];
      };
      const perms = new Set((cap.permissions ?? []).map((p) => (typeof p === "string" ? p : JSON.stringify(p))));
      for (const cmd of STORE_COMMANDS_USED) {
        expect(perms.has(`store:allow-${cmd}`), `${name} lacks store:allow-${cmd}`).toBe(true);
      }
      expect(cap.windows ?? []).toContain("dashboard");
    });
  }
});
