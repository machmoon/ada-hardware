// The bands in order (ada-site-ref/spec.md section 1). Header, announcement and hero share the first screen, as on
// manus.im, so the first scroll starts one viewport down; the header sits outside that wrapper so it stays sticky
// for the whole page.
import { Announcement } from "@/sections/Announcement";
import { BoardReveal } from "@/sections/BoardReveal";
import { Checks } from "@/sections/Checks";
import { Closing } from "@/sections/Closing";
import { Footer } from "@/sections/Footer";
import { Header } from "@/sections/Header";
import { Hero } from "@/sections/Hero";
import { SeeItWork } from "@/sections/SeeItWork";
import { TryIt } from "@/sections/TryIt";

export default function App() {
  return (
    <>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-4 focus:z-50 focus:rounded-[8px] focus:bg-[var(--Button-black)] focus:px-3 focus:py-2 focus:text-sm focus:text-[var(--text-onblack)]"
      >
        Skip to content
      </a>
      <Header />
      <main id="main" tabIndex={-1} className="outline-none">
        <div className="flex min-h-[calc(100svh-3.5rem)] flex-col">
          <Announcement />
          <Hero />
        </div>
        <BoardReveal />
        <Checks />
        <SeeItWork />
        <TryIt />
        <Closing />
      </main>
      <Footer />
    </>
  );
}
