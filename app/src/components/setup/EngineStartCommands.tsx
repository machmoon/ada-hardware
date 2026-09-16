import { useEffect, useState } from "react";
import { Loader2, PlayIcon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui";
import { CommandLine } from "@/components/CommandLine";
import { ENGINE_START_STEPS, type EngineStartStep } from "@/config/kaleo.constants";
import {
  engineProcessStatus,
  portOf,
  startEngine,
  stopEngine,
  type EngineProcessStatus,
} from "@/lib/engine-process";

/** Start or stop the engine from the app. */
export const StartEngineButton = ({
  baseUrl,
  onChange,
}: {
  baseUrl: string;
  onChange?: () => void;
}) => {
  const [status, setStatus] = useState<EngineProcessStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    engineProcessStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  const act = async (start: boolean) => {
    setBusy(true);
    setError(null);
    try {
      setStatus(start ? await startEngine(portOf(baseUrl)) : await stopEngine());
      // The server takes a few seconds to bind before /healthz answers.
      setTimeout(() => onChange?.(), start ? 3000 : 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const running = status?.running ?? false;
  return (
    <div className="space-y-1" data-testid="engine-start-button">
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={() => act(!running)} disabled={busy}>
          {busy ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : running ? (
            <SquareIcon className="size-3.5" />
          ) : (
            <PlayIcon className="size-3.5" />
          )}
          {running ? "Stop engine" : "Start engine"}
        </Button>
        {running && status?.port ? (
          <span className="text-xs text-muted-foreground">Running on port {status.port}</span>
        ) : null}
      </div>
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
      {status?.log && (error || running) ? (
        <p className="truncate text-[11px] text-muted-foreground" title={status.log}>
          Log: {status.log}
        </p>
      ) : null}
    </div>
  );
};

const StartStep = ({ step, compact }: { step: EngineStartStep; compact: boolean }) => (
  <div data-testid="engine-start-step" data-step={step.id} className="space-y-1">
    <p className="text-xs font-medium">{step.title}</p>
    {compact ? null : <p className="text-xs text-muted-foreground">{step.detail}</p>}
    <CommandLine command={step.command} />
  </div>
);

interface EngineStartCommandsProps {
  /** Only these step ids, in catalogue order. Default: all of them. */
  only?: readonly EngineStartStep["id"][];
  /** Drop the detail lines: the wizard has a 620 px window. */
  compact?: boolean;
  /** Shows the in-app Start button when given. */
  baseUrl?: string;
  onEngineChange?: () => void;
  className?: string;
}

/** Start the engine here, or copy the command for a terminal. */
export const EngineStartCommands = ({
  only,
  compact = false,
  baseUrl,
  onEngineChange,
  className,
}: EngineStartCommandsProps) => {
  const steps = only ? ENGINE_START_STEPS.filter((s) => only.includes(s.id)) : ENGINE_START_STEPS;
  return (
    <div className={`space-y-3 ${className ?? ""}`} data-testid="engine-start-commands">
      {baseUrl ? <StartEngineButton baseUrl={baseUrl} onChange={onEngineChange} /> : null}
      {steps.map((step) => (
        <StartStep key={step.id} step={step} compact={compact} />
      ))}
    </div>
  );
};
