// Start and stop the local engine (Rust `engine.rs`). The engine is the
// checkout's `python -m silkscreen.serve`; its output goes to the log file
// the status names.

import { invoke } from "@tauri-apps/api/core";

export interface EngineProcessStatus {
  running: boolean;
  pid: number | null;
  port: number | null;
  log: string;
}

export const engineProcessStatus = () => invoke<EngineProcessStatus>("engine_status");

export const startEngine = (port: number) =>
  invoke<EngineProcessStatus>("engine_start", { port });

export const stopEngine = () => invoke<EngineProcessStatus>("engine_stop");

/** The port in a loopback base URL, or the engine's default. */
export function portOf(baseUrl: string): number {
  try {
    const port = Number(new URL(baseUrl).port);
    return Number.isInteger(port) && port > 0 ? port : 8081;
  } catch {
    return 8081;
  }
}
