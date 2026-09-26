// The middle bands follow manus's feature page (https://manus.im/features/webapp, saved as
// ada-site-ref/manus/features-webapp.html). These class lists are copied from every middle band there:
//   outer  box-border flex w-full flex-col items-center gap-12 px-6 py-16 md:py-20
//   inner  flex w-full max-w-[1032px] flex-col items-center gap-12
//   header flex w-full flex-col items-center gap-3 text-center max-w-[680px]
//   h2     font-serif text-[28px] font-[600] leading-[1.2] md:text-[40px] text-[var(--text-primary)]
//   p      text-base font-normal leading-6 text-[var(--text-secondary)]
// One deviation: the gutter is 16px below 640px (px-4 sm:px-6), per the site brief.
import { cn } from "@/lib/utils";

export function Band({
  id,
  labelledBy,
  children,
  className,
}: {
  id?: string;
  labelledBy: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      id={id}
      aria-labelledby={labelledBy}
      className={cn("box-border flex w-full flex-col items-center gap-12 px-4 py-16 sm:px-6 md:py-20", className)}
    >
      <div className="flex w-full max-w-[1032px] flex-col items-center gap-12">{children}</div>
    </section>
  );
}

export function BandHeader({ id, title, body }: { id: string; title: string; body: string }) {
  return (
    <div className="flex w-full max-w-[680px] flex-col items-center gap-3 text-center">
      <h2 id={id} className="font-serif text-[28px] leading-[1.2] font-[600] text-[var(--text-primary)] md:text-[40px]">
        {title}
      </h2>
      <p className="text-base leading-6 font-normal text-[var(--text-secondary)]">{body}</p>
    </div>
  );
}

/** FEAT's black pill button ("freedom & control" and CTA bands). */
export function PillLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      className="inline-flex h-[40px] min-w-[80px] items-center justify-center gap-[6px] rounded-full bg-[var(--Button-black)] px-[16px] text-sm font-medium text-[var(--text-onblack)] transition-colors duration-150 hover:opacity-90 active:opacity-80"
    >
      {children}
    </a>
  );
}
