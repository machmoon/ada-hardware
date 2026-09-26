// Band M4, #try: the feature page's "freedom & control" card grid, with Aceternity's terminal in the second card.
import { Laptop, Terminal as TerminalIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Band, BandHeader, PillLink } from "./Band";
import { NavbarButton } from "@/components/aceternity/resizable-navbar";
import { Terminal } from "@/components/aceternity/terminal";
import { DEMO_MAILTO, TRY } from "@/content";

const CARD = "flex h-full min-w-0 flex-col gap-4 rounded-xl bg-[var(--fill-tsp-white-light)] p-6";

function CardTitle({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      {icon}
      <h3 className="text-base leading-5 font-medium text-[var(--text-primary)]">{children}</h3>
    </div>
  );
}

export function TryIt() {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(t);
  }, [copied]);

  return (
    <Band id="try" labelledBy="try-title">
      <BandHeader id="try-title" title={TRY.title} body={TRY.body} />
      <div className="grid w-full grid-cols-1 gap-6 sm:grid-cols-2">
        <div className={CARD}>
          <CardTitle icon={<Laptop size={20} aria-hidden className="shrink-0 text-[var(--text-tertiary)]" />}>
            {TRY.mac.title}
          </CardTitle>
          <p className="text-sm leading-5 text-[var(--text-secondary)]">{TRY.mac.body}</p>
          <div className="mt-auto">
            <PillLink href={DEMO_MAILTO}>Get a demo</PillLink>
          </div>
        </div>
        <div className={CARD}>
          <CardTitle icon={<TerminalIcon size={20} aria-hidden className="shrink-0 text-[var(--text-tertiary)]" />}>
            {TRY.engine.title}
          </CardTitle>
          <p className="text-sm leading-5 text-[var(--text-secondary)]">{TRY.engine.body}</p>
          <Terminal
            commands={[...TRY.engine.commands]}
            outputs={{}}
            username="you"
            typingSpeed={15}
            delayBetweenCommands={600}
            label="Install commands"
            className="max-w-none px-0"
          />
          <div>
            <NavbarButton
              as="button"
              type="button"
              variant="secondary"
              onClick={() => {
                navigator.clipboard?.writeText(TRY.engine.commands[0]).then(
                  () => setCopied(true),
                  () => {},
                );
              }}
            >
              {copied ? "Copied" : "Copy install command"}
            </NavbarButton>
          </div>
        </div>
      </div>
    </Band>
  );
}
