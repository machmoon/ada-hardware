// Band M3, #see-it-work: Aceternity's bento-grid in manus's feature-card materials, with Aceternity's tabs in the
// review tile. Tile text is verbatim from the old page (site/index.html before the rebuild).
import { MessageSquareWarning, PanelTop, ReceiptText, RefreshCcw, Route } from "lucide-react";
import board from "../../assets/board.png";
import strip from "../../assets/strip.png";
import { Band, BandHeader } from "./Band";
import { BentoGrid, BentoGridItem } from "@/components/aceternity/bento-grid";
import { Tabs } from "@/components/aceternity/tabs";
import { SEE } from "@/content";
import { cn } from "@/lib/utils";

// flex-1: the window fills the tile down to its title block, as the header Skeleton in Aceternity's own demo does
// (`flex flex-1 w-full h-full min-h-[6rem]`, https://ui.aceternity.com/registry/bento-grid-demo.json). A tile that
// shares its row with a taller neighbour then shows a taller window, not a blank gap above its title.
const WINDOW =
  "flex-1 overflow-hidden rounded-[12px] border border-[var(--border-main)] bg-[var(--background-gray-main)]";
const ICON = "size-5 text-[var(--text-tertiary)]";

function Receipt() {
  const r = SEE.receipt;
  return (
    <div className={WINDOW}>
      <div className="flex gap-4 border-b border-[var(--border-main)] px-4 text-sm leading-5">
        {r.tabs.map((t) => (
          <span
            key={t.label}
            className={cn(
              "-mb-px border-b-2 py-2.5",
              t.active
                ? "border-[var(--text-primary)] font-medium text-[var(--text-primary)]"
                : "border-transparent text-[var(--text-secondary)]",
            )}
          >
            {t.label} <span className="tabular-nums">{t.count}</span>
          </span>
        ))}
      </div>
      <ul>
        {r.rows.map((row) => (
          <li
            key={row.id}
            className="grid min-h-10 grid-cols-[auto_auto_1fr] items-center gap-3 border-b border-[var(--border-main)] px-4 py-2 last:border-b-0 hover:bg-[var(--fill-tsp-white-light)]"
          >
            <span className="rounded-[4px] bg-[var(--function-success-tsp)] px-1.5 font-mono text-xs leading-4 font-semibold text-[var(--function-success)]">
              PASS
            </span>
            <span className="font-mono text-xs leading-4 font-semibold text-[var(--text-primary)]">{row.id}</span>
            <span className="min-w-0 text-sm leading-5 text-[var(--text-secondary)]">{row.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function RoundLog() {
  const colour = {
    dim: "text-[var(--text-secondary)]",
    bad: "text-[var(--function-error)]",
    good: "text-[var(--function-success)]",
    plain: "text-[var(--text-primary)]",
  } as const;
  return (
    <pre className={cn(WINDOW, "p-4 font-mono text-xs leading-[19px] whitespace-pre-wrap text-[var(--text-primary)]")}>
      {SEE.fixed.log.map((l, i) => (
        <span key={i} className="block">
          {l.kind === "bad" && <span className={colour.bad}>✗ </span>}
          {l.kind === "good" && <span className={colour.good}>✓ </span>}
          <span className={l.kind === "dim" ? colour.dim : colour.plain}>{l.text || " "}</span>
        </span>
      ))}
    </pre>
  );
}

function Finding({ title, body, fix }: { title: string; body: string; fix: string }) {
  return (
    <article className="flex h-full flex-col gap-2 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-6">
      <p className="flex items-start gap-2 text-sm leading-5 font-medium text-[var(--text-primary)]">
        <span className="mt-0.5 shrink-0 rounded-[4px] bg-[var(--function-error-tsp)] px-1.5 font-mono text-xs leading-4 font-semibold text-[var(--function-error)]">
          HIGH
        </span>
        <span>{title}</span>
      </p>
      <p className="text-sm leading-5 text-[var(--text-secondary)]">{body}</p>
      <p className="font-mono text-xs leading-[18px] text-[var(--text-primary)]">{fix}</p>
    </article>
  );
}

export function SeeItWork() {
  return (
    <Band id="see-it-work" labelledBy="see-title">
      <BandHeader id="see-title" title={SEE.title} body={SEE.body} />
      <BentoGrid>
        <BentoGridItem
          className="md:col-span-2"
          header={<Receipt />}
          icon={<ReceiptText aria-hidden className={ICON} />}
          title={SEE.receipt.title}
          description={SEE.receipt.body}
        />
        <BentoGridItem
          header={
            <div className={cn(WINDOW, "flex items-center p-4")}>
              <img
                src={strip}
                alt={SEE.kicad.imageAlt}
                width={1200}
                height={108}
                loading="lazy"
                decoding="async"
                className="h-auto w-full"
              />
            </div>
          }
          icon={<PanelTop aria-hidden className={ICON} />}
          title={SEE.kicad.title}
          description={SEE.kicad.body}
        />
        <BentoGridItem
          header={<RoundLog />}
          icon={<RefreshCcw aria-hidden className={ICON} />}
          title={SEE.fixed.title}
          description={SEE.fixed.body}
        />
        <BentoGridItem
          className="md:col-span-2"
          header={
            <div className={WINDOW}>
              <img
                src={board}
                alt={SEE.routed.imageAlt}
                width={1950}
                height={1338}
                loading="lazy"
                decoding="async"
                className="h-auto w-full"
              />
            </div>
          }
          icon={<Route aria-hidden className={ICON} />}
          title={SEE.routed.title}
          description={SEE.routed.body}
        />
        <BentoGridItem
          className="md:col-span-3"
          header={
            <div className="relative flex flex-col">
              <Tabs
                label="Review findings"
                tabs={SEE.review.findings.map((f) => ({
                  title: f.tab,
                  value: f.tab,
                  content: <Finding title={f.title} body={f.body} fix={f.fix} />,
                }))}
                // Back cards lift idx*fan and shrink toward their centre by (1 - scale)/2 of the card height. At
                // 14px the third card on the shortest (1440px, 124px) card tops out 16px up, level with the tab
                // row's bottom edge, so the fan never covers a tab.
                fan={14}
                containerClassName="gap-2"
                tabClassName="h-8 px-3 py-0 rounded-[8px] text-sm"
                activeTabClassName="rounded-[8px] bg-[var(--fill-tsp-white-main)]"
                contentClassName="mt-4"
              />
            </div>
          }
          icon={<MessageSquareWarning aria-hidden className={ICON} />}
          title={SEE.review.title}
          description={
            <>
              <p>{SEE.review.body}</p>
              <p className="mt-2">{SEE.review.caption}</p>
            </>
          }
        />
      </BentoGrid>
    </Band>
  );
}
