import { useState } from "react";
import { CheckIcon, CopyIcon, Loader2, PlayIcon } from "lucide-react";
import { Button } from "@/components/ui";
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard";
import { CliError, runCli, type CliToolId } from "@/lib/cli";
import { cn } from "@/lib/utils";

/** How the app runs this command itself, through the `run_cli` allowlist. */
export interface CommandRun {
  tool: CliToolId;
  args: string[];
  timeoutSecs?: number;
}

type RunState =
  | { state: "idle" }
  | { state: "running" }
  | { state: "done"; ok: boolean; output: string };

export interface CommandLineProps {
  command: string;
  /** Present when the app can run the command itself. */
  run?: CommandRun;
  className?: string;
}

/**
 * One command: copy it, or run it here when the app is allowed to. The
 * result is one line. The full output opens on request.
 */
export const CommandLine = ({ command, run, className }: CommandLineProps) => {
  const { isCopied, handleCopy } = useCopyToClipboard({ text: command });
  const [result, setResult] = useState<RunState>({ state: "idle" });
  const [open, setOpen] = useState(false);

  const start = async () => {
    if (!run) return;
    setResult({ state: "running" });
    setOpen(false);
    try {
      const out = await runCli(run.tool, run.args, run.timeoutSecs);
      setResult({ state: "done", ok: true, output: (out.stdout + out.stderr).trim() });
    } catch (error) {
      const r = error instanceof CliError ? error.result : undefined;
      const output = r ? (r.stdout + r.stderr).trim() : String(error instanceof Error ? error.message : error);
      setResult({ state: "done", ok: false, output });
    }
  };

  return (
    <div className={cn("space-y-1", className)} data-testid="command-line">
      <div className="flex items-center gap-1 rounded-lg border border-input/50 bg-muted/40 py-1 pl-2.5 pr-1">
        <code
          className="flex-1 truncate font-mono text-[11px] lg:text-xs select-all"
          title={command}
          data-testid="command-text"
        >
          {command}
        </code>
        {run ? (
          <Button
            size="sm"
            variant="ghost"
            className="h-6 gap-1 px-2 text-xs"
            onClick={start}
            disabled={result.state === "running"}
            data-testid="command-run"
          >
            {result.state === "running" ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <PlayIcon className="size-3" />
            )}
            Run
          </Button>
        ) : null}
        <Button
          size="icon"
          variant="ghost"
          className="size-6 shrink-0"
          aria-label={isCopied ? "Copied" : "Copy"}
          title={isCopied ? "Copied" : "Copy"}
          onClick={handleCopy}
          data-testid="command-copy"
        >
          {isCopied ? <CheckIcon className="size-3" /> : <CopyIcon className="size-3" />}
        </Button>
      </div>
      {result.state === "done" ? (
        <div className="pl-2.5 text-[11px]" data-testid="command-result" data-ok={result.ok ? "yes" : "no"}>
          <button
            type="button"
            className={cn("hover:underline", result.ok ? "text-muted-foreground" : "text-destructive")}
            onClick={() => setOpen((v) => !v)}
          >
            {result.ok ? "Done" : "Failed"}
            {result.output ? (open ? " · hide output" : " · show output") : ""}
          </button>
          {open && result.output ? (
            <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-2 font-mono text-[11px] text-muted-foreground">
              {result.output}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};
