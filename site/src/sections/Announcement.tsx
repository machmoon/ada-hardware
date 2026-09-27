// Band 2: manus's full-bleed 44px announcement strip (manus-reference.md band 2). Plain markup: Aceternity's
// sticky-banner was read and rejected (it is sticky, slides in, adds a close button and logs every scroll).
import { ArrowRight } from "lucide-react";
import { ANNOUNCEMENT } from "@/content";

export function Announcement() {
  return (
    <div className="w-full bg-[var(--fill-tsp-white-light)] px-4 py-[12px] sm:px-[24px]">
      <a
        href={ANNOUNCEMENT.href}
        className="mx-auto flex w-fit max-w-full items-center justify-center gap-[4px] text-center text-sm leading-5 font-medium text-[var(--text-primary)] hover:opacity-80 motion-safe:transition-opacity motion-safe:duration-300"
      >
        <span>{ANNOUNCEMENT.text}</span>
        <ArrowRight size={16} aria-hidden className="shrink-0" />
      </a>
    </div>
  );
}
