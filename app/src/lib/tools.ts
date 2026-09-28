// The outside tools Ada drives (KiCad, FreeCAD, ngspice): what this machine
// has, and a download-and-install with progress. The Rust side is
// `app/src-tauri/src/tools.rs`; this is its typed seam, the `lib/cli.ts`
// convention, so the Setup Assistant's Tools step is testable with no IPC.
//
// Progress arrives over a Tauri `Channel`, the shape Tauri's own guide uses for
// a download (`develop/calling-frontend.mdx`): `{event, data}` with camelCase
// fields. The byte formatting, speed and time-left wording follow OpenWhispr's
// `DownloadProgressBar.tsx` / `formatETA` (`vendor/openwhispr/src/components/ui/`,
// MIT, `hooks/useModelDownload.ts`).

import { Channel, invoke } from "@tauri-apps/api/core";

export type ToolId = "kicad" | "freecad" | "ngspice";

export interface ToolStatus {
  id: ToolId;
  name: string;
  version: string;
  purpose: string;
  installed: boolean;
  detail: string;
  /** Bytes to download, or null when not known up front (Homebrew). */
  downloadBytes: number | null;
  installable: boolean;
}

export type ToolEvent =
  | { event: "started"; data: { id: ToolId; contentLength: number } }
  | { event: "progress"; data: { id: ToolId; transferred: number; contentLength: number } }
  | { event: "verifying"; data: { id: ToolId } }
  | { event: "installing"; data: { id: ToolId } }
  | { event: "log"; data: { id: ToolId; line: string } }
  | { event: "finished"; data: { id: ToolId; path: string } }
  | { event: "failed"; data: { id: ToolId; message: string; cancelled: boolean } };

export interface ToolsApi {
  status: () => Promise<ToolStatus[]>;
  /** Resolves with the installed path; rejects with the failure sentence. */
  install: (id: ToolId, onEvent: (event: ToolEvent) => void) => Promise<string>;
  cancel: (id: ToolId) => Promise<void>;
}

export const tauriTools: ToolsApi = {
  status: () => invoke<ToolStatus[]>("tools_status"),
  install: (id, onEvent) => {
    const channel = new Channel<ToolEvent>();
    channel.onmessage = onEvent;
    return invoke<string>("tools_install", { id, onEvent: channel });
  },
  cancel: (id) => invoke<void>("tools_cancel", { id }),
};

/** Decimal units, as the hosts publish sizes (KiCad's 1.40 GB image). */
export function formatBytes(bytes: number): string {
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} KB`;
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(bytes < 10_000_000 ? 1 : 0)} MB`;
  return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
}

export function formatEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s left`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s left`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m left`;
}

/**
 * A smoothed download rate. Raw per-sample rates jump with every TCP burst, so
 * the time-left reading would too; an exponential moving average (the
 * smoothing browsers' download shelves use) keeps it readable.
 */
export interface RateState {
  at: number;
  bytes: number;
  bytesPerSecond: number;
}

export function nextRate(prev: RateState | null, bytes: number, at: number, smoothing = 0.2): RateState {
  if (!prev || at <= prev.at || bytes < prev.bytes) return { at, bytes, bytesPerSecond: prev?.bytesPerSecond ?? 0 };
  const instant = ((bytes - prev.bytes) * 1000) / (at - prev.at);
  const bytesPerSecond = prev.bytesPerSecond === 0 ? instant : prev.bytesPerSecond + smoothing * (instant - prev.bytesPerSecond);
  return { at, bytes, bytesPerSecond };
}

/** "412 MB of 1.40 GB · 18.2 MB/s · 1m 3s left", or the byte count alone. */
export function progressCaption(transferred: number, total: number, bytesPerSecond: number): string {
  const parts = [total > 0 ? `${formatBytes(transferred)} of ${formatBytes(total)}` : formatBytes(transferred)];
  if (bytesPerSecond > 0) {
    parts.push(`${(bytesPerSecond / 1_000_000).toFixed(1)} MB/s`);
    if (total > 0) {
      const eta = formatEta((total - transferred) / bytesPerSecond);
      if (eta) parts.push(eta);
    }
  }
  return parts.join(" · ");
}
