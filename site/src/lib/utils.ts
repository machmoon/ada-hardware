import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** The `cn()` helper every shadcn-registry component imports from `@/lib/utils`. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
