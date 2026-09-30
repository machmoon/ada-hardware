import { useState } from "react";
import { CheckIcon, ClockIcon, ExternalLinkIcon, FolderOpenIcon, StarIcon, XIcon } from "lucide-react";
import { openUrl, revealItemInDir } from "@tauri-apps/plugin-opener";
import { Badge, Button } from "@/components";
import { leadTimeText, priceAmount } from "@/lib/silkscreen/steps";
import type { FabHouseGroup, OrderPanel } from "@/lib/silkscreen/steps";
import type { FabHouseCard } from "@/lib/silkscreen/types";
import { cn } from "@/lib/utils";

/**
 * "Order your board": one card per fab house, after the order step.
 *
 * Layout after Kitspace's "Order PCBs" menu (kitspace/kitspace-v2
 * `frontend/src/components/Board/OrderPCBs.jsx` at f7adaab, AGPL-3.0, design
 * only): the package beside one outbound link per fab. The deviation is that
 * each card carries the engine's own verdict and price, because the engine
 * already ran each house's capability check and OSH Park's published rule.
 *
 * Every number on a card is one the engine sent. A house with no published
 * price shows "Quote on <house>" and no figure. The buttons open the house's
 * own page in the default browser and reveal the files on disk; nothing here
 * places or pays for an order.
 */

function errorText(caught: unknown): string {
  if (caught instanceof Error) return caught.message;
  return typeof caught === "string" ? caught : String(caught ?? "unknown error");
}

const HouseCard = ({
  group,
  onOpen,
}: {
  group: FabHouseGroup;
  onOpen: (card: FabHouseCard) => void;
}) => {
  const card = group.primary;
  const amount = priceAmount(card);
  const lead = leadTimeText(card);
  return (
    <li
      className={cn(
        "relative flex min-w-0 flex-col gap-1.5 rounded-lg border p-2.5",
        group.recommended
          ? "border-emerald-500/70 bg-emerald-500/5 ring-1 ring-emerald-500/40"
          : "border-input/50"
      )}
      data-testid="fab-house"
      data-house={group.house}
      data-buildable={String(card.buildable)}
      data-recommended={String(group.recommended)}
    >
      <div className="flex items-start justify-between gap-1">
        <div className="min-w-0">
          <p className="truncate text-[13px] font-semibold leading-tight">{group.house}</p>
          <p className="truncate text-[10px] text-muted-foreground">{card.service}</p>
        </div>
        {group.recommended ? (
          <Badge
            variant="outline"
            className="h-4 shrink-0 gap-0.5 border-emerald-500/60 px-1 text-[9px] text-emerald-700 dark:text-emerald-400"
            data-testid="fab-recommended"
          >
            <StarIcon className="size-2.5" />
            Recommended
          </Badge>
        ) : null}
      </div>

      {card.buildable ? (
        <p
          className="flex items-center gap-1 text-[11px] font-medium text-emerald-700 dark:text-emerald-400"
          data-testid="fab-buildable"
        >
          <CheckIcon className="size-3.5" />
          Builds this board
        </p>
      ) : (
        <div data-testid="fab-not-buildable">
          <p className="flex items-center gap-1 text-[11px] font-medium text-destructive">
            <XIcon className="size-3.5" />
            Won't build it
          </p>
          <ul className="mt-0.5 flex flex-col gap-0.5">
            {card.blockers.map((b, index) => (
              <li
                key={`${b.code ?? "blocker"}-${index}`}
                className="text-[10px] leading-tight text-muted-foreground"
                title={b.detail ?? undefined}
                data-testid="fab-reason"
              >
                {b.title ?? b.code}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-auto" data-testid="fab-price">
        {amount && card.price ? (
          <>
            <p className="text-2xl font-bold leading-none tracking-tight" data-testid="fab-price-amount">
              {amount}
            </p>
            <p className="mt-0.5 text-[10px] text-muted-foreground">
              {card.price.boards} {card.price.boards === 1 ? "board" : "boards"}
              {card.price.shipping_cents === 0 ? " · shipping included" : ""}
            </p>
          </>
        ) : (
          <p className="text-[13px] font-medium leading-tight" data-testid="fab-price-note" title={card.unpriced_reason ?? undefined}>
            {card.price_note ?? `Quote on ${group.house}`}
          </p>
        )}
      </div>

      {lead ? (
        <p className="flex items-center gap-1 text-[10px] text-muted-foreground" data-testid="fab-lead-time">
          <ClockIcon className="size-3" />
          {lead} to build
        </p>
      ) : null}

      {group.others.map((other) => (
        <p key={other.id} className="text-[10px] leading-tight text-muted-foreground" data-testid="fab-alternative">
          {other.service}: {other.price ? other.price.text : other.price_note}
          {leadTimeText(other) ? `, ${leadTimeText(other)}` : ""}
        </p>
      ))}

      {card.quote_url ? (
        <Button
          size="sm"
          variant={group.recommended ? "default" : "outline"}
          className="h-7 w-full text-[11px]"
          onClick={() => onOpen(card)}
          data-testid="fab-open"
          data-house={group.house}
        >
          <ExternalLinkIcon className="size-3.5" />
          Open {group.house}
        </Button>
      ) : null}
    </li>
  );
};

export const OrderYourBoard = ({ panel, zip }: { panel: OrderPanel; zip: string | null }) => {
  const [note, setNote] = useState<string | null>(null);

  const open = (card: FabHouseCard) => {
    const target = card.quote_url;
    if (!target) return;
    setNote(null);
    openUrl(target).catch((caught) =>
      setNote(`No browser opened ${card.house} (${errorText(caught)}). It is at ${target}`)
    );
  };

  const reveal = () => {
    if (!zip) return;
    setNote(null);
    revealItemInDir(zip).catch((caught) =>
      setNote(`Could not reveal the order files (${errorText(caught)}). They are at ${zip}`)
    );
  };

  return (
    <section className="flex flex-col gap-2 rounded-lg border border-input/50 p-2.5" data-testid="order-panel">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold leading-tight">Order your board</h3>
          {panel.recommendedReason ? (
            <p className="text-[10px] text-muted-foreground" data-testid="order-panel-reason">
              {panel.recommendedReason}
            </p>
          ) : null}
        </div>
        {zip ? (
          <Button size="sm" variant="outline" className="h-7 shrink-0 text-[11px]" onClick={reveal} data-testid="order-panel-reveal">
            <FolderOpenIcon className="size-3.5" />
            Reveal order files
          </Button>
        ) : null}
      </div>

      <ul className="grid grid-cols-3 gap-2" data-testid="fab-houses">
        {panel.houses.map((group) => (
          <HouseCard key={group.house} group={group} onOpen={open} />
        ))}
      </ul>

      <p className="text-[10px] text-muted-foreground" data-testid="order-panel-boundary">
        {panel.boundary}
      </p>

      {note ? (
        <p className="break-all text-[11px] text-muted-foreground" data-testid="order-panel-note">
          {note}
        </p>
      ) : null}
    </section>
  );
};
