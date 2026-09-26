// Band 3: manus's hero prompt (manus-reference.md band 3), with the feature page's subtitle under the h1.
// The prompt card is Aceternity's placeholders-and-vanish-input. Submitting never pretends to run Ada: the vanish
// plays, then the chip row crossfades (manus's own swap target: opacity 0, y 8px, scale 0.98, 300ms ease-out) into
// a run row that hands over the real CLI command.
import { Cpu, Lightbulb, Zap } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { PlaceholdersAndVanishInput } from "@/components/aceternity/placeholders-and-vanish-input";
import { NavbarButton } from "@/components/aceternity/resizable-navbar";
import { HERO, demoMailtoWithBody, shellQuote } from "@/content";

const CHIP_ICONS = { zap: Zap, lightbulb: Lightbulb, cpu: Cpu } as const;

export function Hero() {
  const reduced = useReducedMotion();
  const [value, setValue] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const pending = useRef<string | null>(null);
  const [copied, setCopied] = useState(false);

  // The vanish ends by clearing the field; that is when the run row takes over.
  const onValueChange = (v: string) => {
    setValue(v);
    if (v === "" && pending.current !== null) {
      setSubmitted(pending.current);
      pending.current = null;
    }
  };

  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(t);
  }, [copied]);

  const command = submitted === null ? "" : `silkscreen ${shellQuote(submitted)}`;
  const swap = {
    initial: { opacity: 0, y: 8, scale: 0.98 },
    animate: { opacity: 1, y: 0, scale: 1 },
    exit: { opacity: 0, y: 8, scale: 0.98 },
    transition: { duration: reduced ? 0 : 0.3, ease: "easeOut" as const },
  };

  return (
    <section
      aria-labelledby="hero-title"
      className="mx-auto mt-[20vh] w-full px-4 pb-16 sm:max-w-[768px] sm:min-w-[360px] md:px-0"
    >
      <div className="mx-auto mb-[28px] flex max-w-[680px] flex-col items-center gap-3 text-center md:mb-[34px]">
        <h1
          id="hero-title"
          className="font-serif text-[28px] leading-[42px] font-normal text-[var(--text-primary)] md:text-[36px] md:leading-[54px]"
        >
          {HERO.title}
        </h1>
        <p className="w-full text-base leading-[1.5] font-normal text-[var(--text-secondary)]">{HERO.subtitle}</p>
      </div>

      <PlaceholdersAndVanishInput
        placeholders={HERO.placeholders}
        value={value}
        onValueChange={onValueChange}
        onSubmit={() => {
          pending.current = value;
          if (reduced) {
            setSubmitted(value);
            pending.current = null;
          }
        }}
      />

      <div className="relative mt-[20px]" aria-live="polite">
        <AnimatePresence mode="wait" initial={false}>
          {submitted === null ? (
            <motion.div key="chips" {...swap} className="flex flex-wrap items-center justify-center gap-2">
              {HERO.chips.map((chip) => {
                const Icon = CHIP_ICONS[chip.icon];
                return (
                  <button
                    key={chip.label}
                    type="button"
                    onClick={() => {
                      setValue(HERO.placeholders[chip.prompt]);
                      document.getElementById("intent")?.focus();
                    }}
                    className="inline-flex h-10 cursor-pointer items-center gap-2 rounded-full border border-[var(--border-main)] px-[14px] py-[7px] text-sm leading-[21px] font-normal text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)] motion-safe:transition-colors"
                  >
                    <Icon size={18} aria-hidden className="text-[var(--text-tertiary)]" />
                    {chip.label}
                  </button>
                );
              })}
            </motion.div>
          ) : (
            <motion.div key="run" {...swap} className="flex flex-col items-center gap-3">
              <p className="text-center text-sm leading-5 text-[var(--text-secondary)]">{HERO.runLine}</p>
              <code className="block w-full rounded-[12px] border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 py-2 text-left font-mono text-sm leading-[21px] text-[var(--text-primary)] [overflow-wrap:anywhere]">
                {command}
              </code>
              <div className="flex items-center gap-2">
                <NavbarButton
                  as="button"
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    navigator.clipboard?.writeText(command).then(
                      () => setCopied(true),
                      () => {},
                    );
                  }}
                >
                  {copied ? "Copied" : "Copy"}
                </NavbarButton>
                <NavbarButton href={demoMailtoWithBody(submitted)} variant="primary">
                  Get a demo
                </NavbarButton>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSubmitted(null);
                  setCopied(false);
                  document.getElementById("intent")?.focus();
                }}
                className="cursor-pointer rounded-[8px] px-2 py-1 text-sm leading-5 font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] motion-safe:transition-colors"
              >
                Try another
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </section>
  );
}
