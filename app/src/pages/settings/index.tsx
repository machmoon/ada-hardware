import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import {
  AdaPro,
  Appearance,
  CalmOutputToggle,
  AlwaysOnTopToggle,
  AppIconToggle,
  AutostartToggle,
  WakeWordToggle,
  OverlaySkin,
  CliTools,
  BillingSetup,
  Notifications,
  RunSetupAgain,
} from "./components";
import { PageLayout } from "@/layouts";
import { ShortcutManager } from "@/pages/shortcuts/components";
import {
  loadEngineBaseUrl,
  loadEngineToken,
} from "@/pages/engine/components/EngineConnection";

const Settings = () => {
  // `/settings#pro` from the strip's "Prepare fab order · Ada Pro" button
  // (src/lib/purchases/pane.ts): the pane ids are element ids, so the hash
  // names the pane and this scrolls to it once the page has rendered.
  const { hash } = useLocation();
  useEffect(() => {
    const id = hash.replace(/^#/, "");
    if (!id) return;
    document.getElementById(id)?.scrollIntoView({ block: "start" });
  }, [hash]);

  return (
    <PageLayout title="Settings" description="How Ada looks, sounds and starts">
      <Appearance />
      <CalmOutputToggle />
      <OverlaySkin />
      <CliTools />
      <Notifications />
      <WakeWordToggle />
      <AutostartToggle />
      <AppIconToggle />
      <AlwaysOnTopToggle />
      <AdaPro />
      <BillingSetup baseUrl={loadEngineBaseUrl()} token={loadEngineToken()} />
      <ShortcutManager />
      <RunSetupAgain />
    </PageLayout>
  );
};

export default Settings;
