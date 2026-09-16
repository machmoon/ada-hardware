import {
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
      <BillingSetup baseUrl={loadEngineBaseUrl()} token={loadEngineToken()} />
      <ShortcutManager />
      <RunSetupAgain />
    </PageLayout>
  );
};

export default Settings;
