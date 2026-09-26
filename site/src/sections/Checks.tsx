// Band M2, #checks: the measured claim, with Ada's differential pair drawn by scroll (skiper19's mechanism).
import { Band, BandHeader } from "./Band";
import { InkLink } from "@/components/skiper/InkLink";
import { PairTrace } from "@/components/skiper/PairTrace";
import { CHECKS } from "@/content";

export function Checks() {
  return (
    <Band id="checks" labelledBy="checks-title">
      <BandHeader id="checks-title" title={CHECKS.title} body={CHECKS.body} />
      <PairTrace label={CHECKS.drawingLabel} />
      <div className="flex w-full max-w-[680px] flex-col items-center gap-2 text-center text-sm leading-5 text-[var(--text-secondary)]">
        <p>{CHECKS.caption}</p>
        <InkLink href={CHECKS.link.href} className="font-medium text-[var(--text-primary)]">
          {CHECKS.link.text}
        </InkLink>
      </div>
    </Band>
  );
}
