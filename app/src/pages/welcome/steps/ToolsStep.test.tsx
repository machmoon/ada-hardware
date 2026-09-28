// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ToolEvent, ToolId, ToolStatus, ToolsApi } from "@/lib/tools";
import { TOOLS_UNKNOWN, ToolsStep } from "./ToolsStep";

afterEach(cleanup);

const tool = (id: ToolId, over: Partial<ToolStatus> = {}): ToolStatus => ({
  id,
  name: { kicad: "KiCad", freecad: "FreeCAD", ngspice: "ngspice" }[id],
  version: "1",
  purpose: `${id} purpose`,
  installed: false,
  detail: "Not on this Mac.",
  downloadBytes: id === "ngspice" ? null : 1_000_000_000,
  installable: true,
  ...over,
});

/** An install the test finishes by hand, with the channel it was given. */
function controllable(initial: ToolStatus[]) {
  let list = initial;
  const pending: Record<string, { emit: (e: ToolEvent) => void; resolve: (p: string) => void; reject: (e: Error) => void }> =
    {};
  const api: ToolsApi = {
    status: vi.fn(async () => list),
    install: vi.fn(
      (id: ToolId, onEvent: (e: ToolEvent) => void) =>
        new Promise<string>((resolve, reject) => {
          pending[id] = { emit: onEvent, resolve, reject };
        }),
    ),
    cancel: vi.fn(async () => undefined),
  };
  return {
    api,
    pending,
    setList: (next: ToolStatus[]) => {
      list = next;
    },
  };
}

const rowOf = (id: string) => document.querySelector(`[data-testid="tool-row"][data-tool="${id}"]`) as HTMLElement;

describe("ToolsStep", () => {
  it("lists every tool, marks what is here, and records only real installs as cards", async () => {
    const c = controllable([tool("kicad", { installed: true, detail: "/Applications/KiCad" }), tool("freecad"), tool("ngspice")]);
    const setCard = vi.fn();
    render(<ToolsStep setCard={setCard} api={c.api} />);
    await waitFor(() => expect(rowOf("kicad").dataset.phase).toBe("done"));
    expect(rowOf("freecad").textContent).toMatch(/1\.00 GB/);
    expect(setCard).toHaveBeenCalledWith("kicad", true);
    expect(setCard).toHaveBeenCalledWith("freecad", false);
    expect(screen.getByTestId("tools-download-all").textContent).toMatch(/Download all · 1\.00 GB/);
  });

  it("downloads one at a time: the second waits as Queued, and the bar shows bytes, speed and time left", async () => {
    const c = controllable([tool("kicad"), tool("freecad")]);
    let t = 0;
    render(<ToolsStep setCard={vi.fn()} api={c.api} now={() => t} />);
    await waitFor(() => screen.getByTestId("tools-download-all"));
    fireEvent.click(screen.getByTestId("tools-download-all"));

    await waitFor(() => expect(c.api.install).toHaveBeenCalledTimes(1));
    expect(rowOf("freecad").dataset.phase).toBe("queued");
    const kicad = c.pending.kicad;
    act(() => kicad.emit({ event: "started", data: { id: "kicad", contentLength: 1_000_000_000 } }));
    act(() => kicad.emit({ event: "progress", data: { id: "kicad", transferred: 0, contentLength: 1_000_000_000 } }));
    t = 1000;
    act(() => kicad.emit({ event: "progress", data: { id: "kicad", transferred: 100_000_000, contentLength: 1_000_000_000 } }));
    const bar = rowOf("kicad").querySelector('[data-testid="tool-progress"]')!;
    expect(bar.getAttribute("aria-valuenow")).toBe("10");
    expect(rowOf("kicad").querySelector('[data-testid="tool-caption"]')!.textContent).toBe(
      "100 MB of 1.00 GB · 100.0 MB/s · 9s left",
    );

    act(() => kicad.emit({ event: "verifying", data: { id: "kicad" } }));
    expect(bar.getAttribute("aria-valuenow")).toBeNull();
    c.setList([tool("kicad", { installed: true }), tool("freecad")]);
    await act(async () => {
      kicad.emit({ event: "finished", data: { id: "kicad", path: "/Applications/KiCad" } });
      kicad.resolve("/Applications/KiCad");
    });
    await waitFor(() => expect(c.api.install).toHaveBeenCalledTimes(2));
    expect((c.api.install as ReturnType<typeof vi.fn>).mock.calls[1][0]).toBe("freecad");
    expect(rowOf("kicad").dataset.phase).toBe("done");
  });

  it("a failure says why and offers Retry; a cancel goes back to Download", async () => {
    const c = controllable([tool("freecad"), tool("ngspice")]);
    render(<ToolsStep setCard={vi.fn()} api={c.api} />);
    await waitFor(() => screen.getAllByTestId("tool-download"));
    fireEvent.click(rowOf("freecad").querySelector('[data-testid="tool-download"]')!);
    await waitFor(() => expect(c.pending.freecad).toBeDefined());
    const message = "The FreeCAD download does not match its published SHA-256; it was deleted.";
    await act(async () => {
      c.pending.freecad.emit({ event: "failed", data: { id: "freecad", message, cancelled: false } });
      c.pending.freecad.reject(new Error(message));
    });
    await waitFor(() => expect(rowOf("freecad").dataset.phase).toBe("failed"));
    expect(rowOf("freecad").textContent).toContain(message);
    expect(rowOf("freecad").querySelector('[data-testid="tool-download"]')!.textContent).toBe("Retry");

    fireEvent.click(rowOf("ngspice").querySelector('[data-testid="tool-download"]')!);
    await waitFor(() => expect(c.pending.ngspice).toBeDefined());
    fireEvent.click(rowOf("ngspice").querySelector('[data-testid="tool-cancel"]')!);
    expect(c.api.cancel).toHaveBeenCalledWith("ngspice");
    await act(async () => {
      c.pending.ngspice.emit({ event: "failed", data: { id: "ngspice", message: "Cancelled.", cancelled: true } });
      c.pending.ngspice.reject(new Error("Cancelled."));
    });
    await waitFor(() => expect(rowOf("ngspice").dataset.phase).toBe("idle"));
  });

  it("Homebrew's install has no byte count, so the bar is indeterminate and shows brew's own line", async () => {
    const c = controllable([tool("ngspice")]);
    render(<ToolsStep setCard={vi.fn()} api={c.api} />);
    await waitFor(() => screen.getByTestId("tool-download"));
    fireEvent.click(screen.getByTestId("tool-download"));
    await waitFor(() => expect(c.pending.ngspice).toBeDefined());
    act(() => c.pending.ngspice.emit({ event: "started", data: { id: "ngspice", contentLength: 0 } }));
    act(() => c.pending.ngspice.emit({ event: "installing", data: { id: "ngspice" } }));
    act(() => c.pending.ngspice.emit({ event: "log", data: { id: "ngspice", line: "==> Pouring ngspice--45.arm64.bottle.tar.gz" } }));
    expect(screen.getByTestId("tool-progress").getAttribute("aria-valuenow")).toBeNull();
    expect(screen.getByTestId("tool-caption").textContent).toBe("==> Pouring ngspice--45.arm64.bottle.tar.gz");
  });

  it("a tool this Mac cannot install says what to do by hand, with no Download button", async () => {
    const detail = "ngspice has no macOS download; it needs Homebrew (brew.sh), then `brew install ngspice`.";
    const c = controllable([tool("ngspice", { installable: false, detail })]);
    render(<ToolsStep setCard={vi.fn()} api={c.api} />);
    await waitFor(() => screen.getByTestId("tool-manual"));
    expect(screen.getByTestId("tool-detail").textContent).toBe(detail);
    expect(screen.queryByTestId("tool-download")).toBeNull();
  });

  it("outside the desktop app it says it could not ask, never 'install KiCad'", async () => {
    const api: ToolsApi = {
      status: () => Promise.reject(new Error("no IPC")),
      install: vi.fn(),
      cancel: vi.fn(),
    };
    render(<ToolsStep setCard={vi.fn()} api={api} />);
    await waitFor(() => expect(screen.getByTestId("tools-unknown").textContent).toBe(TOOLS_UNKNOWN));
  });
});
