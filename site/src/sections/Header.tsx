// Band 1: manus's sticky 56px header (manus-reference.md band 1), built from Aceternity's resizable-navbar.
import { BookOpen, LayoutGrid, ShieldCheck, Terminal } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import {
  MobileNav,
  MobileNavHeader,
  MobileNavMenu,
  MobileNavToggle,
  NavBody,
  NavItems,
  Navbar,
  NavbarButton,
} from "@/components/aceternity/resizable-navbar";
import { Mark } from "@/components/Mark";
import { DEMO_MAILTO, LINKS, NAV } from "@/content";

const DRAWER_ICONS = [ShieldCheck, LayoutGrid, Terminal, BookOpen];

function Logo() {
  return (
    <a href="#" aria-label="Ada home" className="flex w-fit items-center gap-2 rounded-[8px] text-[var(--text-primary)]">
      <Mark className="h-5 w-8" />
      <span className="font-serif text-[20px] leading-8 font-normal" aria-hidden="true">
        Ada
      </span>
    </a>
  );
}

// Below 360px (a 320px phone) the bar cannot hold the logo and both buttons, so GitHub steps out of the bar;
// the footer still links it.
function Buttons({ phone = false }: { phone?: boolean }) {
  return (
    <div className="flex items-center justify-end gap-2">
      <NavbarButton href={DEMO_MAILTO} variant="primary">
        Get a demo
      </NavbarButton>
      <NavbarButton href={LINKS.repo} variant="secondary" className={phone ? "max-[359px]:hidden" : undefined}>
        GitHub
      </NavbarButton>
    </div>
  );
}

export function Header() {
  const [open, setOpen] = useState(false);
  const toggle = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => {
    setOpen(false);
    toggle.current?.focus();
  }, []);

  return (
    <Navbar>
        <NavBody>
          <Logo />
          <nav aria-label="Main">
            <NavItems items={NAV} />
          </nav>
          <Buttons />
        </NavBody>

        <MobileNav>
          <MobileNavHeader>
            <Logo />
            <div className="flex items-center gap-3">
              <Buttons phone />
              <MobileNavToggle
                ref={toggle}
                isOpen={open}
                onClick={() => setOpen((o) => !o)}
                controls="mobile-menu"
              />
            </div>
          </MobileNavHeader>
          <MobileNavMenu id="mobile-menu" isOpen={open} onClose={close}>
            <nav aria-label="Main" className="flex flex-col py-2">
              {NAV.map((item, i) => {
                const Icon = DRAWER_ICONS[i];
                return (
                  <a
                    key={item.name}
                    href={item.link}
                    onClick={close}
                    className="flex items-center gap-3 px-4 py-3 text-base leading-6 font-medium text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)] sm:px-6"
                  >
                    <span className="flex size-10 items-center justify-center rounded-[8px] bg-[var(--fill-tsp-white-light)] p-[10px]">
                      <Icon size={20} aria-hidden className="text-[var(--text-secondary)]" />
                    </span>
                    {item.name}
                  </a>
                );
              })}
            </nav>
          </MobileNavMenu>
        </MobileNav>
    </Navbar>
  );
}
