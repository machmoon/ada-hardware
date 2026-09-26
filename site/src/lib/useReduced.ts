// motion's own hook, for the parts <MotionConfig reducedMotion="user"> cannot
// reach: scroll-linked `style` MotionValues, timers and the vanish canvas.
// It reads (prefers-reduced-motion: reduce) synchronously on first call.
export { useReducedMotion as useReduced } from "motion/react";
