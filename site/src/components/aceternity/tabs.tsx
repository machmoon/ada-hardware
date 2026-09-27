// Source: https://ui.aceternity.com/registry/tabs.json (fetched 2026-09-25)
// Aceternity UI free component, used under the Aceternity License (https://ui.aceternity.com/licence).
// NOT covered by this repository's MIT licence. Modified for Ada:
//  1. ARIA tabs: role="tablist" on the row, role="tab" + aria-selected + aria-controls + a roving tabIndex on the
//     buttons, role="tabpanel" on the front card, and aria-hidden plus inert on the cards stacked behind it.
//  2. Left/Right (and Home/End) move between tabs and select them.
//  3. `contentClassName` is applied once, to the stack's container (upstream put mt-32 on every card, sized for a
//     full-page demo). The cards are stacked in one grid cell (col-start-1 row-start-1, position relative) instead
//     of upstream's `absolute top-0 left-0 h-full w-full`, so the stack is as tall as its tallest card and needs no
//     fixed-height wrapper: the tab row wraps on a phone (item 5) and the card text reflows with the width, and a
//     fixed height clipped the card at 320px. `isolate` keeps the negative z-index of the cards behind the front one
//     inside the stack instead of behind the tile's background.
//  4. The hover fan-out offset is a prop, `fan` (upstream 50px per card). With the stack 16px under the row, 50px
//     would lift the back cards over the tab buttons and steal the hover that raised them.
//  5. Tab labels use --text-primary and do not wrap. Below sm the row wraps onto a second line instead of scrolling
//     inside itself, so no tab is cut off at the tile edge behind a hidden scrollbar (upstream: overflow-auto).
//  6. Tab and panel ids are built from the tab's index, not its value (the value may hold spaces).
//  The registry declares @radix-ui/react-tabs, but the file never imports it; it is not installed.
//  "use client" removed.
import { useId, useRef, useState } from "react";
import { motion } from "motion/react";
import { cn } from "@/lib/utils";

type Tab = {
  title: string;
  value: string;
  content?: React.ReactNode;
};

export const Tabs = ({
  tabs: propTabs,
  containerClassName,
  activeTabClassName,
  tabClassName,
  contentClassName,
  label,
  fan = 50,
}: {
  tabs: Tab[];
  containerClassName?: string;
  activeTabClassName?: string;
  tabClassName?: string;
  contentClassName?: string;
  label?: string;
  fan?: number;
}) => {
  const [active, setActive] = useState<Tab>(propTabs[0]);
  const [tabs, setTabs] = useState<Tab[]>(propTabs);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const uid = useId();
  // Ids come from the tab's position in `tabs` as passed, never from its value: a value is free text ("U1 · J2
  // serial header"), and whitespace in an id splits aria-controls/aria-labelledby into several IDREFs that resolve
  // to nothing.
  const indexOf = (t: Tab) => propTabs.findIndex((p) => p.value === t.value);
  const tabId = (t: Tab) => `${uid}-tab-${indexOf(t)}`;
  const panelId = (t: Tab) => `${uid}-panel-${indexOf(t)}`;

  const moveSelectedTabToTop = (idx: number) => {
    const newTabs = [...propTabs];
    const selectedTab = newTabs.splice(idx, 1);
    newTabs.unshift(selectedTab[0]);
    setTabs(newTabs);
    setActive(newTabs[0]);
  };

  const onKeyDown = (e: React.KeyboardEvent, idx: number) => {
    const last = propTabs.length - 1;
    const next =
      e.key === "ArrowRight" ? (idx === last ? 0 : idx + 1)
      : e.key === "ArrowLeft" ? (idx === 0 ? last : idx - 1)
      : e.key === "Home" ? 0
      : e.key === "End" ? last
      : null;
    if (next === null) return;
    e.preventDefault();
    moveSelectedTabToTop(next);
    buttons.current[next]?.focus();
  };

  const [hovering, setHovering] = useState(false);

  return (
    <>
      <div
        role="tablist"
        aria-label={label}
        className={cn(
          "relative flex w-full max-w-full flex-row flex-wrap items-center justify-start [perspective:1000px] sm:flex-nowrap",
          containerClassName,
        )}
      >
        {propTabs.map((tab, idx) => (
          <button
            key={tab.title}
            ref={(el) => {
              buttons.current[idx] = el;
            }}
            type="button"
            role="tab"
            id={tabId(tab)}
            aria-selected={active.value === tab.value}
            aria-controls={panelId(tab)}
            tabIndex={active.value === tab.value ? 0 : -1}
            onClick={() => {
              moveSelectedTabToTop(idx);
            }}
            onKeyDown={(e) => onKeyDown(e, idx)}
            onMouseEnter={() => setHovering(true)}
            onMouseLeave={() => setHovering(false)}
            className={cn("relative shrink-0 cursor-pointer rounded-full px-4 py-2", tabClassName)}
            style={{
              transformStyle: "preserve-3d",
            }}
          >
            {active.value === tab.value && (
              <motion.div
                layoutId="clickedbutton"
                transition={{ type: "spring", bounce: 0.3, duration: 0.6 }}
                className={cn(
                  "absolute inset-0 rounded-full bg-gray-200 dark:bg-zinc-800",
                  activeTabClassName,
                )}
              />
            )}

            <span className="relative block whitespace-nowrap text-[var(--text-primary)]">
              {tab.title}
            </span>
          </button>
        ))}
      </div>
      <FadeInDiv
        tabs={tabs}
        active={active}
        key={active.value}
        hovering={hovering}
        fan={fan}
        tabId={tabId}
        panelId={panelId}
        className={contentClassName}
      />
    </>
  );
};

export const FadeInDiv = ({
  className,
  tabs,
  hovering,
  fan,
  tabId,
  panelId,
}: {
  className?: string;
  key?: string;
  tabs: Tab[];
  active: Tab;
  hovering?: boolean;
  fan: number;
  tabId: (t: Tab) => string;
  panelId: (t: Tab) => string;
}) => {
  const isActive = (tab: Tab) => {
    return tab.value === tabs[0].value;
  };
  return (
    <div className={cn("relative isolate grid w-full", className)}>
      {tabs.map((tab, idx) => (
        <motion.div
          key={tab.value}
          layoutId={tab.value}
          id={panelId(tab)}
          role={isActive(tab) ? "tabpanel" : undefined}
          aria-labelledby={isActive(tab) ? tabId(tab) : undefined}
          aria-hidden={isActive(tab) ? undefined : true}
          inert={!isActive(tab)}
          style={{
            scale: 1 - idx * 0.1,
            top: hovering ? idx * -fan : 0,
            zIndex: -idx,
            opacity: idx < 3 ? 1 - idx * 0.1 : 0,
          }}
          animate={{
            y: isActive(tab) ? [0, 40, 0] : 0,
          }}
          className="relative col-start-1 row-start-1 w-full"
        >
          {tab.content}
        </motion.div>
      ))}
    </div>
  );
};
