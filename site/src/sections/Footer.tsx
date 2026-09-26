// Band 4: manus's dark footer (manus-reference.md band 4): 100px padding, 48px rhythm, italic serif tagline, a
// 7-track link grid (Ada fills three; it adds no links for show), 60%-white links with Skiper-derived underlines.
import { InkLink } from "@/components/skiper/InkLink";
import { FOOTER, LINKS } from "@/content";
import { ThemeButton } from "./ThemeButton";

export function Footer() {
  return (
    <footer className="bg-[var(--footer)] dark:border-t dark:border-[var(--border-main)]">
      <div className="mx-auto max-w-[1080px] space-y-12 px-4 py-[100px] sm:px-6">
        <h2 className="font-serif text-4xl leading-[44px] font-normal text-white italic">
          {FOOTER.tagline[0]}
          <br />
          {FOOTER.tagline[1]}
        </h2>
        <nav aria-label="Footer" className="grid grid-cols-2 gap-6 md:grid-cols-7">
          {FOOTER.columns.map((col) => (
            <div key={col.heading} className="space-y-2">
              <h3 className="text-sm leading-5 font-medium text-white">{col.heading}</h3>
              {/* 14/20 on the list too, so each li is the link's 20px line and the pitch is manus's 28 (20 + 8) */}
              <ul className="space-y-2 text-sm leading-5">
                {col.links.map((l) => (
                  <li key={l.name}>
                    <InkLink href={l.href} className="text-sm leading-5 text-[var(--text-white-tsp)] hover:text-white">
                      {l.name}
                    </InkLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
        <div className="flex items-center justify-between gap-6 max-sm:flex-col max-sm:items-start">
          <p className="text-sm leading-[21px] text-white/60">
            {FOOTER.copyright} · UI components from{" "}
            <a href={LINKS.aceternity} className="underline underline-offset-2 hover:text-white">
              Aceternity UI
            </a>{" "}
            and{" "}
            <a href={LINKS.skiper} className="underline underline-offset-2 hover:text-white">
              Skiper UI
            </a>
          </p>
          <ThemeButton />
        </div>
      </div>
    </footer>
  );
}
