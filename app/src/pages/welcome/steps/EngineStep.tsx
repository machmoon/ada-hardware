import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2Icon, Loader2Icon, XCircleIcon } from "lucide-react";
import { Button } from "@/components/ui";
import { EngineStartCommands, StepFrame } from "@/components/setup";
import { useEngineHealth } from "@/hooks/useEngineHealth";
import { fetchKeyStatus, keyAllowsContinue, type KeyStatus } from "@/lib/setup/engine";

export const ENGINE_TITLE = "Start the engine";
export const ENGINE_TITLE_DOWN = "Start the engine in a terminal";
export const ENGINE_SUBTITLE =
  "The engine that generates boards runs outside Ada. KiCad and the other tools come next.";

/** How long a healthy engine sits on screen before the wizard moves on. */
export const AUTO_ADVANCE_MS = 600;
export const ENGINE_POLL_MS = 2000;

interface EngineStepProps {
  baseUrl: string;
  token: string;
  /** Whether Continue may be enabled, whenever the answer changes. */
  onCanContinue: (allowed: boolean) => void;
  /** Fired once, after the first healthy probe with a usable key row. */
  onAutoAdvance: () => void;
}

/**
 * The app cannot start Python. When nothing answers, this screen is the
 * terminal commands with Copy; when something does, it says exactly what
 * answered ("Answered /healthz at …", never "Running") and then asks the
 * second question, whether that engine has a key, in the service's words.
 */
export const EngineStep = ({
  baseUrl,
  token,
  onCanContinue,
  onAutoAdvance,
}: EngineStepProps) => {
  const navigate = useNavigate();
  const health = useEngineHealth(baseUrl, ENGINE_POLL_MS, token);
  const [key, setKey] = useState<KeyStatus | null>(null);
  const [keyChecked, setKeyChecked] = useState(false);
  const advanced = useRef(false);

  // The key row, once per healthy transition.
  useEffect(() => {
    if (!health.ok) {
      setKey(null);
      setKeyChecked(false);
      return;
    }
    let cancelled = false;
    void fetchKeyStatus(baseUrl, token).then((status) => {
      if (cancelled) return;
      setKey(status);
      setKeyChecked(true);
    });
    return () => {
      cancelled = true;
    };
  }, [health.ok, baseUrl, token]);

  const allowed = health.ok && keyChecked && keyAllowsContinue(key);

  useEffect(() => {
    onCanContinue(allowed);
  }, [allowed, onCanContinue]);

  useEffect(() => {
    if (!allowed || advanced.current) return;
    advanced.current = true;
    const timer = window.setTimeout(onAutoAdvance, AUTO_ADVANCE_MS);
    return () => window.clearTimeout(timer);
  }, [allowed, onAutoAdvance]);

  const probing = health.lastCheckedAt === null;
  const down = !probing && !health.ok;

  return (
    <StepFrame stepId="engine" title={down ? ENGINE_TITLE_DOWN : ENGINE_TITLE} subtitle={ENGINE_SUBTITLE}>
      <div className="space-y-4 text-left">
        <div
          className="flex items-start gap-3 rounded-lg border bg-card p-3"
          data-testid="engine-card"
          data-state={probing ? "probing" : health.ok ? "up" : "down"}
        >
          {probing ? (
            <Loader2Icon className="mt-0.5 size-4 shrink-0 animate-spin text-muted-foreground" aria-hidden="true" />
          ) : health.ok ? (
            <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600" aria-hidden="true" />
          ) : (
            <XCircleIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <p className="text-sm font-medium" data-testid="engine-answer">
              {probing
                ? `Asking ${baseUrl} …`
                : health.ok
                  ? `Answered /healthz at ${baseUrl}`
                  : `Nothing answered at ${baseUrl}`}
            </p>
            {down && health.detail ? (
              <p className="text-xs text-muted-foreground">{health.detail}</p>
            ) : null}
            {health.ok ? (
              <p
                className={`text-xs ${key && !keyAllowsContinue(key) ? "text-destructive" : "text-muted-foreground"}`}
                data-testid="engine-key"
                data-state={key?.state ?? "checking"}
              >
                {!keyChecked
                  ? "Checking for a Gemini key…"
                  : key?.state === "ready"
                    ? `Gemini key: ${key.summary}`
                    : key?.summary || "Key state unknown."}
              </p>
            ) : null}
          </div>
          <Button size="sm" variant="ghost" onClick={health.recheck} disabled={health.checking} data-testid="engine-recheck">
            {health.checking ? "Checking…" : "Recheck"}
          </Button>
        </div>

        {down ? (
          <EngineStartCommands compact only={["serve"]} baseUrl={baseUrl} onEngineChange={health.recheck} />
        ) : null}

        <div className="text-center">
          <Button
            variant="link"
            size="sm"
            onClick={() => navigate("/engine?return=/welcome")}
            data-testid="engine-different-address"
          >
            Use a different address
          </Button>
        </div>
      </div>
    </StepFrame>
  );
};
