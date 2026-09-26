import { MotionConfig } from "motion/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

// reducedMotion="user": motion switches transform animations off when the device asks for reduced motion
// (motiondivision/motion packages/framer-motion/src/context/MotionConfigContext.tsx). Scroll-linked values, timers
// and the vanish canvas check useReducedMotion() themselves.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
      <App />
    </MotionConfig>
  </StrictMode>,
);
