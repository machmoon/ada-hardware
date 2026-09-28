import { useCallback, useEffect, useRef, useState } from "react";
import { CheckIcon, DownloadIcon, Loader2Icon, XIcon } from "lucide-react";
import { Button } from "@/components/ui";
import { StepFrame } from "@/components/setup";
import {
  formatBytes,
  nextRate,
  progressCaption,
  tauriTools,
  type RateState,
  type ToolEvent,
  type ToolId,
  type ToolStatus,
  type ToolsApi,
} from "@/lib/tools";
import type { SetupCardId } from "@/lib/setup/machine";

export const TOOLS_TITLE = "Download the tools";
export const TOOLS_SUBTITLE =
  "KiCad, FreeCAD and ngspice run beside Ada. Download what this Mac is missing.";
export const TOOLS_UNKNOWN = "Ada could not ask this machine what it has. That answer only exists in the desktop app.";

type Phase = "idle" | "queued" | "downloading" | "verifying" | "installing" | "done" | "failed";

interface RowState {
  phase: Phase;
  transferred: number;
  total: number;
  rate: RateState | null;
  line: string;
  error: string;
}

const IDLE: RowState = { phase: "idle", transferred: 0, total: 0, rate: null, line: "", error: "" };
const BUSY: readonly Phase[] = ["downloading", "verifying", "installing"];

interface ToolsStepProps {
  setCard: (card: SetupCardId, done: boolean) => void;
  /** Injected so tests need no Tauri IPC. */
  api?: ToolsApi;
  now?: () => number;
}

/**
 * The Tools screen: one row per outside tool, OpenWhispr's required-download
 * checklist (`vendor/openwhispr/src/components/onboarding/RequiredModelDownloadStep.tsx`):
 * one download at a time, later rows wait as "Queued", a failed row waits for
 * an explicit Retry. Two deliberate differences: nothing starts until the user
 * presses a button (these are gigabytes, not a model the org requires), and
 * Continue is never blocked, because every tool is optional to designing a
 * board. A download already running finishes in the shell if the user moves
 * on; one still queued does not start, and Settings can run setup again.
 */
export const ToolsStep = ({ setCard, api = tauriTools, now = Date.now }: ToolsStepProps) => {
  const [tools, setTools] = useState<ToolStatus[] | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [rows, setRows] = useState<Record<string, RowState>>({});
  const queue = useRef<ToolId[]>([]);
  const running = useRef<ToolId | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await api.status();
      setTools(list);
      setUnknown(false);
    } catch {
      setUnknown(true);
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A card is done only when the tool is actually on this Mac.
  useEffect(() => {
    for (const tool of tools ?? []) setCard(tool.id, tool.installed);
  }, [tools, setCard]);

  const patch = useCallback((id: ToolId, change: Partial<RowState>) => {
    setRows((current) => ({ ...current, [id]: { ...(current[id] ?? IDLE), ...change } }));
  }, []);

  const onEvent = useCallback(
    (e: ToolEvent) => {
      const id = e.data.id;
      switch (e.event) {
        case "started":
          patch(id, { phase: "downloading", total: e.data.contentLength, transferred: 0, rate: null, error: "" });
          break;
        case "progress":
          setRows((current) => {
            const row = current[id] ?? IDLE;
            return {
              ...current,
              [id]: {
                ...row,
                phase: "downloading",
                transferred: e.data.transferred,
                total: e.data.contentLength,
                rate: nextRate(row.rate, e.data.transferred, now()),
              },
            };
          });
          break;
        case "verifying":
          patch(id, { phase: "verifying" });
          break;
        case "installing":
          patch(id, { phase: "installing" });
          break;
        case "log":
          patch(id, { line: e.data.line });
          break;
        case "finished":
          patch(id, { phase: "done", line: "", error: "" });
          break;
        case "failed":
          patch(id, { phase: e.data.cancelled ? "idle" : "failed", error: e.data.cancelled ? "" : e.data.message });
          break;
      }
    },
    [now, patch],
  );

  const pump = useCallback(async () => {
    if (running.current) return;
    const next = queue.current.shift();
    if (!next) return;
    running.current = next;
    patch(next, { phase: "downloading", error: "", line: "" });
    try {
      await api.install(next, onEvent);
    } catch (error) {
      // The channel's `failed` event normally says this first; a rejection
      // with no event (the command never started) still reaches the row.
      setRows((current) => {
        const row = current[next] ?? IDLE;
        if (row.phase === "failed" || row.phase === "idle") return current;
        return { ...current, [next]: { ...row, phase: "failed", error: String((error as Error)?.message ?? error) } };
      });
    } finally {
      running.current = null;
      await refresh();
      void pump();
    }
  }, [api, onEvent, patch, refresh]);

  const enqueue = useCallback(
    (ids: ToolId[]) => {
      for (const id of ids) {
        if (running.current === id || queue.current.includes(id)) continue;
        queue.current.push(id);
        patch(id, { phase: "queued", error: "" });
      }
      void pump();
    },
    [patch, pump],
  );

  const cancel = useCallback(
    (id: ToolId) => {
      if (running.current === id) {
        void api.cancel(id).catch(() => undefined);
        return;
      }
      queue.current = queue.current.filter((queued) => queued !== id);
      patch(id, { phase: "idle" });
    },
    [api, patch],
  );

  const missing = (tools ?? []).filter((t) => !t.installed && t.installable);
  const idleMissing = missing.filter((t) => {
    const phase = rows[t.id]?.phase ?? "idle";
    return phase === "idle" || phase === "failed";
  });
  const allBytes = idleMissing.reduce((sum, t) => sum + (t.downloadBytes ?? 0), 0);

  return (
    <StepFrame stepId="tools" title={TOOLS_TITLE} subtitle={TOOLS_SUBTITLE}>
      <div className="space-y-3 text-left">
        {unknown ? (
          <p className="rounded-lg border bg-card p-3 text-xs text-muted-foreground" data-testid="tools-unknown">
            {TOOLS_UNKNOWN}
          </p>
        ) : tools === null ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground" data-testid="tools-loading">
            <Loader2Icon className="size-4 animate-spin" aria-hidden="true" /> Looking at this Mac…
          </p>
        ) : (
          <>
            <div className="divide-y rounded-lg border bg-card">
              {tools.map((tool) => (
                <ToolRow
                  key={tool.id}
                  tool={tool}
                  row={rows[tool.id] ?? IDLE}
                  onDownload={() => enqueue([tool.id])}
                  onCancel={() => cancel(tool.id)}
                />
              ))}
            </div>
            {idleMissing.length > 1 ? (
              <div className="text-center">
                <Button size="sm" onClick={() => enqueue(idleMissing.map((t) => t.id))} data-testid="tools-download-all">
                  <DownloadIcon className="size-4" aria-hidden="true" />
                  Download all{allBytes > 0 ? ` · ${formatBytes(allBytes)}` : ""}
                </Button>
              </div>
            ) : null}
            {missing.length === 0 ? (
              <p className="text-center text-xs text-muted-foreground" data-testid="tools-all-here">
                Everything Ada uses is on this Mac.
              </p>
            ) : null}
          </>
        )}
      </div>
    </StepFrame>
  );
};

interface ToolRowProps {
  tool: ToolStatus;
  row: RowState;
  onDownload: () => void;
  onCancel: () => void;
}

const ToolRow = ({ tool, row, onDownload, onCancel }: ToolRowProps) => {
  const busy = BUSY.includes(row.phase);
  const installed = tool.installed || row.phase === "done";
  const percent = row.total > 0 ? Math.min(100, (row.transferred / row.total) * 100) : 0;
  // Homebrew and the post-download phases have no byte count: an honest
  // indeterminate bar, never a bar stuck at 0 %.
  const indeterminate = row.phase === "verifying" || row.phase === "installing" || (busy && row.total === 0);

  const caption = (() => {
    if (row.phase === "failed") return row.error;
    if (row.phase === "queued") return "Queued";
    if (row.phase === "verifying") return "Checking the download…";
    if (row.phase === "installing") return row.line || "Installing…";
    if (row.phase === "downloading")
      return row.total > 0 || row.transferred > 0
        ? progressCaption(row.transferred, row.total, row.rate?.bytesPerSecond ?? 0)
        : row.line || "Starting…";
    if (installed) return tool.installed ? tool.detail : "Installed";
    return tool.purpose;
  })();

  return (
    <div className="space-y-2 p-3" data-testid="tool-row" data-tool={tool.id} data-phase={installed ? "done" : row.phase}>
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">
            {tool.name}{" "}
            <span className="font-normal text-muted-foreground">
              {tool.version}
              {!installed && tool.downloadBytes ? ` · ${formatBytes(tool.downloadBytes)}` : ""}
            </span>
          </p>
          <p
            className={`truncate text-xs tabular-nums ${row.phase === "failed" ? "text-destructive" : "text-muted-foreground"}`}
            title={caption}
            data-testid="tool-caption"
          >
            {caption}
          </p>
        </div>
        {installed ? (
          <span className="flex h-7 shrink-0 items-center gap-1 rounded-full bg-emerald-600/10 px-3 text-xs text-emerald-700 dark:text-emerald-400">
            <CheckIcon className="size-3.5" aria-hidden="true" /> Installed
          </span>
        ) : busy || row.phase === "queued" ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={onCancel}
            disabled={row.phase === "verifying" || row.phase === "installing"}
            aria-label={`Cancel ${tool.name}`}
            data-testid="tool-cancel"
          >
            <XIcon className="size-4" aria-hidden="true" />
          </Button>
        ) : tool.installable ? (
          <Button size="sm" variant="outline" onClick={onDownload} data-testid="tool-download">
            {row.phase === "failed" ? "Retry" : "Download"}
          </Button>
        ) : (
          <span className="shrink-0 text-xs text-muted-foreground" data-testid="tool-manual">
            Manual install
          </span>
        )}
      </div>
      {!tool.installable && !installed ? (
        <p className="text-xs" data-testid="tool-detail">
          {tool.detail}
        </p>
      ) : null}
      {busy ? (
        <div
          className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
          role="progressbar"
          aria-label={`${tool.name} download`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={indeterminate ? undefined : Math.round(percent)}
          data-testid="tool-progress"
        >
          {indeterminate ? (
            <div className="kv-indeterminate h-full w-1/3 rounded-full bg-primary" />
          ) : (
            <div className="h-full rounded-full bg-primary transition-[width] duration-300 ease-out" style={{ width: `${percent}%` }} />
          )}
        </div>
      ) : null}
    </div>
  );
};
