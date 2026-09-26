// Source: https://ui.aceternity.com/registry/container-scroll-animation.json (fetched 2026-09-25)
// Aceternity UI free component, used under the Aceternity License (https://ui.aceternity.com/licence).
// NOT covered by this repository's MIT licence. Modified for Ada:
//  1. Container `h-[60rem] md:h-[80rem] p-2 md:p-20` -> `h-[44rem] md:h-[64rem] px-4 sm:px-6 md:px-20`: the board
//     render is 2:1, shorter than upstream's 16:10 frame, so it needs less scroll.
//  2. The scroll offset is set explicitly to ["start end", "center center"]. Upstream relies on motion's default
//     ("start start" -> "end end"), which only runs forwards while the container is taller than the viewport;
//     at 44rem (704px) on an 844px phone it would run backwards, flattening the card before it arrives.
//  3. Card: `h-[30rem] md:h-[40rem]` -> the inner frame takes the image's aspect (1674/834), so the frame hugs the
//     image at every width with no empty bezel; radius 30 -> 22 (manus's card), inner 14; `bg-[#222222]
//     border-[#6C6C6C]` -> manus's black (--footer) and #4d4d4d (--text-secondary); inner bg-gray-100 -> white (the
//     render's own ground), and its md:p-4 goes so the image fills the frame. Upstream's six-layer box-shadow is kept: it is the component's depth cue.
//  4. Reduced motion: rotate 0, scale 1 and translate 0 as constants, so the final frame shows.
//  "use client" removed; `any` props typed.
import React, { useRef } from "react";
import { useScroll, useTransform, motion, useReducedMotion, type MotionValue } from "motion/react";

export const ContainerScroll = ({
  titleComponent,
  children,
}: {
  titleComponent: string | React.ReactNode;
  children: React.ReactNode;
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start end", "center center"],
  });
  const [isMobile, setIsMobile] = React.useState(false);

  React.useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth <= 768);
    };
    checkMobile();
    window.addEventListener("resize", checkMobile);
    return () => {
      window.removeEventListener("resize", checkMobile);
    };
  }, []);

  const scaleDimensions = () => {
    return isMobile ? [0.7, 0.9] : [1.05, 1];
  };

  const rotate = useTransform(scrollYProgress, [0, 1], [20, 0]);
  const scale = useTransform(scrollYProgress, [0, 1], scaleDimensions());
  const translate = useTransform(scrollYProgress, [0, 1], [0, -100]);

  return (
    <div
      className="relative flex h-[44rem] items-center justify-center px-4 sm:px-6 md:h-[64rem] md:px-20"
      ref={containerRef}
    >
      <div
        className="relative w-full py-10 md:py-40"
        style={{
          perspective: "1000px",
        }}
      >
        <Header translate={reduced ? 0 : translate} titleComponent={titleComponent} />
        <Card rotate={reduced ? 0 : rotate} translate={reduced ? 0 : translate} scale={reduced ? 1 : scale}>
          {children}
        </Card>
      </div>
    </div>
  );
};

export const Header = ({
  translate,
  titleComponent,
}: {
  translate: MotionValue<number> | number;
  titleComponent: string | React.ReactNode;
}) => {
  return (
    <motion.div
      style={{
        translateY: translate,
      }}
      className="div mx-auto max-w-5xl text-center"
    >
      {titleComponent}
    </motion.div>
  );
};

export const Card = ({
  rotate,
  scale,
  children,
}: {
  rotate: MotionValue<number> | number;
  scale: MotionValue<number> | number;
  translate: MotionValue<number> | number;
  children: React.ReactNode;
}) => {
  return (
    <motion.div
      style={{
        rotateX: rotate,
        scale,
        boxShadow:
          "0 0 #0000004d, 0 9px 20px #0000004a, 0 37px 37px #00000042, 0 84px 50px #00000026, 0 149px 60px #0000000a, 0 233px 65px #00000003",
      }}
      className="mx-auto -mt-12 h-auto w-full max-w-5xl rounded-[22px] border-4 border-[#4d4d4d] bg-[var(--footer)] p-2 shadow-2xl md:p-6"
    >
      <div className="aspect-[1674/834] w-full overflow-hidden rounded-[14px] bg-white">
        {children}
      </div>
    </motion.div>
  );
};
