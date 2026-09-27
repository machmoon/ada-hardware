// Band M5: the feature page's closing panel (FEAT's last band), one black pill.
import { PillLink } from "./Band";
import { CLOSING, DEMO_MAILTO } from "@/content";

export function Closing() {
  return (
    <section aria-labelledby="closing-title" className="flex w-full justify-center px-4 pt-16 pb-32 sm:px-6 md:pt-20 md:pb-40">
      <div className="box-border flex w-full max-w-[1032px] flex-col items-center justify-center gap-6 rounded-2xl bg-[var(--fill-tsp-white-light)] px-6 py-10 md:px-12 md:py-20">
        <div className="flex max-w-[680px] flex-col gap-2 text-center">
          <h2
            id="closing-title"
            className="font-serif text-[28px] leading-[1.2] font-[600] tracking-[-0.56px] text-[var(--text-primary)] md:text-[40px] md:tracking-[-0.8px]"
          >
            {CLOSING.title}
          </h2>
          <p className="text-base leading-6 text-[var(--text-secondary)]">{CLOSING.body}</p>
        </div>
        <PillLink href={DEMO_MAILTO}>Get a demo</PillLink>
      </div>
    </section>
  );
}
