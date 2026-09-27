// The footer's theme control, in manus's language-button box (h-9 px-3 py-1.5 radius 8, --border-white, weight 510).
// Cycles System -> Light -> Dark. "System" removes the saved choice, and the head script in index.html then follows
// prefers-color-scheme again. Same storage key as the old page's stars.js: "ada-theme".
import { Moon, Sun, SunMoon } from "lucide-react";
import { useState } from "react";

type Choice = "system" | "light" | "dark";
const KEY = "ada-theme";
const ORDER: Choice[] = ["system", "light", "dark"];
const LABEL = { system: "System", light: "Light", dark: "Dark" } as const;
const ICON = { system: SunMoon, light: Sun, dark: Moon } as const;

function readChoice(): Choice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

function apply(choice: Choice) {
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    /* storage blocked: the choice lasts for this page only */
  }
  const dark =
    choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

export function ThemeButton() {
  const [choice, setChoice] = useState<Choice>(readChoice);
  const Icon = ICON[choice];
  return (
    <button
      type="button"
      onClick={() => {
        const next = ORDER[(ORDER.indexOf(choice) + 1) % ORDER.length];
        apply(next);
        setChoice(next);
      }}
      aria-label={`Theme: ${LABEL[choice]}. Change theme`}
      className="inline-flex h-9 cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-white)] px-3 py-1.5 text-sm leading-5 font-[510] text-white hover:bg-white/[.06] motion-safe:transition-colors"
    >
      <Icon size={20} aria-hidden />
      {LABEL[choice]}
    </button>
  );
}
