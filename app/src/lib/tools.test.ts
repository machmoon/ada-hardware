import { describe, expect, it } from "vitest";
import { formatBytes, formatEta, nextRate, progressCaption } from "./tools";

describe("tools progress wording", () => {
  it("formats sizes in the decimal units the hosts publish", () => {
    expect(formatBytes(1_404_303_659)).toBe("1.40 GB");
    expect(formatBytes(649_856_006)).toBe("650 MB");
    expect(formatBytes(4_200_000)).toBe("4.2 MB");
    expect(formatBytes(512_000)).toBe("512 KB");
  });

  it("says time left in seconds, minutes or hours, and nothing for no answer", () => {
    expect(formatEta(9.4)).toBe("9s left");
    expect(formatEta(63)).toBe("1m 3s left");
    expect(formatEta(3_725)).toBe("1h 2m left");
    expect(formatEta(0)).toBe("");
    expect(formatEta(Infinity)).toBe("");
  });

  it("smooths the rate instead of jumping to each burst", () => {
    let r = nextRate(null, 0, 0);
    r = nextRate(r, 10_000_000, 1000);
    expect(r.bytesPerSecond).toBe(10_000_000);
    r = nextRate(r, 60_000_000, 2000); // a 50 MB/s burst
    expect(r.bytesPerSecond).toBe(18_000_000);
    // A reset (retry from zero) keeps the old rate rather than going negative.
    expect(nextRate(r, 0, 3000).bytesPerSecond).toBe(18_000_000);
  });

  it("captions bytes, speed and time left, and bytes alone when the total is unknown", () => {
    expect(progressCaption(412_000_000, 1_404_303_659, 18_200_000)).toBe("412 MB of 1.40 GB · 18.2 MB/s · 55s left");
    expect(progressCaption(5_000_000, 0, 0)).toBe("5.0 MB");
  });
});
