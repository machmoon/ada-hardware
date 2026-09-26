// Source: https://ui.aceternity.com/registry/placeholders-and-vanish-input.json (fetched 2026-09-25)
// Aceternity UI free component, used under the Aceternity License (https://ui.aceternity.com/licence).
// NOT covered by this repository's MIT licence. Modified for Ada:
//  1. Controllable: optional `value` and `onValueChange` props, so the hero's chips can fill the field.
//  2. Restyled from the h-12 pill to manus's prompt card (768 wide, min-h 128, radius 22, py-3 gap-3, 1px card
//     border, manus's card shadow): the input sits in a min-h-[46px] ps-4 pe-2 row, and the submit button moves to
//     a bottom-right toolbar row (px-3 h-8) as a 32px --Button-black circle with a lucide ArrowUp. Manus's "+"
//     button is omitted: there is nothing to attach on the web. The canvas is re-anchored inside the input row at
//     left-2 top-2, derived from upstream's own numbers: fillText(x 16, baseline 40) at scale-50 lands at (8, 20),
//     so left 8 puts the text on the input's 16px inset and top 8 puts it on a 16px line's baseline in a 46px row.
//  3. `max-w-xl` is removed; the hero column (sm:max-w-[768px]) sets the width.
//  4. Accessibility: a visually hidden <label>, id/name/autoComplete on the input, aria-hidden on the animated
//     placeholder, aria-label on the submit button, and a focus ring on the card while the input has focus.
//  5. NodeJS.Timeout -> ReturnType<typeof setInterval> (no @types/node in a browser build); "use client" removed.
//  6. Reduced motion (useReducedMotion): the placeholder does not rotate, the canvas vanish is skipped and the field
//     clears at once.
//  7. Enter is left to the form's own submit: upstream also called vanishAndSubmit() from onKeyDown, so one Enter
//     started two vanish loops. An empty field does not submit.
//  8. The placeholder uses --text-tertiary, not manus's --text-disable (#a6a6a6 is 2.4:1 on white; the placeholder
//     here carries real example prompts).
//  9. The placeholder wraps to two lines (line-clamp-2, top-aligned on the input's first line) instead of upstream's
//     `truncate`: at phone widths the example prompts are longer than the field and were cut off with an ellipsis.
// 10. The canvas is read back only when the vanish starts: upstream redrew and scanned all 800x800 pixels with
//     getImageData on every keystroke (a useEffect on `value`), and vanishAndSubmit() draws again anyway. The 2D
//     context is created with willReadFrequently, as Chrome asks of a canvas that is read back.
import { ArrowUp } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

type Dot = { x: number; y: number; r: number; color: string };

export function PlaceholdersAndVanishInput({
  placeholders,
  onChange,
  onSubmit,
  value: controlledValue,
  onValueChange,
  label = "Describe a board",
  submitLabel = "Show the command",
}: {
  placeholders: readonly string[];
  onChange?: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onSubmit?: (e: React.FormEvent<HTMLFormElement>) => void;
  value?: string;
  onValueChange?: (v: string) => void;
  label?: string;
  submitLabel?: string;
}) {
  const reduced = useReducedMotion();
  const [currentPlaceholder, setCurrentPlaceholder] = useState(0);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startAnimation = () => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    intervalRef.current = setInterval(() => {
      setCurrentPlaceholder((prev) => (prev + 1) % placeholders.length);
    }, 3000);
  };
  const handleVisibilityChange = () => {
    if (document.visibilityState !== "visible" && intervalRef.current) {
      clearInterval(intervalRef.current); // Clear the interval when the tab is not visible
      intervalRef.current = null;
    } else if (document.visibilityState === "visible") {
      startAnimation(); // Restart the interval when the tab becomes visible
    }
  };

  useEffect(() => {
    if (reduced) return;
    startAnimation();
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [placeholders, reduced]);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const newDataRef = useRef<Dot[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const [innerValue, setInnerValue] = useState("");
  const value = controlledValue ?? innerValue;
  const setValue = (v: string) => {
    if (controlledValue === undefined) setInnerValue(v);
    onValueChange?.(v);
  };
  const [animating, setAnimating] = useState(false);

  const draw = useCallback(() => {
    if (!inputRef.current) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) return;

    canvas.width = 800;
    canvas.height = 800;
    ctx.clearRect(0, 0, 800, 800);
    const computedStyles = getComputedStyle(inputRef.current);

    const fontSize = parseFloat(computedStyles.getPropertyValue("font-size"));
    ctx.font = `${fontSize * 2}px ${computedStyles.fontFamily}`;
    ctx.fillStyle = "#FFF";
    ctx.fillText(value, 16, 40);

    const imageData = ctx.getImageData(0, 0, 800, 800);
    const pixelData = imageData.data;
    const newData: { x: number; y: number; color: number[] }[] = [];

    for (let t = 0; t < 800; t++) {
      const i = 4 * t * 800;
      for (let n = 0; n < 800; n++) {
        const e = i + 4 * n;
        if (pixelData[e] !== 0 && pixelData[e + 1] !== 0 && pixelData[e + 2] !== 0) {
          newData.push({
            x: n,
            y: t,
            color: [pixelData[e], pixelData[e + 1], pixelData[e + 2], pixelData[e + 3]],
          });
        }
      }
    }

    newDataRef.current = newData.map(({ x, y, color }) => ({
      x,
      y,
      r: 1,
      color: `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${color[3]})`,
    }));
  }, [value]);

  const animate = (start: number) => {
    const animateFrame = (pos: number = 0) => {
      requestAnimationFrame(() => {
        const newArr = [];
        for (let i = 0; i < newDataRef.current.length; i++) {
          const current = newDataRef.current[i];
          if (current.x < pos) {
            newArr.push(current);
          } else {
            if (current.r <= 0) {
              current.r = 0;
              continue;
            }
            current.x += Math.random() > 0.5 ? 1 : -1;
            current.y += Math.random() > 0.5 ? 1 : -1;
            current.r -= 0.05 * Math.random();
            newArr.push(current);
          }
        }
        newDataRef.current = newArr;
        const ctx = canvasRef.current?.getContext("2d", { willReadFrequently: true });
        if (ctx) {
          ctx.clearRect(pos, 0, 800, 800);
          newDataRef.current.forEach((t) => {
            const { x: n, y: i, r: s, color: color } = t;
            if (n > pos) {
              ctx.beginPath();
              ctx.rect(n, i, s, s);
              ctx.fillStyle = color;
              ctx.strokeStyle = color;
              ctx.stroke();
            }
          });
        }
        if (newDataRef.current.length > 0) {
          animateFrame(pos - 8);
        } else {
          setValue("");
          setAnimating(false);
        }
      });
    };
    animateFrame(start);
  };

  const vanishAndSubmit = () => {
    if (reduced) {
      setValue("");
      return;
    }
    setAnimating(true);
    draw();

    const value = inputRef.current?.value || "";
    if (value && inputRef.current) {
      const maxX = newDataRef.current.reduce(
        (prev, current) => (current.x > prev ? current.x : prev),
        0,
      );
      animate(maxX);
    }
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (animating || !value.trim()) return;
    vanishAndSubmit();
    onSubmit && onSubmit(e);
  };
  return (
    <form
      className={cn(
        "relative flex min-h-[128px] w-full flex-col justify-between gap-3 overflow-hidden rounded-[22px] border border-[var(--card-border)] bg-[var(--background-menu-white)] py-3 shadow-card dark:border-[var(--border-main)]",
        "has-[input:focus-visible]:outline-2 has-[input:focus-visible]:outline-offset-2 has-[input:focus-visible]:outline-[var(--text-primary)]",
      )}
      onSubmit={handleSubmit}
    >
      <div className="relative min-h-[46px] ps-4 pe-2">
        <label className="sr-only" htmlFor="intent">
          {label}
        </label>
        <canvas
          className={cn(
            "pointer-events-none absolute top-2 left-2 origin-top-left scale-50 transform pr-20 text-base invert filter dark:invert-0",
            !animating ? "opacity-0" : "opacity-100",
          )}
          ref={canvasRef}
        />
        <input
          onChange={(e) => {
            if (!animating) {
              setValue(e.target.value);
              onChange && onChange(e);
            }
          }}
          ref={inputRef}
          value={value}
          type="text"
          id="intent"
          name="intent"
          autoComplete="off"
          className={cn(
            "relative z-10 h-[46px] w-full border-none bg-transparent text-[15px] leading-6 text-[var(--text-primary)] focus:ring-0 focus:outline-none sm:text-base",
            animating && "text-transparent dark:text-transparent",
          )}
        />
        <div
          className="pointer-events-none absolute inset-0 flex items-start ps-4 pe-2 pt-[11px]"
          aria-hidden="true"
        >
          <AnimatePresence mode="wait">
            {!value && (
              <motion.p
                initial={{
                  y: 5,
                  opacity: 0,
                }}
                key={`current-placeholder-${currentPlaceholder}`}
                animate={{
                  y: 0,
                  opacity: 1,
                }}
                exit={{
                  y: -15,
                  opacity: 0,
                }}
                transition={{
                  duration: reduced ? 0 : 0.3,
                  ease: "linear",
                }}
                className="line-clamp-2 w-[calc(100%-1rem)] text-left text-[15px] leading-6 font-normal text-[var(--text-tertiary)] sm:text-base"
              >
                {placeholders[currentPlaceholder]}
              </motion.p>
            )}
          </AnimatePresence>
        </div>
      </div>

      <div className="flex h-8 items-center justify-end px-3">
        <button
          disabled={!value}
          type="submit"
          aria-label={submitLabel}
          className="flex size-8 cursor-pointer items-center justify-center rounded-full bg-[var(--Button-black)] text-[var(--text-onblack)] transition-colors duration-150 hover:opacity-90 active:opacity-80 disabled:cursor-not-allowed disabled:bg-[var(--fill-tsp-white-dark)] disabled:text-[var(--text-tertiary)] disabled:opacity-50"
        >
          <ArrowUp size={18} aria-hidden />
        </button>
      </div>
    </form>
  );
}
