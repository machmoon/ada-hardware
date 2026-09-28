// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const healthMock = vi.fn();
vi.mock("@/hooks/useEngineHealth", () => ({ useEngineHealth: (...args: unknown[]) => healthMock(...args) }));
const keyMock = vi.fn();
vi.mock("@/lib/setup/engine", async () => {
  const actual = await vi.importActual<typeof import("@/lib/setup/engine")>("@/lib/setup/engine");
  return { ...actual, fetchKeyStatus: (...args: unknown[]) => keyMock(...args) };
});

import {
  AUTO_ADVANCE_MS,
  ENGINE_TITLE,
  ENGINE_TITLE_DOWN,
  EngineStep,
} from "./EngineStep";

const BASE = "http://127.0.0.1:8081";

function health(ok: boolean, detail = "") {
  return { baseUrl: BASE, ok, detail, checking: false, lastCheckedAt: 1, recheck: vi.fn() };
}

beforeEach(() => {
  healthMock.mockReset();
  keyMock.mockReset();
});
afterEach(cleanup);

const mount = (over: Partial<React.ComponentProps<typeof EngineStep>> = {}) => {
  const onCanContinue = vi.fn();
  const onAutoAdvance = vi.fn();
  render(
    <MemoryRouter>
      <EngineStep
        baseUrl={BASE}
        token="tok"
        onCanContinue={onCanContinue}
        onAutoAdvance={onAutoAdvance}
        {...over}
      />
    </MemoryRouter>,
  );
  return { onCanContinue, onAutoAdvance };
};

describe("EngineStep", () => {
  it("polls every 2 s with the bearer", () => {
    healthMock.mockReturnValue(health(false, "connection refused"));
    mount();
    expect(healthMock).toHaveBeenCalledWith(BASE, 2000, "tok");
  });

  it("when nothing answers: the terminal heading, the two start commands, Continue blocked", () => {
    healthMock.mockReturnValue(health(false, "connection refused"));
    const h = mount();
    expect(screen.getByTestId("setup-title").textContent).toBe(ENGINE_TITLE_DOWN);
    expect(screen.getByTestId("engine-card").getAttribute("data-state")).toBe("down");
    expect(screen.getByTestId("engine-answer").textContent).toBe(`Nothing answered at ${BASE}`);
    const steps = screen.getAllByTestId("engine-start-step").map((el) => el.getAttribute("data-step"));
    expect(steps).toEqual(["serve"]);
    expect(h.onCanContinue).toHaveBeenLastCalledWith(false);
    expect(h.onAutoAdvance).not.toHaveBeenCalled();
    expect(keyMock).not.toHaveBeenCalled();
  });

  it("when it answers: says which route at which address, never Running, and asks for the key", async () => {
    healthMock.mockReturnValue(health(true));
    keyMock.mockResolvedValue({ state: "ready", summary: "API access verified; 3 generation models available." });
    const h = mount();
    expect(screen.getByTestId("setup-title").textContent).toBe(ENGINE_TITLE);
    expect(screen.getByTestId("engine-answer").textContent).toBe(`Answered /healthz at ${BASE}`);
    expect(screen.getByTestId("engine-answer").textContent).not.toMatch(/Running/);
    await waitFor(() => expect(screen.getByTestId("engine-key").getAttribute("data-state")).toBe("ready"));
    expect(keyMock).toHaveBeenCalledWith(BASE, "tok");
    expect(screen.getByTestId("engine-key").textContent).toContain("API access verified");
    expect(h.onCanContinue).toHaveBeenLastCalledWith(true);
    expect(screen.queryByTestId("engine-start-commands")).toBeNull();
  });

  it("a warning still allows Continue; an error blocks it with the service's sentence", async () => {
    healthMock.mockReturnValue(health(true));
    keyMock.mockResolvedValueOnce({ state: "warning", summary: "Key loaded, but live model discovery could not be verified." });
    const first = mount();
    await waitFor(() => expect(first.onCanContinue).toHaveBeenLastCalledWith(true));
    expect(screen.getByTestId("engine-key").textContent).toContain("could not be verified");
    cleanup();

    keyMock.mockResolvedValueOnce({ state: "error", summary: "Add GOOGLE_API_KEY to run the orchestrator and workers." });
    const second = mount();
    await waitFor(() => expect(screen.getByTestId("engine-key").getAttribute("data-state")).toBe("error"));
    expect(screen.getByTestId("engine-key").textContent).toBe("Add GOOGLE_API_KEY to run the orchestrator and workers.");
    expect(second.onCanContinue).toHaveBeenLastCalledWith(false);
    expect(second.onAutoAdvance).not.toHaveBeenCalled();
  });

  it("auto-advances once, 600 ms after the first usable answer", async () => {
    healthMock.mockReturnValue(health(true));
    keyMock.mockResolvedValue({ state: "ready", summary: "ok" });
    const h = mount();
    await waitFor(() => expect(h.onCanContinue).toHaveBeenLastCalledWith(true));
    // Not yet: the engine sits on screen for its 600 ms first.
    expect(h.onAutoAdvance).not.toHaveBeenCalled();
    await act(async () => {
      await new Promise((r) => setTimeout(r, AUTO_ADVANCE_MS + 100));
    });
    expect(h.onAutoAdvance).toHaveBeenCalledTimes(1);
  });
});
