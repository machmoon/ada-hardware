// @vitest-environment jsdom
//
// "Order your board": one card per fab house after the order step. What is
// pinned here is the honesty of each card -- a price only where the engine
// sent one, the capability check's own reasons when a house will not build
// it, one recommended pick -- and that the buttons only open the house's page
// and reveal the files. The fixture is `service/fabhouses.py`'s output for a
// 45.4 x 77.65 mm outline (the ESP32 demo board), JLCPCB refused on its via
// annular ring exactly as the engine's capability check refuses it.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => ({
  openPath: vi.fn(() => Promise.resolve()),
  revealItemInDir: vi.fn(() => Promise.resolve()),
  openUrl: vi.fn(() => Promise.resolve()),
}));
vi.mock("@tauri-apps/plugin-fs", () => ({ readFile: vi.fn(() => new Promise(() => {})) }));

import { openUrl, revealItemInDir } from "@tauri-apps/plugin-opener";
import { leadTimeText, orderDetails, orderPanel, priceAmount } from "@/lib/silkscreen/steps";
import type { FabHousesBlock, StepResponse } from "@/lib/silkscreen/types";
import { OrderYourBoard } from "./OrderYourBoard";

const mockOpenUrl = vi.mocked(openUrl);
const mockReveal = vi.mocked(revealItemInDir);

const ZIP = "/tmp/steps/s1/esp32-order.zip";
const BOUNDARY =
  "Ada prepared and priced this order. You place it and pay the fab on their own site; nothing has been ordered or paid for.";

const BLOCK: FabHousesBlock = {
  houses: [
    {
      id: "oshpark-2layer",
      house: "OSH Park",
      service: "2 Layer Prototype",
      buildable: true,
      blockers: [],
      warnings: [],
      price: {
        total_cents: 2732,
        currency: "USD",
        boards: 3,
        text: "3 boards, $27.32",
        basis: "published-rule",
        shipping_cents: 0,
      },
      price_note: null,
      unpriced_reason: null,
      lead_time_days: [9, 12],
      quote_url: "https://oshpark.com/",
      source_url: "https://docs.oshpark.com/services/two-layer/",
      recommended: true,
    },
    {
      id: "oshpark-2layer-swift",
      house: "OSH Park",
      service: "2 Layer Super Swift",
      buildable: true,
      blockers: [],
      warnings: [],
      price: {
        total_cents: 5464,
        currency: "USD",
        boards: 3,
        text: "3 boards, $54.64",
        basis: "published-rule",
        shipping_cents: 0,
      },
      price_note: null,
      lead_time_days: [4, 5],
      quote_url: "https://oshpark.com/",
      recommended: false,
    },
    {
      id: "jlcpcb-2layer",
      house: "JLCPCB",
      service: "2 Layer FR-4, standard",
      buildable: false,
      blockers: [
        {
          code: "annular-ring-below-fab-minimum",
          title: "A via's annular ring is thinner than JLCPCB 2 Layer FR-4, standard allows",
          detail: "The worst via leaves 0.15 mm of copper around its hole; JLCPCB requires 0.18 mm",
        },
      ],
      warnings: [],
      price: null,
      price_note: "Quote on JLCPCB",
      unpriced_reason: "JLCPCB quotes through its API platform, which requires a registered application's key and secret.",
      lead_time_days: [2, 5],
      quote_url: "https://cart.jlcpcb.com/quote",
      recommended: false,
    },
    {
      id: "pcbway-2layer",
      house: "PCBWay",
      service: "2 Layer FR-4, standard",
      buildable: true,
      blockers: [],
      warnings: [],
      price: null,
      price_note: "Quote on PCBWay",
      lead_time_days: [2, 5],
      quote_url: "https://www.pcbway.com/orderonline.aspx",
      recommended: false,
    },
  ],
  recommended: "oshpark-2layer",
  recommended_reason:
    "Lowest published price among the houses that will build this board: 3 boards, $27.32 at OSH Park 2 Layer Prototype.",
  boundary: BOUNDARY,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const houseCard = (house: string) =>
  screen.getAllByTestId("fab-house").find((li) => li.getAttribute("data-house") === house) as HTMLElement;

describe("orderPanel", () => {
  it("groups one card per house and leads with the recommended service", () => {
    const panel = orderPanel(BLOCK)!;
    expect(panel.houses.map((g) => g.house)).toEqual(["OSH Park", "JLCPCB", "PCBWay"]);
    expect(panel.houses[0].primary.id).toBe("oshpark-2layer");
    expect(panel.houses[0].others.map((c) => c.id)).toEqual(["oshpark-2layer-swift"]);
    expect(panel.houses.filter((g) => g.recommended).map((g) => g.house)).toEqual(["OSH Park"]);
    expect(panel.boundary).toBe(BOUNDARY);
  });

  it("is absent for an engine that sends no block, or an empty one", () => {
    expect(orderPanel(undefined)).toBeNull();
    expect(orderPanel({ ...BLOCK, houses: [] })).toBeNull();
  });

  it("formats only numbers the engine sent", () => {
    expect(priceAmount(BLOCK.houses[0])).toBe("$27.32");
    expect(priceAmount(BLOCK.houses[3])).toBeNull();
    expect(leadTimeText(BLOCK.houses[0])).toBe("9–12 days");
    expect(leadTimeText({ ...BLOCK.houses[0], lead_time_days: null })).toBeNull();
  });

  it("rides on orderDetails from the order envelope", () => {
    const response = {
      session: "s1",
      step: "order",
      stage: "routed",
      intent: "esp32",
      files: { order: ZIP },
      next: [],
      shown_in_kicad: false,
      fab_houses: BLOCK,
    } as unknown as StepResponse;
    expect(orderDetails(response)?.fabHouses?.houses).toHaveLength(3);
    expect(orderDetails({ ...response, fab_houses: undefined })?.fabHouses).toBeNull();
  });
});

describe("OrderYourBoard", () => {
  it("shows the price big with its board count, and a green check", () => {
    render(<OrderYourBoard panel={orderPanel(BLOCK)!} zip={ZIP} />);
    expect(screen.getByText("Order your board")).toBeTruthy();
    const osh = houseCard("OSH Park");
    expect(osh.getAttribute("data-recommended")).toBe("true");
    expect(osh.querySelector('[data-testid="fab-recommended"]')).toBeTruthy();
    expect(osh.querySelector('[data-testid="fab-buildable"]')?.textContent).toContain("Builds this board");
    expect(osh.querySelector('[data-testid="fab-price-amount"]')?.textContent).toBe("$27.32");
    expect(osh.textContent).toContain("3 boards");
    expect(osh.querySelector('[data-testid="fab-lead-time"]')?.textContent).toContain("9–12 days");
    expect(osh.querySelector('[data-testid="fab-alternative"]')?.textContent).toContain(
      "2 Layer Super Swift: 3 boards, $54.64, 4–5 days"
    );
    expect(screen.getByTestId("order-panel-reason").textContent).toContain("Lowest published price");
  });

  it("gives an unpriced house its quote link and no number", () => {
    render(<OrderYourBoard panel={orderPanel(BLOCK)!} zip={ZIP} />);
    const pcbway = houseCard("PCBWay");
    expect(pcbway.querySelector('[data-testid="fab-price-amount"]')).toBeNull();
    expect(pcbway.querySelector('[data-testid="fab-price-note"]')?.textContent).toBe("Quote on PCBWay");
    expect(pcbway.textContent).not.toContain("$");
  });

  it("names the capability check's reason when a house will not build it", () => {
    render(<OrderYourBoard panel={orderPanel(BLOCK)!} zip={ZIP} />);
    const jlc = houseCard("JLCPCB");
    expect(jlc.getAttribute("data-buildable")).toBe("false");
    expect(jlc.querySelector('[data-testid="fab-not-buildable"]')?.textContent).toContain("Won't build it");
    const reasons = Array.from(jlc.querySelectorAll('[data-testid="fab-reason"]'));
    expect(reasons.map((r) => r.textContent)).toEqual([
      "A via's annular ring is thinner than JLCPCB 2 Layer FR-4, standard allows",
    ]);
    expect(reasons[0].getAttribute("title")).toContain("0.18 mm");
    expect(jlc.getAttribute("data-recommended")).toBe("false");
  });

  it("opens the house's own page and reveals the files, and nothing else", () => {
    render(<OrderYourBoard panel={orderPanel(BLOCK)!} zip={ZIP} />);
    const buttons = screen.getAllByTestId("fab-open");
    expect(buttons.map((b) => b.textContent)).toEqual(["Open OSH Park", "Open JLCPCB", "Open PCBWay"]);
    fireEvent.click(buttons[1]);
    expect(mockOpenUrl).toHaveBeenCalledWith("https://cart.jlcpcb.com/quote");
    fireEvent.click(screen.getByTestId("order-panel-reveal"));
    expect(mockReveal).toHaveBeenCalledWith(ZIP);
    expect(screen.getByTestId("order-panel-boundary").textContent).toBe(BOUNDARY);
  });

  it("shows the URL when no browser opens it, instead of failing silently", async () => {
    mockOpenUrl.mockRejectedValueOnce("no handler");
    render(<OrderYourBoard panel={orderPanel(BLOCK)!} zip={null} />);
    expect(screen.queryByTestId("order-panel-reveal")).toBeNull();
    fireEvent.click(screen.getAllByTestId("fab-open")[0]);
    await waitFor(() => expect(screen.getByTestId("order-panel-note")).toBeTruthy());
    expect(screen.getByTestId("order-panel-note").textContent).toContain("https://oshpark.com/");
  });
});
