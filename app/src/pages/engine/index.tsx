import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Header } from "@/components";
import { AgentAccess, EngineStartCommands } from "@/components/setup";
import { PageLayout } from "@/layouts";
import { useEngineHealth } from "@/hooks";
import { useSilkscreenRun } from "@/contexts/run.context";
import {
  EngineConnection,
  EngineStatus,
  VoiceSettings,
  loadEngineBaseUrl,
  loadEngineToken,
} from "./components";

/**
 * The engine page: point Ada at a running service, and prove it is alive.
 *
 * This page exists because "connection refused" is not a diagnosis. Every
 * other surface in the app fails the same opaque way when the Python service
 * is not running, and the service is a separate process the user starts by
 * hand — so somewhere has to say which address is in use, whether anything is
 * answering there, and what to type if nothing is.
 */
const Engine = () => {
  // Read once: a rerender must not reach back into storage and stomp a value
  // the user just changed.
  const navigate = useNavigate();
  // The wizard's "Use a different address" lands here with ?return=/welcome.
  const [params] = useSearchParams();
  const returnTo = params.get("return");
  const [baseUrl, setBaseUrl] = useState(loadEngineBaseUrl);
  const [token, setToken] = useState(loadEngineToken);
  // The poll restarts on its own when `baseUrl` changes — the hook keys on it.
  const health = useEngineHealth(baseUrl, undefined, token);
  // Saving here must reach the run layer too, or the address on this page and
  // the address runs actually target quietly diverge until the next launch.
  const run = useSilkscreenRun();
  const applyBaseUrl = (url: string) => {
    setBaseUrl(url);
    run.setBaseUrl(url);
  };
  const applyToken = (value: string) => {
    setToken(value);
    run.setToken(value);
  };

  return (
    <PageLayout
      title="Engine"
      description="The service that designs your boards"
      rightSlot={
        <div className="flex items-center gap-2">
          {returnTo === "/welcome" ? (
            <Button size="sm" variant="outline" onClick={() => navigate(returnTo)} data-testid="engine-return-setup">
              Back to setup
            </Button>
          ) : null}
          <EngineStatus health={health} showDetail />
          <Button
            size="sm"
            variant="outline"
            onClick={health.recheck}
            disabled={health.checking}
          >
            Recheck
          </Button>
        </div>
      }
    >
      <div id="engine-start" className="space-y-3">
        <Header title="Start" description="Ada can run the engine for you" isMainTitle />
        <EngineStartCommands baseUrl={baseUrl} onEngineChange={health.recheck} />
      </div>

      <EngineConnection
        baseUrl={baseUrl}
        onBaseUrlChange={applyBaseUrl}
        token={token}
        onTokenChange={applyToken}
      />

      <AgentAccess />

      <VoiceSettings />
    </PageLayout>
  );
};

export default Engine;
