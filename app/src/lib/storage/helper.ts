/**
 * `localStorage` with the failure modes named. A read that fails is `null`
 * (nothing stored is the honest reading). A write that fails returns
 * `false` -- quota, a private window, storage switched off -- so a caller
 * that just flipped a user-visible preference can say "could not save"
 * instead of showing a switch that will be back to its old position on the
 * next launch. Nothing here throws.
 */
export const safeLocalStorage = {
  getItem: (key: string): string | null => {
    if (typeof window === "undefined") return null;
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  /** True when the value is stored; false when the browser refused. */
  setItem: (key: string, value: string): boolean => {
    if (typeof window === "undefined") return false;
    try {
      localStorage.setItem(key, value);
      return true;
    } catch (error) {
      console.warn(`[ada storage] could not save ${key}:`, error);
      return false;
    }
  },
  /** True when the key is gone (or never was); false when the browser refused. */
  removeItem: (key: string): boolean => {
    if (typeof window === "undefined") return false;
    try {
      localStorage.removeItem(key);
      return true;
    } catch (error) {
      console.warn(`[ada storage] could not remove ${key}:`, error);
      return false;
    }
  },
};
