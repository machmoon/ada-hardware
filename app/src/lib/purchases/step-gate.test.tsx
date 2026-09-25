// @vitest-environment jsdom
//
// The Ada Pro gate on the strip's order step, driven by a supplied purchases
// state. `free` and a 402 from the service lock the button behind Settings,
// and the 402 lock yields once the desktop's verdict turns `entitled`;
// `unknown` leaves it live with a note; `entitled` changes nothing. It lives
// here beside the purchases seam because the gate is the seam's one consumer
// on the strip.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(() => Promise.resolve()) }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => ({
  openPath: vi.fn(() => Promise.resolve()),
  revealItemInDir: vi.fn(() => Promise.resolve()),
  openUrl: vi.fn(() => Promise.resolve()),
}));
vi.mock("@tauri-apps/plugin-fs", () => ({ readFile: vi.fn(() => new Promise(() => {})) }));

import { invoke } from "@tauri-apps/api/core";
import { PurchasesStateProvider, type PurchasesState } from "@/contexts/purchases.context";
import type { StepRun } from "@/hooks/useStepRun";
import { SilkscreenError } from "@/lib/silkscreen/client";
import type { StepName, StepResponse } from "@/lib/silkscreen/types";
import { StepPanel } from "@/pages/kaleo/components/StepPanel";
import { PRO_BUTTON_LABEL } from "./client";
import { readPaneRequest } from "./pane";

const mockInvoke = vi.mocked(invoke);

function routed(next: StepName[]): StepResponse {
  return {
    session: "s1",
    step: "route",
    stage: "routed",
    intent: "a 3.3V LDO board",
    files: { board: "/tmp/steps/s1/board.kicad_pcb" },
    next,
    shown_in_kicad: true,
    events: [],
    duration_s: 3,
  };
}

function waiting(available: StepName[]): StepRun {
  return {
    status: "waiting",
    session: "s1",
    history: [routed(available)],
    running: null,
    failedStep: null,
    available,
    error: null,
    elapsedS: 0,
    start: vi.fn(),
    approve: vi.fn(),
    cancel: vi.fn(),
    reset: vi.fn(),
  };
}

function purchases(status: PurchasesState["status"], reason: string | null = null): PurchasesState {
  return {
    status,
    configured: status !== "unknown",
    sandbox: false,
    appUserId: null,
    reason,
    buy: vi.fn(() => Promise.reject(new Error("not in this test"))),
    refresh: vi.fn(() => Promise.resolve()),
  };
}

function draw(run: StepRun, state: PurchasesState, extra: Partial<React.ComponentProps<typeof StepPanel>> = {}) {
  return render(
    <PurchasesStateProvider value={state}>
      <StepPanel run={run} onDismiss={() => {}} {...extra} />
    </PurchasesStateProvider>
  );
}

beforeEach(() => {
  window.localStorage.clear();
  mockInvoke.mockClear();
});

afterEach(cleanup);

describe("the order step under the Ada Pro gate", () => {
  it("free: the order control reads Ada Pro, opens Settings at the pane, and spends nothing", async () => {
    const run = waiting(["order", "case"]);
    draw(run, purchases("free"));
    expect(screen.queryByTestId("step-approve")?.getAttribute("data-step")).toBe("case");
    const pro = screen.getByTestId("step-pro");
    expect(pro.textContent).toBe(PRO_BUTTON_LABEL);
    expect(pro.textContent).toBe("Prepare fab order · Ada Pro");
    expect(pro.getAttribute("data-locked")).toBe("pro");
    expect(pro.textContent).not.toContain("1 call");
    fireEvent.click(pro);
    expect(run.approve).not.toHaveBeenCalled();
    // The request is in storage before the dashboard is asked for, so a
    // window the command creates reads it as it mounts.
    expect(readPaneRequest()).toBe("pro");
    await waitFor(() => expect(mockInvoke).toHaveBeenCalledWith("open_dashboard"));
    expect(screen.queryByTestId("pro-note")).toBeNull();
  });

  it("free: the other steps are untouched and keep their price", () => {
    draw(waiting(["route", "order"]), purchases("free"));
    const route = screen.getByTestId("step-approve");
    expect(route.getAttribute("data-step")).toBe("route");
    expect(route.textContent).toBe("Route copper · 1 call");
    expect(screen.getByTestId("step-pro")).toBeTruthy();
  });

  it("unknown: the button stays live and a note says Ada Pro was not checked, with the reason", () => {
    const run = waiting(["order"]);
    draw(run, purchases("unknown", "Ada Pro could not be checked: Network request failed"));
    const approve = screen.getByTestId("step-approve");
    expect(approve.getAttribute("data-step")).toBe("order");
    expect(approve.textContent).toBe("Prepare fab order · 1 call");
    expect(approve.hasAttribute("disabled")).toBe(false);
    fireEvent.click(approve);
    expect(run.approve).toHaveBeenCalledWith("order");
    expect(screen.getByTestId("pro-note").textContent).toBe(
      "Ada Pro not checked: Ada Pro could not be checked: Network request failed"
    );
    expect(screen.queryByTestId("step-pro")).toBeNull();
  });

  it("checking: the same as unknown, so a check in flight never locks anything", () => {
    draw(waiting(["order"]), purchases("checking", "Ada Pro has not been checked yet."));
    expect(screen.getByTestId("step-approve").getAttribute("data-step")).toBe("order");
    expect(screen.getByTestId("pro-note").getAttribute("data-status")).toBe("checking");
  });

  it("the note is not shown when the order step is not on offer", () => {
    draw(waiting(["route"]), purchases("unknown", "no key"));
    expect(screen.queryByTestId("pro-note")).toBeNull();
  });

  it("entitled: nothing changes", () => {
    const run = waiting(["order"]);
    draw(run, purchases("entitled"));
    const approve = screen.getByTestId("step-approve");
    expect(approve.textContent).toBe("Prepare fab order · 1 call");
    expect(screen.queryByTestId("pro-note")).toBeNull();
    expect(screen.queryByTestId("step-pro")).toBeNull();
    fireEvent.click(approve);
    expect(run.approve).toHaveBeenCalledWith("order");
  });

  const refusedByService = (): StepRun => ({
    ...waiting(["order"]),
    status: "error",
    failedStep: "order",
    error: new SilkscreenError("entitlement", "Ada Pro is required to prepare a fab order.", { status: 402 }),
  });

  it("a 402 from the service locks the order step even when the desktop could not check", () => {
    draw(refusedByService(), purchases("unknown", "no key"));
    expect(screen.getByTestId("step-headline").textContent).toBe("Ada Pro is required to prepare a fab order.");
    expect(screen.getByTestId("step-pro").textContent).toBe(PRO_BUTTON_LABEL);
    expect(screen.queryByTestId("step-approve")).toBeNull();
  });

  it("a 402 while the desktop also says free is locked", () => {
    draw(refusedByService(), purchases("free"));
    expect(screen.getByTestId("step-pro").textContent).toBe(PRO_BUTTON_LABEL);
    expect(screen.queryByTestId("step-approve")).toBeNull();
  });

  it("a 402, then Pro bought: the button comes back without starting the run over", () => {
    const run = refusedByService();
    draw(run, purchases("entitled"));
    const approve = screen.getByTestId("step-approve");
    expect(approve.getAttribute("data-step")).toBe("order");
    expect(approve.textContent).toBe("Prepare fab order · 1 call");
    expect(screen.queryByTestId("step-pro")).toBeNull();
    expect(screen.queryByTestId("pro-note")).toBeNull();
    fireEvent.click(approve);
    expect(run.approve).toHaveBeenCalledWith("order");
  });

  it("any other error on the order step keeps the paid retry", () => {
    const failed: StepRun = {
      ...waiting(["order"]),
      status: "error",
      failedStep: "order",
      error: new SilkscreenError("server", "order failed", { status: 500 }),
    };
    draw(failed, purchases("entitled"));
    expect(screen.getByTestId("step-approve").textContent).toContain("1 call");
    expect(screen.queryByTestId("step-pro")).toBeNull();
  });

  it("an armed order under free confirms into Settings, not into a paid step", () => {
    const onConfirm = vi.fn();
    const run = waiting(["order"]);
    draw(run, purchases("free"), { armed: { step: "order", source: "order it" }, onConfirm });
    expect(screen.queryByTestId("step-confirm")).toBeNull();
    fireEvent.click(screen.getByTestId("step-pro"));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(run.approve).not.toHaveBeenCalled();
    expect(readPaneRequest()).toBe("pro");
    expect(screen.getByTestId("step-disarm")).toBeTruthy();
  });
});
