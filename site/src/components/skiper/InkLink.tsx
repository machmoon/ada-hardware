// Mechanism adapted from Skiper UI, skiper40 Link001 (https://skiper-ui.com/registry/skiper40.json), used with
// attribution per skiper-ui.com/docs/quick-start ("Attribution to Skiper UI is required when using the free
// version"). Skiper 40 is itself "inspired by and adapted from https://cursor.com".
//
// What is taken: a ::before underline, h-[0.05em] w-full bg-current, scaled from scale-x-0 origin-right to
// scale-x-100 origin-left on hover over 300ms cubic-bezier(0.4,0,0.2,1). Re-implemented on Ada's tokens; no
// Skiper file ships verbatim. Changes: a plain <a> (upstream's Link000 imports next/link), no forced
// target="_blank", no arrow glyph, focus-visible shows the underline too, and motion-reduce drops the transition
// (upstream reduced only the arrow).
import { cn } from "@/lib/utils";

export function InkLink({ className, children, ...props }: React.ComponentPropsWithoutRef<"a">) {
  return (
    <a
      className={cn(
        "relative inline-flex items-center",
        "before:pointer-events-none before:absolute before:top-[1.5em] before:left-0 before:h-[0.05em] before:w-full before:bg-current before:content-['']",
        "before:origin-right before:scale-x-0 before:transition-transform before:duration-300 before:ease-[cubic-bezier(0.4,0,0.2,1)]",
        "hover:before:origin-left hover:before:scale-x-100 focus-visible:before:origin-left focus-visible:before:scale-x-100",
        "motion-reduce:before:transition-none",
        className,
      )}
      {...props}
    >
      {children}
    </a>
  );
}
