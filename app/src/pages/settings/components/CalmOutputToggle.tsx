import { Switch, Label, Header } from "@/components";
import { useCalm, writeCalm } from "@/lib/calm";

/** Fold run receipts so a run reads as status, decisions and nothing else. */
export const CalmOutputToggle = ({ className }: { className?: string }) => {
  const calm = useCalm();
  return (
    <div id="calm-output" className={`space-y-2 ${className ?? ""}`}>
      <Header title="Calm output" description="Show progress and decisions. Fold the rest." isMainTitle />
      <div className="flex items-center justify-between">
        <Label className="text-sm font-medium">{calm ? "On" : "Off"}</Label>
        <Switch checked={calm} onCheckedChange={writeCalm} aria-label="Calm output" data-testid="calm-output" />
      </div>
    </div>
  );
};
