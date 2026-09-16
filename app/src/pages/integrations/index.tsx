import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2Icon, RefreshCwIcon, XCircleIcon } from "lucide-react";
import { Button, Header } from "@/components";
import { PageLayout } from "@/layouts";
import {
  loadEngineBaseUrl,
  loadEngineToken,
} from "@/pages/engine/components/EngineConnection";
import {
  fetchIntegrations,
  groupByKind,
  runIntegrationAction,
  type Integration,
  type IntegrationAction,
  type IntegrationKind,
} from "@/lib/silkscreen/integrations";
import { logEvent } from "@/lib/silkscreen/log";
import { IntegrationCard } from "./components";

/** Heading and one line of orientation per group, in the frozen kind order. */
const GROUP: Record<IntegrationKind, { title: string; description: string }> = {
  delivery: {
    title: "Delivery",
    description: "Where finished boards go",
  },
  meeting: {
    title: "Meetings",
    description: "Meetings that can start a board",
  },
  design: {
    title: "Design",
    description: "What Ada adds beyond the board",
  },
  fabrication: {
    title: "Fabrication",
    description: "From routed board to factory",
  },
};

/**
 * Every Ada surface, whether it is set up, and what to do about it if not.
 *
 * The panel exists because the answer to "can this thing email me the board?"
 * was previously spread across a Python module docstring, an `.env.example`
 * line and a failed run. Each surface has its own credentials, its own
 * optional package and its own idea of being half-configured, and none of that
 * was visible anywhere until something failed.
 *
 * Two rules run through the whole page. Nothing that exists is hidden — an
 * integration the engine cannot even import is listed as `unavailable` rather
 * than dropped, because "not built here" and "not on the menu" are different
 * facts and only one of them is true. And the fixes are the engine's own
 * words: the settings live in the environment the service was started with,
 * which this window cannot reach, so the hints are printed verbatim.
 */
const Integrations = () => {
  // Read once per mount from the same validated source the router seeds the
  // run provider from, so this page asks the engine that runs actually target.
  const [baseUrl] = useState(loadEngineBaseUrl);
  const [token] = useState(loadEngineToken);

  const [items, setItems] = useState<Integration[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [failures, setFailures] = useState<Record<string, string>>({});
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);

  const load = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      return fetchIntegrations(baseUrl, token, signal)
        .then((found) => {
          if (!mountedRef.current || signal?.aborted) return;
          setItems(found);
          setLoadError(null);
        })
        .catch((caught: unknown) => {
          if (!mountedRef.current || signal?.aborted) return;
          // A failure must never read as "you have no integrations". The list
          // is left exactly as it was and the reason is shown instead.
          setLoadError(
            (caught as Error)?.message ??
              "Could not ask the engine what is configured."
          );
        })
        .finally(() => {
          if (mountedRef.current && !signal?.aborted) setLoading(false);
        });
    },
    [baseUrl, token]
  );

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    void load(controller.signal);
    return () => {
      mountedRef.current = false;
      controller.abort();
    };
  }, [load]);

  const refresh = useCallback(() => {
    if (inFlightRef.current) return;
    void load();
  }, [load]);

  const onAction = useCallback(
    (item: Integration, action: IntegrationAction) => {
      // One in flight at a time. Every action is a real call against the
      // engine — a sign-in opens a browser, a probe spends a request — and a
      // second click while the first is running would be a second one.
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      setBusy(item.id);
      setFailures((previous) => {
        const next = { ...previous };
        delete next[item.id];
        return next;
      });
      void (async () => {
        try {
          await runIntegrationAction(baseUrl, token, action);
          logEvent("integrations", `${item.name}: ${action.label} completed.`);
          // The action changed something on the engine side; the states on
          // screen are now claims about a world that has moved.
          await load();
        } catch (caught) {
          if (!mountedRef.current) return;
          const message =
            (caught as Error)?.message ?? `${action.label} failed`;
          setFailures((previous) => ({ ...previous, [item.id]: message }));
        } finally {
          inFlightRef.current = false;
          if (mountedRef.current) setBusy(null);
        }
      })();
    },
    [baseUrl, token, load]
  );

  const groups = items ? groupByKind(items) : [];
  const waiting = loading && items === null && loadError === null;

  return (
    <PageLayout
      title="Connections"
      description="What Ada can reach, and what is set up"
      rightSlot={
        <Button
          size="sm"
          variant="outline"
          data-testid="integrations-refresh"
          onClick={refresh}
          disabled={loading || busy !== null}
        >
          {loading ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <RefreshCwIcon className="size-3.5" />
          )}
          Refresh
        </Button>
      }
    >
      {loadError && (
        <div
          data-testid="integrations-error"
          className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-3"
        >
          <XCircleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div className="text-xs leading-relaxed">
            <p className="font-medium text-destructive">
              The engine at {baseUrl} did not answer
            </p>
            {/* The engine's own words. "Connection refused" and "answered 404"
                send the user to entirely different fixes. */}
            <p className="mt-1 text-muted-foreground">
              Start it on the Engine tab, then refresh.
            </p>
            <details className="mt-1 text-muted-foreground">
              <summary className="cursor-pointer">Details</summary>
              <p className="font-mono break-all">{loadError}</p>
            </details>
          </div>
        </div>
      )}

      {waiting && (
        <p
          data-testid="integrations-loading"
          className="text-xs text-muted-foreground"
        >
          Checking…
        </p>
      )}

      {groups.map((group) => (
        <div key={group.kind} className="space-y-3">
          <Header
            title={GROUP[group.kind].title}
            description={GROUP[group.kind].description}
            isMainTitle
          />
          <div
            data-testid="integration-group"
            data-kind={group.kind}
            className="flex flex-col gap-3"
          >
            {group.items.map((item) => (
              <IntegrationCard
                key={item.id}
                item={item}
                busy={busy === item.id}
                locked={busy !== null}
                failure={failures[item.id]}
                onAction={onAction}
              />
            ))}
          </div>
        </div>
      ))}

      {items !== null && items.length === 0 && !loadError && (
        <p
          data-testid="integrations-empty"
          className="text-xs text-muted-foreground"
        >
          The engine listed nothing. Update the engine.
        </p>
      )}

      <p className="text-[11px] text-muted-foreground" data-testid="integrations-note">
        These come from the engine's environment. Set a key, then restart the engine.
        Ready means set up, not yet tested live.
      </p>
    </PageLayout>
  );
};

export default Integrations;
