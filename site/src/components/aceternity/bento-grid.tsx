// Source: https://ui.aceternity.com/registry/bento-grid.json (fetched 2026-09-25)
// Aceternity UI free component, used under the Aceternity License (https://ui.aceternity.com/licence).
// NOT covered by this repository's MIT licence. Modified for Ada:
//  1. BentoGrid: `max-w-7xl gap-4 md:auto-rows-[18rem]` -> `max-w-[1032px] gap-6 md:auto-rows-[minmax(22rem,auto)]`
//     (manus's 1032 content width and 24px gap; the receipt tile needs about 320px).
//  2. BentoGridItem takes manus's feature-card materials: radius 12, --background-menu-white, a --border-main
//     hairline, p-6 and no shadow (upstream: neutral-200 border, shadow-input, hover:shadow-xl). The title is an <h3>
//     at 16/20 500 --text-primary (upstream a bold neutral-600 div); the description is 14/20 --text-secondary
//     (upstream text-xs). The hover shift group-hover/bento:translate-x-2 is kept, gated motion-safe.
//  3. The header is a flex child of the tile: pass it `flex-1` (SeeItWork's WINDOW does) and it fills the tile down to
//     the title block, as the Skeleton header in Aceternity's bento-grid-demo does (`flex flex-1 w-full h-full
//     min-h-[6rem]`). Otherwise `justify-between` leaves a blank band between a short visual and its title whenever a
//     taller neighbour sets the row height.
//  The registry declares @tabler/icons-react, but this file never imports it; it is not installed.
import { cn } from "@/lib/utils";

export const BentoGrid = ({
  className,
  children,
}: {
  className?: string;
  children?: React.ReactNode;
}) => {
  return (
    <div
      className={cn(
        "mx-auto grid w-full max-w-[1032px] grid-cols-1 gap-6 md:auto-rows-[minmax(22rem,auto)] md:grid-cols-3",
        className,
      )}
    >
      {children}
    </div>
  );
};

export const BentoGridItem = ({
  className,
  title,
  description,
  header,
  icon,
}: {
  className?: string;
  title?: string | React.ReactNode;
  description?: string | React.ReactNode;
  header?: React.ReactNode;
  icon?: React.ReactNode;
}) => {
  return (
    <div
      className={cn(
        "group/bento row-span-1 flex min-w-0 flex-col justify-between space-y-4 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-6",
        className,
      )}
    >
      {header}
      <div className="motion-safe:transition motion-safe:duration-200 motion-safe:group-hover/bento:translate-x-2">
        {icon}
        <h3 className="mt-2 mb-2 font-sans text-base leading-5 font-medium text-[var(--text-primary)]">
          {title}
        </h3>
        <div className="font-sans text-sm leading-5 font-normal text-[var(--text-secondary)]">
          {description}
        </div>
      </div>
    </div>
  );
};
