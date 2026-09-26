// Mechanism adapted from Skiper UI, skiper19 (https://skiper-ui.com/registry/skiper19.json), used with
// attribution per skiper-ui.com/docs/quick-start ("Attribution to Skiper UI is required when using the free
// version"). Skiper 19 is itself "inspired by and adapted from https://comgio.ai/".
//
// What is taken: skiper19's LinePath maps useScroll({ target }).scrollYProgress through useTransform to a
// motion.path's pathLength, so the stroke draws as the page scrolls. Re-implemented on Ada's tokens and geometry;
// no Skiper file ships verbatim. There is no squiggle and no 350vh section here.
//
// Geometry: Ada's mark (site/assets/mark.svg, app/src-tauri/icons/gen_app_icon.py), a differential pair. Two traces
// of equal length run horizontally, jog 45 degrees up and to the right, and run on. Pitch p = 40, stroke 25 (the
// mark's 50/80). The lower trace starts its jog later by p * tan(22.5 deg) = 16.6, so the perpendicular gap on the
// slant is (16.6 + 40) / sqrt 2 = 40 = p (commit f208241, "Mark: keep the pair's gap on the slant"). Both paths are
// 407 + 80 sqrt 2 + 519 = 423.6 + 80 sqrt 2 + 502.4 long, so one pathLength draws them in lockstep: the claim shown.
import { motion, useReducedMotion, useScroll, useTransform } from "motion/react";
import { useRef } from "react";
import { cn } from "@/lib/utils";

const A = "M13 140 H420 L500 60 H1019";
const B = "M13 180 H436.6 L516.6 100 H1019";

export function PairTrace({ label, className }: { label: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start end", "center center"] });
  const pathLength = useTransform(scrollYProgress, [0, 1], [0, 1]);
  // A round cap on a zero-length dash still paints a dot; hide the pair until it has length.
  const opacity = useTransform(pathLength, (v) => (v > 0.001 ? 1 : 0));
  const style = reduced ? { pathLength: 1, opacity: 1 } : { pathLength, opacity };

  return (
    <div ref={ref} className={cn("w-full", className)}>
    <svg
      viewBox="0 40 1032 160"
      role="img"
      aria-label={label}
      className="block h-auto w-full"
      fill="none"
      stroke="var(--ada-copper)"
      strokeWidth={25}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <motion.path d={A} style={style} />
      <motion.path d={B} style={style} />
    </svg>
    </div>
  );
}
