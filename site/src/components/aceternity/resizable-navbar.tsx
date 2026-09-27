// Source: https://ui.aceternity.com/registry/resizable-navbar.json (fetched 2026-09-25)
// Aceternity UI free component, used under the Aceternity License (https://ui.aceternity.com/licence).
// NOT covered by this repository's MIT licence. Modified for Ada:
//  1. The scroll-driven `visible` state and its animation (width 40%, y 20, blur, pill) are removed, with the
//     inline minWidth 800px: manus's bar does not change on scroll.
//  2. Navbar: `sticky top-20 z-40` -> `sticky top-0 z-20` on --background-gray-main, rendered as the page's <header>
//     landmark (a plain element; nothing animates).
//  3. NavBody: manus's 1080 box, h-14 px-6 py-3, grid-cols-[1fr_auto_1fr] from md (768), not lg.
//  4. NavItems: manus's item (px-3 py-1.5 rounded 8, 14/20 500, --text-secondary); the layoutId="hovered" pill is
//     kept and restyled to --fill-tsp-white-main; it sits in the grid's middle track instead of absolute inset-0;
//     keyboard focus shows the same fill (onFocus).
//  5. NavbarButton: primary and secondary rewritten to manus's header buttons (32px, radius 8, 14/18 500); the hover
//     lift, the shadow and the `dark` and `gradient` variants are deleted.
//  6. MobileNavToggle: lucide Menu/X inside a real <button aria-label aria-expanded aria-controls> (upstream put
//     onClick on a bare icon); @tabler/icons-react is no longer imported. aria-controls is set only while the drawer
//     is open, because the closed drawer is unmounted and an IDREF to nothing is a broken reference; Headless UI's
//     Disclosure does the same (packages/@headlessui-react/src/components/disclosure/disclosure.tsx:403 at fe9ec17,
//     `'aria-controls': state.panelElement ? state.panelId : undefined`).
//  7. MobileNavMenu: manus's full-screen drawer (fixed top-14 inset-x-0 bottom-0), menu-slide-down 0.35s
//     cubic-bezier(.32,.72,0,1) from (opacity 0, y -8); Esc closes it and focus returns to the toggle.
//  8. NavbarLogo (Aceternity's placeholder logo and "Startup") is deleted; Ada's header passes its own mark.
//  "use client" is removed (no RSC here).
import { cn } from "@/lib/utils";
import { Menu, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import React, { useEffect, useRef, useState } from "react";

interface NavbarProps {
  children: React.ReactNode;
  className?: string;
}

interface NavBodyProps {
  children: React.ReactNode;
  className?: string;
}

interface NavItemsProps {
  items: readonly {
    name: string;
    link: string;
  }[];
  className?: string;
  onItemClick?: () => void;
}

interface MobileNavProps {
  children: React.ReactNode;
  className?: string;
}

interface MobileNavHeaderProps {
  children: React.ReactNode;
  className?: string;
}

interface MobileNavMenuProps {
  id: string;
  children: React.ReactNode;
  className?: string;
  isOpen: boolean;
  onClose: () => void;
}

export const Navbar = ({ children, className }: NavbarProps) => {
  return (
    <header
      className={cn(
        "sticky inset-x-0 top-0 z-20 w-full bg-[var(--background-gray-main)]",
        className,
      )}
    >
      {children}
    </header>
  );
};

export const NavBody = ({ children, className }: NavBodyProps) => {
  return (
    <div
      className={cn(
        "relative mx-auto hidden h-14 w-full max-w-[1080px] items-center px-6 py-3 md:grid md:grid-cols-[1fr_auto_1fr]",
        className,
      )}
    >
      {children}
    </div>
  );
};

export const NavItems = ({ items, className, onItemClick }: NavItemsProps) => {
  const [hovered, setHovered] = useState<number | null>(null);

  return (
    <div
      onMouseLeave={() => setHovered(null)}
      onBlur={() => setHovered(null)}
      className={cn("flex flex-row items-center justify-center gap-2", className)}
    >
      {items.map((item, idx) => (
        <a
          onMouseEnter={() => setHovered(idx)}
          onFocus={() => setHovered(idx)}
          onClick={onItemClick}
          className="relative rounded-[8px] px-3 py-1.5 text-sm leading-5 font-medium text-[var(--text-secondary)]"
          key={`link-${idx}`}
          href={item.link}
        >
          {hovered === idx && (
            <motion.div
              layoutId="hovered"
              className="absolute inset-0 h-full w-full rounded-[8px] bg-[var(--fill-tsp-white-main)]"
            />
          )}
          <span className="relative z-20">{item.name}</span>
        </a>
      ))}
    </div>
  );
};

export const MobileNav = ({ children, className }: MobileNavProps) => {
  return (
    <div className={cn("relative flex w-full flex-col md:hidden", className)}>{children}</div>
  );
};

export const MobileNavHeader = ({ children, className }: MobileNavHeaderProps) => {
  return (
    <div
      className={cn(
        "flex h-14 w-full flex-row items-center justify-between px-4 sm:px-6",
        className,
      )}
    >
      {children}
    </div>
  );
};

export const MobileNavMenu = ({ id, children, className, isOpen, onClose }: MobileNavMenuProps) => {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    ref.current?.querySelector<HTMLElement>("a,button")?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div
          ref={ref}
          id={id}
          initial={{ opacity: 0, y: -8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.35, ease: [0.32, 0.72, 0, 1] }}
          className={cn(
            "fixed inset-x-0 top-14 bottom-0 z-50 flex w-full flex-col overflow-y-auto bg-[var(--background-gray-main)]",
            className,
          )}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
};

export const MobileNavToggle = React.forwardRef<
  HTMLButtonElement,
  { isOpen: boolean; onClick: () => void; controls: string }
>(({ isOpen, onClick, controls }, ref) => {
  return (
    <button
      ref={ref}
      type="button"
      onClick={onClick}
      aria-label={isOpen ? "Close menu" : "Open menu"}
      aria-expanded={isOpen}
      aria-controls={isOpen ? controls : undefined}
      className="inline-flex size-8 items-center justify-center rounded-[8px] text-[var(--text-primary)]"
    >
      {isOpen ? <X size={24} aria-hidden /> : <Menu size={24} aria-hidden />}
    </button>
  );
});
MobileNavToggle.displayName = "MobileNavToggle";

export const NavbarButton = ({
  href,
  as: Tag = "a",
  children,
  className,
  variant = "primary",
  ...props
}: {
  href?: string;
  as?: React.ElementType;
  children: React.ReactNode;
  className?: string;
  variant?: "primary" | "secondary";
} & (React.ComponentPropsWithoutRef<"a"> | React.ComponentPropsWithoutRef<"button">)) => {
  const baseStyles =
    "relative inline-flex h-8 min-w-[64px] cursor-pointer items-center justify-center gap-1 rounded-[8px] px-2 text-center text-sm leading-[18px] font-medium whitespace-nowrap transition-colors duration-150 ease-[cubic-bezier(.4,0,.2,1)]";

  const variantStyles = {
    primary:
      "bg-[var(--Button-black)] text-[var(--text-onblack)] hover:opacity-90 active:opacity-80",
    secondary:
      "bg-transparent text-[var(--text-primary)] outline-1 -outline-offset-1 outline-[var(--Button-border-secondary)] hover:bg-[var(--fill-tsp-white-light)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--text-primary)]",
  };

  return (
    <Tag
      href={href || undefined}
      className={cn(baseStyles, variantStyles[variant], className)}
      {...props}
    >
      {children}
    </Tag>
  );
};
